#!/usr/bin/env python3
"""Ollama cookie sync listener — OPTIONAL companion to ollama-usage-monitor.

Receives the __Secure-session cookie from the optional browser extension and
updates ~/.hermes/ollama_cookie.txt, so the plugin keeps working when the
session cookie rotates without a manual re-paste.

⚠️  THIS COMPONENT IS OPTIONAL. The plugin works fully without it — it only
    removes the rare manual re-paste (roughly every 2 months) by syncing the
    cookie from your browser.

Security:
  - binds 127.0.0.1 ONLY (loopback) — unreachable from the network
  - requires a Bearer token (generated on first run, printed once)
  - validates cookie shape (non-empty, ASCII, sane length)
  - never logs the cookie value — only length + timestamp

Run:  python3 ollama-cookie-sync-listener.py [--port 8765] [--token <t>]
"""

import argparse
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
TOKEN_FILE = Path.home() / ".hermes" / "ollama-cookie-sync-token.txt"
LAST_SYNC_FILE = Path.home() / ".hermes" / "ollama-cookie-sync-last.json"

MIN_COOKIE_LEN = 20
MAX_COOKIE_LEN = 4096


def load_token() -> str:
    """Read the pairing token, generating + printing one on first run."""
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    print("=" * 62)
    print("  NEW PAIRING TOKEN (paste into the extension options):")
    print(f"    {tok}")
    print("=" * 62)
    return tok


def write_cookie(value: str) -> bool:
    """Atomically replace the cookie file (temp + rename)."""
    if not (MIN_COOKIE_LEN <= len(value) <= MAX_COOKIE_LEN):
        return False
    if not all(32 <= ord(c) < 127 for c in value):
        return False
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIE_FILE.with_suffix(".tmp")
    tmp.write_text(value)
    os.replace(tmp, COOKIE_FILE)
    return True


def record_sync(ok: bool, source: str) -> None:
    try:
        LAST_SYNC_FILE.write_text(json.dumps({
            "ts": int(time.time()),
            "ok": ok,
            "source": source,
            "cookie_len": COOKIE_FILE.stat().st_size if COOKIE_FILE.exists() else 0,
        }))
    except OSError:
        pass


def make_handler(token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # quiet by default (base signature)
            pass

        def _auth_ok(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {token}"

        def do_GET(self):
            if self.path == "/health":
                self._json({"ok": True, "listener": "ollama-cookie-sync"})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/sync":
                self._json({"error": "not found"}, 404)
                return
            if not self._auth_ok():
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "bad json"}, 400)
                return
            cookie = payload.get("cookie", "")
            source = payload.get("source", "browser-extension")
            if write_cookie(cookie):
                record_sync(True, source)
                print(f"[{time.strftime('%H:%M:%S')}] cookie synced "
                      f"({len(cookie)} chars, source={source})")
                self._json({"ok": True, "written": len(cookie)})
            else:
                record_sync(False, source)
                print(f"[{time.strftime('%H:%M:%S')}] REJECTED cookie "
                      f"({len(cookie)} chars, source={source})")
                self._json({"error": "invalid cookie"}, 400)

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama cookie sync listener (optional)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None, help="pairing token (default: read/generate file)")
    args = parser.parse_args()

    token = args.token or load_token()
    server = ThreadingHTTPServer((HOST, args.port), make_handler(token))
    print(f"Listening on http://{HOST}:{args.port} (ctrl-C to stop)")
    print(f"Cookie file: {COOKIE_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
