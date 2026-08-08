#!/usr/bin/env python3
"""
Ollama Cloud daily usage line — prints ONE short summary line (no_agent cron).

Reuses the watchdog's fetch logic. Output is delivered verbatim to the chat.
"""
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
HISTORY_FILE = Path.home() / ".hermes" / "ollama-usage-history.jsonl"
SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15


def _load_cookie() -> str:
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


def main() -> int:
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
    session = re.search(r"Session usage\s+([0-9.]+)%", html)
    if not weekly:
        raise ValueError("Could not parse weekly usage")

    w = float(weekly.group(1))
    s = float(session.group(1)) if session else None

    # Top model from the latest history record (best effort)
    top = None
    try:
        lines = HISTORY_FILE.read_text().splitlines()
        if lines:
            rec = json.loads(lines[-1])
            models = rec.get("models") or []
            if models:
                top = max(models, key=lambda m: m.get("share_pct") or 0).get("model")
    except (OSError, json.JSONDecodeError):
        pass

    parts = [f"📊 Ollama Cloud: weekly {w:.1f}%"]
    if s is not None:
        parts.append(f"session {s:.1f}%")
    if top:
        parts.append(f"top: {top}")
    print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"❌ Ollama daily usage error: {e}", file=sys.stderr)
        sys.exit(1)
