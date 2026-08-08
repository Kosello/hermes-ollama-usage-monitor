#!/usr/bin/env python3
"""
Ollama Cloud usage watchdog — silent unless a threshold is crossed.

Runs every 30 min via no_agent cron (watchdog pattern):
  - fetches fresh usage from ollama.com/settings
  - records a weekly history snapshot (same JSONL as the backend)
  - fires a macOS notification + stdout line when weekly usage crosses
    75% (warning) or 90% (critical) — once per threshold per week

STDOUT CONTRACT: empty when nothing to report (silent tick), a short
alert line when a threshold just crossed. Non-zero exit = error alert.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "ollama-usage-monitor"
HISTORY_FILE = Path.home() / ".hermes" / "ollama-usage-history.jsonl"
STATE_FILE = Path.home() / ".hermes" / "ollama-alert-state.json"
COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15

WARN_PCT = 75.0
CRIT_PCT = 90.0


def _load_cookie() -> str:
    # Keychain first, then file — same order as the plugin backend.
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "hermes-ollama-cookie",
             "-a", "ollama", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    if not COOKIE_FILE.exists():
        raise FileNotFoundError(f"No cookie in Keychain or {COOKIE_FILE}")
    cookie = COOKIE_FILE.read_text().strip()
    if not cookie:
        raise ValueError("Cookie file is empty")
    return cookie


def _fetch_weekly_pct() -> float:
    """Fresh fetch — minimal parse of the settings page (weekly % only)."""
    cookie = _load_cookie()
    req = urllib.request.Request(
        SETTINGS_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    weekly = re.search(r"Weekly usage\s+([0-9.]+)%", html)
    if not weekly:
        raise ValueError("Could not parse weekly usage from settings page")
    return float(weekly.group(1))


def _record_history(weekly_pct: float) -> None:
    """Record a weekly snapshot (dedupe per ISO week, keep latest)."""
    # Week key from today — the cron guarantees one snapshot per week even if
    # the backend never fetched; dedupe keeps only the newest value per week.
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).date()
    week = monday.isoformat()

    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
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
            "ts": now.isoformat(timespec="seconds"),
            "weekly_used_pct": weekly_pct,
            "session_used_pct": None,
            "est_cost_consumed": None,
            "est_weekly_budget": None,
            "models": [],
            "source": "cron",
        }
        kept.append(json.dumps(record))
        HISTORY_FILE.write_text("\n".join(kept) + "\n")
        lines = HISTORY_FILE.read_text().splitlines()
        if len(lines) > 8:
            HISTORY_FILE.write_text("\n".join(lines[-8:]) + "\n")
    except OSError:
        pass  # history is best-effort


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass  # no osascript (non-macOS) — stdout line still delivers


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(st))
    except OSError:
        pass


def main() -> int:
    weekly = _fetch_weekly_pct()
    _record_history(weekly)

    now = datetime.now(timezone.utc)
    monday = (now - __import__("datetime").timedelta(days=now.weekday())).date()
    week = monday.isoformat()

    st = _state()
    fired_crit = st.get("crit") == week
    fired_warn = st.get("warn") == week

    if weekly >= CRIT_PCT and not fired_crit:
        _notify("Ollama Cloud CRITICAL",
                f"Weekly usage at {weekly:.1f}% — over {CRIT_PCT:.0f}%!")
        st["crit"] = week
        _save_state(st)
        print(f"⚠️ Ollama Cloud: weekly usage {weekly:.1f}% — CRITICAL (>{CRIT_PCT:.0f}%)")
        return 0
    if weekly >= WARN_PCT and not fired_warn:
        _notify("Ollama Cloud warning",
                f"Weekly usage at {weekly:.1f}% — over {WARN_PCT:.0f}%")
        st["warn"] = week
        _save_state(st)
        print(f"⚠️ Ollama Cloud: weekly usage {weekly:.1f}% — warning (>{WARN_PCT:.0f}%)")
        return 0

    # Nothing to report — stay silent (watchdog pattern)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Error alert — always report failures loudly
        print(f"❌ Ollama usage watchdog error: {e}", file=sys.stderr)
        sys.exit(1)
