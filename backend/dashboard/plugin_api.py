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
import sqlite3
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
SESSION_FILE = Path.home() / ".hermes" / "ollama-usage-sessions.jsonl"
REPORT_FILE = Path.home() / ".hermes" / "ollama-usage-report.md"
SESSION_LOG_CAP = 500

_cache: dict = {"ts": 0, "data": None}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _relative_reset(iso_str: str) -> str:
    """Format a reset timestamp as compact relative time: '4h', '23 min', '2 days'."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        total_s = int((dt - _utc_now()).total_seconds())
        if total_s <= 0:
            return "now"
        minutes = total_s // 60
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        return f"{days} day" if days == 1 else f"{days} days"
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

    # Reset timestamps — data-time attributes, first two are session then weekly.
    # Stored as compact relative time for display ('4h', '2 days'), raw ISO
    # kept in session_reset_iso / weekly_reset_iso for the history week-key.
    resets = re.findall(r'data-time="([^"]*)"', html)
    if len(resets) >= 1:
        result["session_reset"] = _relative_reset(resets[0])
        result["session_reset_iso"] = resets[0]
    if len(resets) >= 2:
        result["weekly_reset"] = _relative_reset(resets[1])
        result["weekly_reset_iso"] = resets[1]

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

    # ── Real API price comparison ─────────────────────────────────────────
    # Official API list prices per 1M tokens (input/output), USD, Aug 2026:
    #   glm-5.2            $1.40 / $4.40   (Z.ai official; cache-hit in $0.26)
    #   deepseek-v4-flash  $0.14 / $0.28   (DeepSeek; cache-hit in $0.0028)
    #   deepseek-v4-pro    $1.74 / $3.48   (DeepSeek; cache-hit in $0.0036)
    #   minimax-m3         $0.24 / $0.96   (MiniMax; cache-hit in $0.06)
    #   gemma4:31b         $0.30 / $0.90   (Gemma via Google)
    # Token counts per request come from Hermes' own state.db — real
    # per-model averages over the last 7 days (input/output/cache-read).
    # Cache-hit input is billed at the discounted rate by all four providers.
    API_PRICES = {
        "glm-5.2": (1.40, 4.40, 0.26),
        "glm-5.2:cloud": (1.40, 4.40, 0.26),
        "glm-5": (1.40, 4.40, 0.26),
        "deepseek-v4-flash:0731": (0.14, 0.28, 0.0028),
        "deepseek-v4-flash": (0.14, 0.28, 0.0028),
        "deepseek-v4-pro": (1.74, 3.48, 0.0036),
        "minimax-m3": (0.24, 0.96, 0.06),
        "gemma4:31b": (0.30, 0.90, 0.30),
        "kimi-k2.7-code": (0.95, 4.00, 0.19),
        "kimi-k2.6": (0.95, 4.00, 0.16),
        "gpt-5.5": (1.25, 10.00, 1.25),
    }

    token_avgs = _real_token_averages()

    # Fallback token averages for models with no state.db data: use the mean
    # across all known models (still far better than the old 1000/500 guess).
    fallback_avg = None
    if token_avgs:
        vals = list(token_avgs.values())
        fallback_avg = (
            round(sum(v[0] for v in vals) / len(vals)),
            round(sum(v[1] for v in vals) / len(vals)),
            round(sum(v[2] for v in vals) / len(vals)),
        )

    api_weekly_total = 0.0
    for seg in weekly_segs:
        prices = API_PRICES.get(seg["model"])
        if prices:
            p_in, p_out, p_cache = prices
            avg = token_avgs.get(seg["model"]) or fallback_avg
            if avg:
                in_t, out_t, cache_t = avg
                # Cache-aware: cache-read input at discounted rate, the rest
                # of the input at full rate, output at output rate.
                miss_in = max(in_t - cache_t, 0)
                per_req = (miss_in / 1e6) * p_in + (cache_t / 1e6) * p_cache + (out_t / 1e6) * p_out
            else:
                # No state.db data at all — old rough assumption as last resort.
                per_req = (1000 / 1e6) * p_in + (500 / 1e6) * p_out
            seg["api_cost_per_req"] = round(per_req, 6)
            seg["api_weekly_cost"] = round(per_req * seg["requests"], 4)
            api_weekly_total += seg["api_weekly_cost"]
            if per_req > 0:
                seg["api_cost_pct"] = round(seg["est_cost_per_req"] / per_req * 100.0, 1)
            else:
                seg["api_cost_pct"] = None
            # Cache + token volume info for new sections
            seg["avg_in_tokens"] = in_t if avg else None
            seg["avg_cache_tokens"] = cache_t if avg else None
            seg["avg_out_tokens"] = out_t if avg else None
            seg["cache_hit_pct"] = round(cache_t / in_t * 100.0, 1) if (avg and in_t > 0) else None
            seg["total_in_tokens"] = round(in_t * seg["requests"]) if avg else None
            seg["total_out_tokens"] = round(out_t * seg["requests"]) if avg else None
            # Cost split
            if avg:
                seg["api_input_cost"] = round((miss_in / 1e6) * p_in * seg["requests"], 4)
                seg["api_cache_cost"] = round((cache_t / 1e6) * p_cache * seg["requests"], 4)
                seg["api_output_cost"] = round((out_t / 1e6) * p_out * seg["requests"], 4)
        else:
            seg["api_cost_per_req"] = None
            seg["api_weekly_cost"] = None
            seg["api_cost_pct"] = None
            seg["cache_hit_pct"] = None
            seg["total_in_tokens"] = None
            seg["total_out_tokens"] = None
            seg["api_input_cost"] = None
            seg["api_cache_cost"] = None
            seg["api_output_cost"] = None
    result["api_weekly_total"] = round(api_weekly_total, 4)
    result["api_assumption"] = _api_assumption_text(token_avgs)
    result["api_total_pct"] = (
        round(cost_consumed / api_weekly_total * 100.0, 1) if api_weekly_total > 0 else None
    )
    # Savings + break-even + monthly projection
    result["api_savings"] = round(api_weekly_total - cost_consumed, 2) if api_weekly_total > 0 else None
    result["api_monthly_proj"] = round(api_weekly_total * 4.33, 2) if api_weekly_total > 0 else None
    result["ollama_monthly"] = 20.0  # Pro plan
    result["break_even_pct"] = (
        round(cost_consumed / api_weekly_total * 100.0, 1) if api_weekly_total > 0 else None
    )

    return result


def _real_token_averages() -> dict:
    """Real per-model avg tokens/request from Hermes state.db (all-time).

    Returns {model: (avg_input, avg_output, avg_cache_read)}. Empty dict when
    the DB is unavailable — callers fall back to the fixed assumption.
    """
    try:
        STATE_DB = Path.home() / ".hermes" / "state.db"
        if not STATE_DB.exists():
            return {}
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                """
                SELECT model,
                       ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                       ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                       ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                FROM sessions
                WHERE billing_provider = 'ollama-cloud'
                  AND api_call_count > 0
                  AND input_tokens > 0
                GROUP BY model
                """,
            ).fetchall()
            return {r[0]: (r[1], r[2], r[3]) for r in rows}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}


def _api_assumption_text(token_avgs: dict) -> str:
    """Human-readable note describing how the API comparison was computed."""
    if not token_avgs:
        return "~1000 in + 500 out tokens/req (fallback — no state.db data)"
    n = len(token_avgs)
    return f"real token averages from Hermes state.db (all-time, {n} models), cache-aware pricing"


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
    week = _week_key(data.get("weekly_reset_iso"))
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
                 "share_pct": m.get("share_pct"),
                 "est_cost_per_req": m.get("est_cost_per_req"),
                 "est_cost_per_req_pct": m.get("est_cost_per_req_pct")}
                for m in (data.get("weekly_models") or [])
            ],
        }
        kept.append(json.dumps(record))
        HISTORY_FILE.write_text("\n".join(kept) + "\n")
        # NOTE: no trimming here — the full log is the lifetime source.
        # Display slicing to HISTORY_MAX_WEEKS happens in _history().
    except OSError as e:
        logger.warning("Could not write history: %s", e)


def _record_session(data: dict) -> None:
    """Append one snapshot per 5h session window to the JSONL session file.

    Dedupes on the session reset ISO (the 5h window id) — keeps the LATEST
    snapshot per window, so each 5h session ends up with one row showing its
    peak usage.
    """
    if not data.get("ok") or data.get("session_used_pct") is None:
        return
    win = data.get("session_reset_iso")
    if not win:
        return
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        kept = []
        if SESSION_FILE.exists():
            for line in SESSION_FILE.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("window") != win:
                    kept.append(line)
        record = {
            "window": win,
            "ts": _utc_now().isoformat(timespec="seconds"),
            "session_used_pct": data.get("session_used_pct"),
            "weekly_used_pct": data.get("weekly_used_pct"),
            "session_models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct")}
                for m in (data.get("session_models") or [])
            ],
            "weekly_models": [
                {"model": m.get("model"), "requests": m.get("requests"),
                 "share_pct": m.get("share_pct")}
                for m in (data.get("weekly_models") or [])
            ],
        }
        kept.append(json.dumps(record))
        SESSION_FILE.write_text("\n".join(kept) + "\n")
        lines = SESSION_FILE.read_text().splitlines()
        if len(lines) > SESSION_LOG_CAP:
            SESSION_FILE.write_text("\n".join(lines[-SESSION_LOG_CAP:]) + "\n")
    except OSError as e:
        logger.warning("Could not write session log: %s", e)


def _generate_report() -> str:
    """Write the full stats MD report (weeks + all 5h sessions) to disk.

    Returns the report file path. The report is regenerated on every call
    from the two JSONL logs, so it always reflects everything captured.
    """
    weeks = []
    if HISTORY_FILE.exists():
        for line in HISTORY_FILE.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            weeks.append(rec)
    weeks.sort(key=lambda w: w.get("week") or "")

    sessions = []
    if SESSION_FILE.exists():
        for line in SESSION_FILE.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sessions.append(rec)
    sessions.sort(key=lambda s: s.get("ts") or "")

    lines = []
    lines.append("# Ollama Cloud Usage Stats")
    lines.append("")
    lines.append(f"_Generated {_utc_now().strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")
    lines.append("## Weekly overview")
    lines.append("")
    if weeks:
        lines.append("| Week | Weekly used | Est. cost | Top model | Requests |")
        lines.append("|------|-------------|-----------|-----------|----------|")
        for w in weeks:
            models = w.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0) if models else {}
            total = sum(m.get("requests") or 0 for m in models)
            cost = w.get("est_cost_consumed")
            cost_s = f"${cost:.2f}" if cost is not None else "?"
            lines.append(
                f"| {w.get('week')} | {w.get('weekly_used_pct')}% | {cost_s} | "
                f"{top.get('model') or '-'} | {total} |"
            )
    else:
        lines.append("_No weekly snapshots yet — the plugin records one per ISO week._")
    lines.append("")

    lines.append("## All 5h sessions")
    lines.append("")
    if sessions:
        for s in reversed(sessions[-50:]):  # newest 50 sessions
            ts = s.get("ts", "")[:16].replace("T", " ")
            lines.append(f"### {ts} — session {s.get('session_used_pct')}% · weekly {s.get('weekly_used_pct')}%")
            lines.append("")
            sm = s.get("session_models") or []
            wm = s.get("weekly_models") or []
            if sm:
                lines.append("Session per model:")
                lines.append("")
                lines.append("| Model | Requests | Share |")
                lines.append("|-------|----------|-------|")
                for m in sm:
                    lines.append(f"| {m.get('model')} | {m.get('requests')} | {m.get('share_pct')}% |")
                lines.append("")
            if wm:
                lines.append("Weekly per model:")
                lines.append("")
                lines.append("| Model | Requests | Share |")
                lines.append("|-------|----------|-------|")
                for m in wm:
                    lines.append(f"| {m.get('model')} | {m.get('requests')} | {m.get('share_pct')}% |")
                lines.append("")
    else:
        lines.append("_No session snapshots yet — recorded once per 5h window._")
        lines.append("")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines))
    return str(REPORT_FILE)


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


def _lifetime_stats() -> dict:
    """Aggregate ALL history records — lifetime totals and per-model
    lifetime average cost per request (in $ and % of weekly budget)."""
    if not HISTORY_FILE.exists():
        return {"ok": True, "weeks_count": 0, "models": []}

    weeks = []
    for line in HISTORY_FILE.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        weeks.append(rec)

    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}

    # Per-model lifetime aggregation: sum requests, and cost per request
    # weighted by requests (weighted mean of each week's est_cost_per_req).
    model_reqs: dict[str, int] = {}
    model_cost_wsum: dict[str, float] = {}
    for rec in weeks:
        for m in rec.get("models") or []:
            model = m.get("model")
            reqs = m.get("requests") or 0
            cost_per_req = m.get("est_cost_per_req")
            if not model or reqs <= 0:
                continue
            model_reqs[model] = model_reqs.get(model, 0) + reqs
            if cost_per_req is not None:
                model_cost_wsum[model] = model_cost_wsum.get(model, 0.0) + cost_per_req * reqs

    total_reqs = sum(model_reqs.values())
    total_cost = sum(model_cost_wsum.values())

    # Weekly budget baseline (Pro) for the % — same basis as the weekly calc.
    weekly_budget = 20.0 / 4.33

    models = []
    for model in model_reqs:
        reqs = model_reqs[model]
        cost_per_req = model_cost_wsum.get(model, 0.0) / reqs if reqs else 0.0
        models.append({
            "model": model,
            "requests": reqs,
            "est_cost_per_req": round(cost_per_req, 4),
            "est_cost_per_req_pct": round(cost_per_req / weekly_budget * 100.0, 4),
            "est_cost": round(model_cost_wsum.get(model, 0.0), 4),
        })
    models.sort(key=lambda m: m.get("requests") or 0, reverse=True)

    return {
        "ok": True,
        "weeks_count": len(weeks),
        "total_requests": total_reqs,
        "est_total_cost": round(total_cost, 4),
        "est_avg_cost_per_req": round(total_cost / total_reqs, 4) if total_reqs else 0.0,
        "est_avg_cost_per_req_pct": round((total_cost / total_reqs) / weekly_budget * 100.0, 4) if total_reqs else 0.0,
        "models": models,
    }


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
        _record_session(data)
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


@router.get("/usage/lifetime")
async def usage_lifetime():
    """Lifetime stats — all history records aggregated."""
    return _lifetime_stats()


@router.get("/usage/report")
async def usage_report():
    """Generate (or refresh) the full stats MD report; returns its path."""
    path = _generate_report()
    return {"ok": True, "path": path}


@router.post("/usage/report/open")
async def usage_report_open():
    """Generate the report and open it in the default app (macOS 'open')."""
    path = _generate_report()
    try:
        import subprocess as _sp
        _sp.Popen(["open", path])
        return {"ok": True, "path": path, "opened": True}
    except (OSError, FileNotFoundError):
        return {"ok": True, "path": path, "opened": False}
