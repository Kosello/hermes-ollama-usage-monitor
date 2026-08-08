"""
Ollama Cloud Usage — backend API routes for the desktop plugin.

Mounted at /api/plugins/ollama-usage/ by the Hermes plugin backend.
Scrapes ollama.com/settings with the session cookie (same approach as the
community /ollama slash command, extended with per-model request counts
and a small in-memory cache so the settings page is not hammered).

Cookie storage (portable — works on any OS):
  - macOS Keychain  (service 'hermes-ollama-cookie', account 'ollama')
  - or plain file   ~/.hermes/ollama_cookie.txt  (__Secure-session=<value>)
  Selection via env OLLAMA_COOKIE_SOURCE=auto|keychain|file (default: auto).
  'auto' = Keychain if a cookie is stored there, otherwise the file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
KEYCHAIN_SERVICE = "hermes-ollama-cookie"
KEYCHAIN_ACCOUNT = "ollama"
SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15
CACHE_TTL_SECONDS = 60
HISTORY_FILE = Path.home() / ".hermes" / "ollama-usage-history.jsonl"
HISTORY_MAX_WEEKS = 8

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


def _cookie_source() -> str:
    """Resolve the cookie storage source: auto | keychain | file."""
    return os.environ.get("OLLAMA_COOKIE_SOURCE", "auto").strip().lower()


def _keychain_cookie() -> str | None:
    """Read cookie from macOS Keychain. Returns None if unavailable/empty."""
    if _cookie_source() == "file":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass  # not macOS / no security binary / timeout
    return None


def _keychain_store_cookie(cookie: str) -> bool:
    """Store cookie in macOS Keychain. Returns False on non-macOS or failure."""
    try:
        # Try delete-then-add so an expired cookie gets replaced.
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True, timeout=10,
        )
        add = subprocess.run(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w", cookie, "-U"],
            capture_output=True, text=True, timeout=10,
        )
        return add.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _load_cookie() -> str:
    """Read the session cookie — Keychain first (auto), then the file."""
    source = _cookie_source()
    if source in ("auto", "keychain"):
        kc = _keychain_cookie()
        if kc:
            return kc
        if source == "keychain":
            raise FileNotFoundError(
                f"No cookie in macOS Keychain ({KEYCHAIN_SERVICE}/{KEYCHAIN_ACCOUNT}).\n"
                f"Store it with: security add-generic-password -s {KEYCHAIN_SERVICE} "
                f"-a {KEYCHAIN_ACCOUNT} -w '<cookie>'"
            )

    if source in ("auto", "file"):
        if not COOKIE_FILE.exists():
            raise FileNotFoundError(
                f"Cookie file not found at {COOKIE_FILE}\n"
                f"Run: echo '__Secure-session=<value>' > {COOKIE_FILE}"
            )
        cookie = COOKIE_FILE.read_text().strip()
        if not cookie:
            raise ValueError("Cookie file is empty")
        return cookie

    raise ValueError(f"Unknown OLLAMA_COOKIE_SOURCE: {source} (use auto|keychain|file)")


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


def _week_key(iso_str: str | None) -> str | None:
    """ISO week start (Monday) for a reset timestamp — used to dedupe snapshots."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        monday = dt - timedelta(days=dt.weekday())
        return monday.date().isoformat()
    except (ValueError, TypeError):
        return None


def _record_history(data: dict) -> None:
    """Append one snapshot per ISO week to the JSONL history file."""
    if not data.get("ok") or data.get("weekly_used_pct") is None:
        return
    week = _week_key(data.get("weekly_reset"))
    if not week:
        return
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Dedupe: rewrite file without an existing record for this week,
        # keeping the latest snapshot per week (Ollama resets weekly).
        kept = []
        if HISTORY_FILE.exists():
            for line in HISTORY_FILE.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("week") != week:
                    kept.append(line)
        record = {
            "week": week,
            "ts": _utc_now().isoformat(timespec="seconds"),
            "weekly_used_pct": data.get("weekly_used_pct"),
            "session_used_pct": data.get("session_used_pct"),
            "est_cost_consumed": data.get("est_cost_consumed"),
            "est_weekly_budget": data.get("est_weekly_budget"),
            "models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct")}
                for m in (data.get("weekly_models") or [])
            ],
        }
        kept.append(json.dumps(record))
        HISTORY_FILE.write_text("\n".join(kept) + "\n")
        # Trim to newest HISTORY_MAX_WEEKS records
        lines = HISTORY_FILE.read_text().splitlines()
        if len(lines) > HISTORY_MAX_WEEKS:
            HISTORY_FILE.write_text("\n".join(lines[-HISTORY_MAX_WEEKS:]) + "\n")
    except OSError as e:
        logger.warning("Could not write history: %s", e)


def _history() -> dict:
    """Aggregate weekly history for the pane (newest first, max 8 weeks)."""
    if not HISTORY_FILE.exists():
        return {"ok": True, "weeks": []}
    weeks = []
    for line in HISTORY_FILE.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        top = None
        if rec.get("models"):
            top = max(rec["models"], key=lambda m: m.get("share_pct") or 0)
        weeks.append({
            "week": rec.get("week"),
            "weekly_used_pct": rec.get("weekly_used_pct"),
            "est_cost_consumed": rec.get("est_cost_consumed"),
            "top_model": top.get("model") if top else None,
            "total_requests": sum(m.get("requests") or 0 for m in (rec.get("models") or [])),
        })
    weeks.sort(key=lambda w: w.get("week") or "", reverse=True)
    return {"ok": True, "weeks": weeks[:HISTORY_MAX_WEEKS]}


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
        _record_history(data)
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


@router.get("/usage/history")
async def usage_history():
    """Weekly history aggregation (newest first, max 8 weeks)."""
    return _history()
