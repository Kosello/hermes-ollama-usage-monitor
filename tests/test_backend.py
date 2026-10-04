"""Offline orchestration regressions; all persistence uses temporary profiles."""
import asyncio
from contextvars import ContextVar
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import sys
import unittest
from unittest.mock import patch
import urllib.error
from email.message import Message

ROOT = Path(__file__).resolve().parents[1]
fastapi = types.ModuleType("fastapi")
setattr(fastapi, "APIRouter", type("APIRouter", (), {"get": lambda self, path: lambda f: f, "post": lambda self, path: lambda f: f}))
sys.modules["fastapi"] = fastapi
pydantic = types.ModuleType("pydantic")
setattr(pydantic, "BaseModel", types.SimpleNamespace)
sys.modules["pydantic"] = pydantic
spec = importlib.util.spec_from_file_location("ollama_test_backend", ROOT / "backend/dashboard/plugin_api.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
HOME = ContextVar("test_home")


def payload(session=0.2, weekly=0.5):
    return {"limits": {"session": {"usage": session, "models": [{"name": "glm-5.2", "request_count": 2}]},
                       "weekly": {"usage": weekly, "models": [{"name": "glm-5.2", "request_count": 5}]}}}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ollama-backend-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "primary"
        self.home.mkdir()
        token = HOME.set(self.home)
        self.addCleanup(HOME.reset, token)
        patches = [
            patch.object(mod, "get_hermes_home", side_effect=HOME.get),
            patch.dict(os.environ, {"HERMES_HOME": str(self.home), "OLLAMA_PLAN": "", "OLLAMA_API_KEY": "", "OLLAMA_COOKIE_SOURCE": "file", "OLLAMA_KEYCHAIN_ACCOUNT": ""}),
            patch.object(mod.urllib.request, "urlopen", side_effect=AssertionError("network is forbidden")),
            patch.object(mod.subprocess, "run", side_effect=AssertionError("subprocess is forbidden")),
            patch.object(mod, "_resolve_api_prices", return_value=(mod._BUILTIN_PRICES, "builtin test prices")),
            patch.object(mod, "_real_token_averages", return_value={}),
            patch.object(mod, "_real_global_token_average", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        mod._usage_caches.clear()
        mod._price_caches.clear()
        self.html = (ROOT / "tests/fixtures/settings_page.html").read_text()

    def api_only(self, data):
        patches = [patch.object(mod, "_load_cookie", side_effect=FileNotFoundError()),
                   patch.object(mod, "_load_api_key", return_value="dummy offline key"),
                   patch.object(mod, "_fetch_usage_api", return_value=data)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_payloads_never_cache_or_persist_zero(self):
        bad = [None, [], {}, {"error": "denied"}, {"limits": {}},
               {"limits": {"session": {"usage": 0}}}]
        bad.extend(payload(v, 0.2) for v in [None, True, -0.1, 1.1, "0.5", float("nan"), float("inf")])
        bad.extend(payload(0.2, v) for v in [False, -1, 2, float("nan")])
        self.api_only({})
        with patch.object(mod, "_record_history") as history, patch.object(mod, "_record_session") as sessions:
            for data in bad:
                with self.subTest(data=data), patch.object(mod, "_fetch_usage_api", return_value=data):
                    result = mod._fetch_usage(force=True)
                    self.assertFalse(result["ok"])
                    self.assertNotIn("session_used_pct", result)
                    self.assertIsNone(mod._cache_for_profile()["data"])
                    json.dumps(result, allow_nan=False)
            history.assert_not_called()
            sessions.assert_not_called()

    def test_valid_zero_is_real_zero_and_api_is_not_quota_share(self):
        self.api_only(payload(0, 0))
        result = mod._fetch_usage()
        self.assertTrue(result["ok"])
        self.assertEqual(result["weekly_used_pct"], 0)
        self.assertFalse(result["stale"])
        self.assertEqual(result["share_basis"], "requests")
        self.assertTrue(result["reset_unavailable"])
        self.assertIsNone(result["weekly_models"][0]["plan_effective_per_1m"])
        json.dumps(result, allow_nan=False)

    def test_model_schema_and_request_counts_validated(self):
        for models in [None, {}, [None], [{"name": "x", "request_count": True}],
                       [{"name": "x", "request_count": -1}], [{"name": "x", "request_count": "3"}],
                       [{"name": "", "request_count": 1}]]:
            data = payload()
            data["limits"]["weekly"]["models"] = models
            with self.subTest(models=models), self.assertRaises(ValueError):
                mod._api_to_usage(data)

    def test_cache_preserves_original_fetch_time(self):
        self.api_only(payload())
        with patch.object(mod.time, "time", return_value=1000):
            first = mod._fetch_usage()
        with patch.object(mod.time, "time", return_value=1050), patch.object(mod, "_fetch_usage_api") as fetch:
            second = mod._fetch_usage()
        fetch.assert_not_called()
        self.assertTrue(second["cached"])
        self.assertEqual(first["fetched_at"], second["fetched_at"])

    def test_force_refresh_preserves_stale_on_failure_without_history_write(self):
        self.api_only(payload())
        first = mod._fetch_usage()
        with patch.object(mod, "_fetch_usage_api", side_effect=RuntimeError("SECRET should not escape")), \
                patch.object(mod, "_record_history") as history, patch.object(mod, "_record_session") as sessions:
            failed = asyncio.run(mod.usage_refresh())
            self.assertTrue(failed["stale"])
            self.assertEqual(failed["session_used_pct"], first["session_used_pct"])
            self.assertEqual(failed["fetched_at"], first["fetched_at"])
            self.assertNotIn("SECRET", json.dumps(failed))
            self.assertEqual(failed["source_errors"]["api"], "request failed")
            self.assertTrue(mod._fetch_usage()["stale"])
            history.assert_not_called()
            sessions.assert_not_called()

    def test_force_refresh_bypasses_fresh_cache_and_recovers(self):
        self.api_only(payload())
        first = mod._fetch_usage()
        with patch.object(mod, "_fetch_usage_api", return_value=payload(0.8, 0.9)) as fetch:
            refreshed = asyncio.run(mod.usage_refresh())
        fetch.assert_called_once()
        self.assertEqual(refreshed["weekly_used_pct"], 90)
        self.assertFalse(refreshed["stale"])
        self.assertNotEqual(first["weekly_used_pct"], refreshed["weekly_used_pct"])

    def test_cookie_first_falls_back_only_for_invalid_cookie(self):
        with patch.object(mod, "_load_cookie", return_value="dummy"), patch.object(mod, "_fetch_settings_page", return_value=self.html), \
                patch.object(mod, "_fetch_usage_api") as api:
            result = mod._fetch_usage()
            self.assertEqual(result["source"], "cookie")
            api.assert_not_called()
        mod._usage_caches.clear()
        self.api_only(payload())
        with patch.object(mod, "_load_cookie", return_value="dummy"), patch.object(mod, "_fetch_settings_page", return_value="<title>Sign in</title>"):
            self.assertEqual(mod._fetch_usage()["source"], "api")

    def test_manual_and_environment_plan_override_cookie_economics(self):
        with patch.object(mod, "_fetch_settings_page", side_effect=AssertionError("must not refetch HTML")):
            with patch.dict(os.environ, {"OLLAMA_PLAN": "max"}):
                data = mod._parse_usage(self.html)
                self.assertEqual(data["plan"], "Max")
                self.assertEqual(data["subscription_monthly_cost"], 100)
                self.assertEqual(asyncio.run(mod.usage_get_plan())["source"], "environment")
                (self.home / mod.PLAN_FILE).write_text("free\n")
                data = mod._parse_usage(self.html)
                self.assertEqual(data["plan"], "Free")
                self.assertEqual(data["subscription_monthly_cost"], 0)
                self.assertEqual(asyncio.run(mod.usage_get_plan())["source"], "config file")

    def test_profile_paths_credentials_plan_and_writes_are_isolated(self):
        second = Path(self.temp.name) / "secondary"
        second.mkdir()
        for home, plan in [(self.home, "pro"), (second, "max")]:
            (home / mod.PLAN_FILE).write_text(plan)
            (home / mod.COOKIE_FILE).write_text(plan + " cookie")
            (home / mod.API_KEY_FILE).write_text(plan + " key")
        for home, expected in [(self.home, "Pro"), (second, "Max"), (self.home, "Pro")]:
            HOME.set(home)
            self.assertEqual(mod._infer_plan(), expected)
            self.assertEqual(mod._load_cookie(), expected.lower() + " cookie")
            self.assertEqual(mod._load_api_key(), expected.lower() + " key")
            for name in [mod.STATE_DB, mod.HISTORY_FILE, mod.SESSION_FILE, mod.REPORT_FILE, mod.REPORTS_DIR, mod.PRICE_CACHE_FILE, mod.PRICE_OVERRIDE_FILE]:
                self.assertEqual(mod._profile_path(name).parent, home)
        HOME.set(second)
        self.assertTrue(asyncio.run(mod.usage_set_plan(mod._PlanBody(plan="free")))["ok"])
        self.assertEqual((second / mod.PLAN_FILE).read_text().strip(), "free")
        self.assertEqual((self.home / mod.PLAN_FILE).read_text().strip(), "pro")

    def test_actual_history_and_report_writes_stay_in_each_profile(self):
        second = Path(self.temp.name) / "secondary"
        second.mkdir()
        for home, tier in [(self.home, "pro"), (second, "max")]:
            HOME.set(home)
            (home / mod.PLAN_FILE).write_text(tier)
            with patch.object(mod, "_load_cookie", return_value="offline dummy"), patch.object(mod, "_fetch_settings_page", return_value=self.html):
                result = mod._fetch_usage(force=True)
            self.assertEqual(result["plan"], tier.capitalize())
            history = [json.loads(line) for line in (home / mod.HISTORY_FILE).read_text().splitlines()]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["plan"], tier.capitalize())
            report = Path(mod._generate_report())
            self.assertEqual(report.parent, home)
            self.assertTrue(report.is_file())
            self.assertTrue((home / mod.REPORTS_DIR / "lifetime.md").is_file())
        first_history = [json.loads(line) for line in (self.home / mod.HISTORY_FILE).read_text().splitlines()]
        self.assertEqual(first_history[0]["plan"], "Pro")

    def test_api_fallback_reuses_html_without_second_cookie_fetch(self):
        self.api_only(payload())
        html = self.html.replace("24.7%", "124.7%")
        with patch.object(mod, "_load_cookie", return_value="dummy"), patch.object(mod, "_fetch_settings_page", return_value=html) as fetch:
            result = mod._fetch_usage()
        self.assertEqual(result["source"], "api")
        self.assertEqual(result["plan"], "Pro")
        fetch.assert_called_once()

    def test_profile_usage_and_price_caches_are_isolated(self):
        first_cache = mod._cache_for_profile()
        first_cache["data"] = {"marker": "primary"}
        prices = mod._prices_for_profile()
        prices["prices"] = {"primary": 1}
        HOME.set(Path(self.temp.name) / "secondary")
        self.assertIsNone(mod._cache_for_profile()["data"])
        self.assertIsNone(mod._prices_for_profile()["prices"])
        HOME.set(self.home)
        self.assertIs(mod._cache_for_profile(), first_cache)
        self.assertIs(mod._prices_for_profile(), prices)

    def test_named_keychain_does_not_read_default_and_sharing_is_explicit(self):
        HOME.set(Path.home() / ".hermes")
        self.assertEqual(mod._keychain_account(), "ollama")
        HOME.set(self.home)
        first = mod._keychain_account()
        self.assertNotEqual(first, "ollama")
        HOME.set(Path(self.temp.name) / "secondary")
        self.assertNotEqual(first, mod._keychain_account())
        with patch.dict(os.environ, {"OLLAMA_KEYCHAIN_ACCOUNT": "shared"}):
            self.assertEqual(mod._keychain_account(), "shared")

    def test_failure_details_are_source_specific_and_redacted(self):
        self.api_only(payload())
        error = urllib.error.HTTPError("https://example.test/secret-token", 401, "SECRET", Message(), None)
        with patch.object(mod, "_fetch_usage_api", side_effect=error):
            result = mod._fetch_usage()
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], "cookie: not configured; api: HTTP 401")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))

    def test_cookie_out_of_range_cannot_write_history(self):
        html = self.html.replace("24.7%", "124.7%")
        with self.assertRaises(ValueError):
            mod._parse_usage(html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
