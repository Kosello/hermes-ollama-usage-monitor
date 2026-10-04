"""Cron safety and stdout-contract tests; no credentials/network/notifications."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]


class CronTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ollama-cron-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "OLLAMA_COOKIE_SOURCE": "file", "OLLAMA_KEYCHAIN_ACCOUNT": ""})
        p.start()
        self.addCleanup(p.stop)
        self.modules = {}
        for name in ["daily", "watch"]:
            spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/ollama_usage_{name}.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.modules[name] = module
        self.watch = self.modules["watch"]
        self.daily = self.modules["daily"]

    def test_profile_paths_and_keychain_scope(self):
        for module in self.modules.values():
            self.assertEqual(module.HISTORY_FILE.parent, self.home)
            self.assertEqual(module.COOKIE_FILE.parent, self.home)
            self.assertNotEqual(module._keychain_account(), "ollama")
        self.assertEqual(self.watch.STATE_FILE.parent, self.home)
        self.assertEqual(self.watch._keychain_account(), self.daily._keychain_account())
        with patch.dict(os.environ, {"OLLAMA_KEYCHAIN_ACCOUNT": "explicit shared"}):
            self.assertEqual(self.watch._keychain_account(), "explicit shared")

    def test_file_source_never_probes_keychain(self):
        (self.home / "ollama_cookie.txt").write_text("offline dummy")
        for module in self.modules.values():
            with patch.object(module.subprocess, "run", side_effect=AssertionError("not allowed")):
                self.assertEqual(module._load_cookie(), "offline dummy")

    def test_watchdog_does_not_trim_full_history(self):
        records = [{"week": f"2000-01-{n:02d}", "models": []} for n in range(1, 13)]
        self.watch.HISTORY_FILE.write_text("\n".join(map(json.dumps, records)) + "\n")
        self.watch._record_history(25)
        stored = [json.loads(s) for s in self.watch.HISTORY_FILE.read_text().splitlines()]
        self.assertEqual(len(stored), 13)
        self.assertEqual(stored[:12], records)

    def test_watchdog_does_not_replace_rich_model_snapshot(self):
        now = datetime.now(timezone.utc)
        week = (now - timedelta(days=now.weekday())).date().isoformat()
        record = {"week": week, "models": [{"model": "example", "requests": 200}], "weekly_used_pct": 44, "source": "cookie"}
        original = json.dumps(record) + "\n"
        self.watch.HISTORY_FILE.write_text(original)
        self.watch._record_history(50)
        self.assertEqual(self.watch.HISTORY_FILE.read_text(), original)

    def test_silent_tick_and_once_per_week_notification(self):
        with patch.object(self.watch, "_fetch_weekly_pct", return_value=20), patch.object(self.watch, "_notify") as notify, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.watch.main(), 0)
            self.assertEqual(out.getvalue(), "")
            notify.assert_not_called()
        with patch.object(self.watch, "_fetch_weekly_pct", return_value=95), patch.object(self.watch, "_notify") as notify, contextlib.redirect_stdout(io.StringIO()) as out:
            self.watch.main()
            first = out.getvalue()
            self.assertIn("CRITICAL", first)
            out.truncate(0)
            out.seek(0)
            self.watch.main()
            # After critical, the existing design can still send the warning once.
            out.truncate(0)
            out.seek(0)
            self.watch.main()
            self.assertEqual(out.getvalue(), "")
            self.assertLessEqual(notify.call_count, 2)

    def test_invalid_quota_is_rejected_before_daily_output(self):
        response = contextlib.nullcontext(io.BytesIO(b"Weekly usage 140% Session usage 10%"))
        with patch.object(self.daily, "_load_cookie", return_value="dummy"), patch.object(self.daily.urllib.request, "urlopen", return_value=response), self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()) as out:
            self.daily.main()
        self.assertEqual(out.getvalue(), "")

    def test_daily_returns_one_line(self):
        response = contextlib.nullcontext(io.BytesIO(b"Weekly usage 44.7% Session usage 81.6%"))
        with patch.object(self.daily, "_load_cookie", return_value="dummy"), patch.object(self.daily.urllib.request, "urlopen", return_value=response), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.daily.main(), 0)
        self.assertEqual(out.getvalue().splitlines(), ["📊 Ollama Cloud: weekly 44.7% · session 81.6%"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
