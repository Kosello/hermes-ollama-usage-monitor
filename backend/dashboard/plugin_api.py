"""
Ollama Cloud Usage — backend API routes for the desktop plugin.

Mounted at /api/plugins/ollama-usage/ by the Hermes plugin backend.
Scrapes ollama.com/settings with the session cookie (same approach as the
community /ollama slash command, extended with per-model request counts
and a small in-memory cache so the settings page is not hammered).

Cookie file: ~/.hermes/ollama_cookie.txt  (__Secure-session=<value>)
"""

from __future__ import annotations

import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15
CACHE_TTL_SECONDS = 60

_cache: dict = {"ts": 0, "data": None}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_reset(iso_str: str) -> str:
    """Format an ISO timestamp as a human-readable 'in Xh Ym' string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = dt - _utc_now()
        total_s = int(delta.total_seconds())
        if total_s <= 0:
            return f"now ({dt.astimezone().strftime('%H:%M %Z')})"
        hours, rem = divmod(total_s, 3600)
        minutes = rem // 60
        if hours >= 24:
            days, hours = divmod(hours, 24)
            rel = f"in {days}d {hours}h"
        elif hours > 0:
            rel = f"in {hours}h {minutes}m"
        else:
            rel = f"in {minutes}m"
        return f"{rel} ({dt.astimezone().strftime('%H:%M %Z')})"
    except (ValueError, TypeError):
        return iso_str


def _load_cookie() -> str:
    """Read the session cookie from the cookie file."""
    if not COOKIE_FILE.exists():
        raise FileNotFoundError(
            f"Cookie file not found at {COOKIE_FILE}\n"
            f"Run: echo '__Secure-session=<value>' > {COOKIE_FILE}"
        )
    cookie = COOKIE_FILE.read_text().strip()
    if not cookie:
        raise ValueError("Cookie file is empty")
    return cookie


def _fetch_settings_page(cookie: str) -> str:
    """Fetch the Ollama settings page with the session cookie."""
    req = urllib.request.Request(
        SETTINGS_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_usage(html: str) -> dict:
    """Parse the settings page HTML for usage data."""
    result = {
        "plan": None,
        "session_used_pct": None,
        "session_reset": None,
        "weekly_used_pct": None,
        "weekly_reset": None,
        "models": [],
    }

    # Plan tier
    plan_match = re.search(
        r'Cloud usage</span>\s*\n?\s*<span[^>]*>\s*(pro|free|max)\s*<',
        html, re.IGNORECASE
    )
    if plan_match:
        result["plan"] = plan_match.group(1).capitalize()
    else:
        fallback = re.search(r'(Pro|Free|Max)[^<]*Cloud usage', html, re.IGNORECASE)
        if fallback:
            result["plan"] = fallback.group(1).capitalize()
        else:
            plans = re.findall(r'\b(Pro|Free|Max)\b', html)
            if plans:
                result["plan"] = max(set(plans), key=plans.count)

    # Session usage %
    session_pct = re.search(r'Session usage\s+([0-9.]+)%', html)
    if session_pct:
        result["session_used_pct"] = float(session_pct.group(1))

    # Weekly usage %
    weekly_pct = re.search(r'Weekly usage\s+([0-9.]+)%', html)
    if weekly_pct:
        result["weekly_used_pct"] = float(weekly_pct.group(1))

    # Reset timestamps — data-time attributes, first two are session then weekly
    resets = re.findall(r'data-time="([^"]*)"', html)
    if len(resets) >= 1:
        result["session_reset"] = resets[0]
    if len(resets) >= 2:
        result["weekly_reset"] = resets[1]

    # Per-model usage segments — the usage bars are segmented per model:
    # <div data-usage-segment data-model="glm-5.2" data-requests="33"
    #      style="width: 87.7%" aria-label="glm-5.2: 33 requests">
    # Session segments appear first, then weekly segments.
    weekly_pos = html.find("Weekly usage")
    session_segs = []
    weekly_segs = []
    for m in re.finditer(
        r'<button[^>]*data-usage-segment[^>]*>',
        html,
    ):
        tag = m.group(0)
        model_m = re.search(r'data-model="([^"]+)"', tag)
        req_m = re.search(r'data-requests="(\d+)"', tag)
        width_m = re.search(r'width:\s*([0-9.]+)%', tag)
        if not model_m or not req_m:
            continue
        seg = {
            "model": model_m.group(1),
            "requests": int(req_m.group(1)),
            "share_pct": float(width_m.group(1)) if width_m else None,
        }
        if weekly_pos != -1 and m.start() > weekly_pos:
            weekly_segs.append(seg)
        else:
            session_segs.append(seg)

    result["session_models"] = session_segs
    result["weekly_models"] = weekly_segs
    result["models"] = session_segs  # backward-compat: session segments

    # ── Pi-mal-Daumen cost per request ────────────────────────────────────
    # Pro = $20/mo → ~$4.67/week.  Max = $100/mo → ~$23.33/week.
    # cost_consumed = weekly_budget × (weekly_used_pct / 100)
    # per_model_cost = cost_consumed × (share_pct / 100)
    # per_request = per_model_cost / requests
    weekly_budgets = {"pro": 20.0 / 4.33, "max": 100.0 / 4.33, "free": 0.0}
    weekly_budget = weekly_budgets.get((result.get("plan") or "").lower(), 20.0 / 4.33)
    weekly_used = result.get("weekly_used_pct") or 0.0
    cost_consumed = weekly_budget * (weekly_used / 100.0)

    for seg in weekly_segs:
        share = seg.get("share_pct") or 0.0
        seg["est_cost"] = round(cost_consumed * (share / 100.0), 4)
        if seg["requests"] > 0:
            seg["est_cost_per_req"] = round(seg["est_cost"] / seg["requests"], 4)
            seg["est_cost_per_req_pct"] = round(seg["est_cost_per_req"] / weekly_budget * 100.0, 4)
        else:
            seg["est_cost_per_req"] = 0.0
            seg["est_cost_per_req_pct"] = 0.0

    result["est_weekly_budget"] = round(weekly_budget, 2)
    result["est_cost_consumed"] = round(cost_consumed, 2)
    total_reqs = sum(s["requests"] for s in weekly_segs)
    result["est_avg_cost_per_req"] = round(cost_consumed / total_reqs, 4) if total_reqs else 0.0

    return result


def _fetch_usage() -> dict:
    """Fetch Ollama Cloud usage — returns dict with data or error."""
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        data = dict(_cache["data"])
        data["cached"] = True
        data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return data

    try:
        cookie = _load_cookie()
        html = _fetch_settings_page(cookie)
        data = _parse_usage(html)
        data["cached"] = False
        data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["ok"] = True
        _cache["ts"] = now
        _cache["data"] = data
        return data
    except FileNotFoundError as e:
        return {"ok": False, "error": "cookie_not_configured", "detail": str(e)}
    except Exception as e:
        logger.warning("Ollama usage fetch failed: %s", e)
        return {
            "ok": False,
            "error": "fetch_failed",
            "detail": "cookie expired or settings page changed",
        }


@router.get("/usage")
async def usage():
    """Full usage snapshot for the desktop plugin."""
    return _fetch_usage()


@router.get("/usage/refresh")
async def usage_refresh():
    """Force-refresh (bypass cache)."""
    _cache["ts"] = 0
    _cache["data"] = None
    return _fetch_usage()
