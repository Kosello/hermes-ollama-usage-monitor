#!/usr/bin/env python3
"""Smoke test for the Ollama usage parser — runs on CI without a real cookie.

Loads a saved HTML fixture and asserts the parser extracts:
  - plan tier
  - session / weekly usage %
  - per-model segments with requests + share (session and weekly)
  - cost estimates
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "settings_page.html"
BACKEND = HERE.parent / "backend" / "dashboard" / "plugin_api.py"

# plugin_api imports fastapi for the router — stub it so the parser can be
# tested without installing fastapi (works in CI too).
_fastapi = types.ModuleType("fastapi")
_RouterStub = type("APIRouter", (), {
    "get": lambda self, path: (lambda f: f),
    "post": lambda self, path: (lambda f: f),
})
_fastapi.APIRouter = _RouterStub
sys.modules["fastapi"] = _fastapi
_pydantic = types.ModuleType("pydantic")
_pydantic.BaseModel = type("BaseModel", (), {})
sys.modules["pydantic"] = _pydantic

# plugin_api also imports pydantic for request bodies; parser tests don't
# exercise those endpoints, so provide a minimal stub for CI environments
# where pydantic is not installed.
_pydantic = types.ModuleType("pydantic")
_pydantic.BaseModel = object
sys.modules["pydantic"] = _pydantic

spec = importlib.util.spec_from_file_location("plugin_api", BACKEND)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main() -> int:
    html = FIXTURE.read_text()
    # Deterministic: stub out state.db so the 1000/500 fallback is exercised
    # regardless of the machine running the test (CI has no state.db), and
    # stub the price chain so no network call happens (CI has no network).
    real_token_averages = mod._real_token_averages
    real_global_token_average = mod._real_global_token_average
    mod._real_token_averages = lambda: {}
    mod._real_global_token_average = lambda: None
    mod._resolve_api_prices = lambda: (mod._BUILTIN_PRICES, "builtin defaults")
    data = mod._parse_usage(html)

    assert data["source"] == "cookie"
    assert data["share_basis"] == "ollama_usage_bar"
    assert data["plan"] == "Pro", f"plan: {data['plan']}"
    assert data["session_used_pct"] == 24.7, data["session_used_pct"]
    assert data["weekly_used_pct"] == 41.1, data["weekly_used_pct"]
    import re as _re
    # Relative format: 'now' | '<n> min' | '<n>h' | '<n> day(s)'
    assert _re.fullmatch(r"(now|\d+ min|\d+h|\d+ days?|2026-08-08T13:00:00Z)", data["session_reset"]), data["session_reset"]
    assert data["session_reset"] != "2026-08-08T13:00:00Z", "session_reset should be relative, not raw ISO"
    assert data["session_reset_iso"] == "2026-08-08T13:00:00Z", data["session_reset_iso"]
    assert data["weekly_reset_iso"] == "2026-08-10T13:00:00Z", data["weekly_reset_iso"]

    s = data["session_models"]
    w = data["weekly_models"]
    assert len(s) == 2, f"session segs: {s}"
    assert len(w) == 3, f"weekly segs: {w}"

    by = {m["model"]: m for m in w}
    assert by["glm-5.2"]["requests"] == 554
    assert abs(by["glm-5.2"]["share_pct"] - 95.2) < 0.01
    assert by["deepseek-v4-flash:0731"]["requests"] == 240
    assert by["minimax-m3"]["requests"] == 12

    # Fixed subscription economics: quota % is never treated as money spent.
    assert abs(data["subscription_monthly_cost"] - 20.0) < 0.001
    assert abs(data["subscription_weekly_equivalent"] - 4.5997) < 0.001
    assert data["effective_subscription_cost_per_req"] > 0
    assert by["glm-5.2"]["est_cost_per_req_pct"] is None

    # Real API price comparison (fixture: glm-5.2, deepseek-v4-flash:0731, minimax-m3)
    assert data["api_window_total"] > 0, data["api_window_total"]
    assert by["glm-5.2"]["api_cost_per_req"] == 0.00018, by["glm-5.2"]["api_cost_per_req"]
    assert by["glm-5.2"]["api_effective_per_1m"] == 0.12, by["glm-5.2"]["api_effective_per_1m"]
    assert abs(by["glm-5.2"]["api_weekly_cost"] - 0.09972) < 0.001, by["glm-5.2"]["api_weekly_cost"]
    assert by["minimax-m3"]["api_cost_per_req"] == 0.0009, by["minimax-m3"]["api_cost_per_req"]
    assert "fixed fallback" in data["api_assumption"], data["api_assumption"]
    # The API-dollar fields remain separate from Ollama's quota allocation.
    assert by["glm-5.2"]["api_cost_pct"] is None
    assert data["api_total_pct"] is not None and data["api_total_pct"] > 0, data["api_total_pct"]
    assert data["api_monthly_proj"] is None

    # Cookie usage-bar widths drive per-model Ollama $/1M. The fixture has
    # 95.2% of the weekly bar assigned to glm and only 4.1% to flash despite
    # their request counts being closer, so their Ollama rates must diverge.
    priced_models = [m for m in data["weekly_models"] if m.get("api_effective_per_1m") is not None]
    assert len(priced_models) == len(data["weekly_models"]), "all fixture models should be priced"
    for m in priced_models:
        assert m["plan_effective_per_1m"] is not None
        assert m["plan_pct_of_api"] is not None
        assert m["plan_pct_of_api"] > 0, m["plan_pct_of_api"]
    assert by["glm-5.2"]["plan_effective_per_1m"] > by["deepseek-v4-flash:0731"]["plan_effective_per_1m"] * 5
    # The model allocations must reconcile to: weekly_equivalent × weekly_used_fraction
    allocated = sum(
        m["plan_effective_per_1m"] / 1e6
        * (m["avg_uncached_input_tokens"] + m["avg_cache_tokens"] + m["avg_out_tokens"])
        * m["requests"]
        for m in data["weekly_models"]
    )
    expected_alloc = data["subscription_weekly_equivalent"] * (data["weekly_used_pct"] / 100.0)
    assert abs(allocated - expected_alloc) < 0.0001, (allocated, expected_alloc)

    # Official API exposes no reset timestamp. Activity period end is not a
    # quota reset and must never be extrapolated from usage percentage.
    api_data = mod._api_to_usage({
        "activity": {"period": {"ending_at": "2026-08-09T17:30:58.790970649Z"}},
        "limits": {
            "session": {"usage": 0.2, "models": []},
            "weekly": {"usage": 0.4, "models": [
                {"name": "glm-5.2", "request_count": 10},
            ]},
        },
    })
    assert api_data["reset_unavailable"] is True
    assert api_data["reset_estimated"] is False
    assert api_data["session_reset_iso"] is None
    assert api_data["weekly_reset_iso"] is None
    assert api_data["source"] == "api"
    assert api_data["weekly_models"][0]["plan_effective_per_1m"] is None
    assert api_data["weekly_models"][0]["plan_pct_of_api"] is None

    # Free tier must not divide by zero.
    free = {"plan": "Free", "source": "cookie", "weekly_used_pct": 50.0,
            "weekly_models": [{"model": "gpt-oss:120b", "requests": 2, "share_pct": 100.0}]}
    mod._enrich_with_costs(free)
    assert free["subscription_weekly_equivalent"] == 0.0
    assert free["effective_subscription_cost_per_req"] == 0.0
    assert free["weekly_models"][0]["plan_effective_per_1m"] == 0.0
    assert free["weekly_models"][0]["plan_pct_of_api"] == 0.0
    # A missing published cache rate falls back to the input rate, never free cache.
    assert free["weekly_models"][0]["api_cache_price_published"] is False
    assert free["weekly_models"][0]["api_cache_per_1m"] == free["weekly_models"][0]["api_input_per_1m"]

    # Canonical Hermes usage keeps uncached input and cache-read input in
    # separate buckets. Do not subtract cache from input a second time.
    mod._resolve_api_prices = lambda: ({"gpt-oss:120b": (0.037, 0.17, None)}, "test")
    mod._resolve_token_averages = lambda overrides: ({"gpt-oss:120b": (1000, 500, 500)}, "test")
    cached = {"plan": "Pro", "weekly_used_pct": 1.0,
              "weekly_models": [{"model": "gpt-oss:120b", "requests": 2, "share_pct": 100.0}]}
    mod._enrich_with_costs(cached)
    cm = cached["weekly_models"][0]
    assert abs(cm["api_cost_per_req"] - 0.0001405) < 0.000001, cm["api_cost_per_req"]
    assert cm["api_effective_per_1m"] == 0.07025, cm["api_effective_per_1m"]
    assert cm["cache_hit_pct"] == 33.3, cm["cache_hit_pct"]
    assert cm["total_prompt_tokens"] == 3000

    # Partial pricing coverage must not be presented as a complete total or
    # feed savings/break-even calculations.
    mod._resolve_api_prices = lambda: ({"priced": (1.0, 1.0, 1.0)}, "test")
    mod._resolve_token_averages = lambda overrides: ({}, "fallback")
    partial = {"plan": "Pro", "source": "cookie", "weekly_used_pct": 1.0, "weekly_models": [
        {"model": "priced", "requests": 1, "share_pct": 50.0},
        {"model": "unpriced", "requests": 1, "share_pct": 50.0},
    ]}
    mod._enrich_with_costs(partial)
    assert partial["api_price_coverage_pct"] == 50.0
    assert partial["api_known_window_total"] is not None
    assert partial["api_window_total"] is None
    assert partial["api_total_pct"] is None
    # Ollama $/1M depends on plan allocation + tokens, not API price coverage.
    unpriced = next(m for m in partial["weekly_models"] if m["model"] == "unpriced")
    assert unpriced["plan_effective_per_1m"] is not None
    assert unpriced["plan_pct_of_api"] is None

    # Documented dictionary overrides normalize to numeric tuples.
    with tempfile.TemporaryDirectory() as td:
        override_path = Path(td) / "prices.json"
        override_path.write_text(json.dumps({"models": {
            "demo": {"input": 1.0, "output": 2.0, "cache_read": 0.25}
        }}))
        old_override = mod.PRICE_OVERRIDE_FILE
        mod.PRICE_OVERRIDE_FILE = override_path
        try:
            assert mod._load_manual_price_overrides()["prices"]["demo"] == (1.0, 2.0, 0.25)
        finally:
            mod.PRICE_OVERRIDE_FILE = old_override

    # Modern per-model accounting wins over legacy session attribution.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        db_dir = home / ".hermes"
        db_dir.mkdir()
        conn = sqlite3.connect(db_dir / "state.db")
        conn.executescript("""
            CREATE TABLE session_model_usage (
              model TEXT, billing_provider TEXT, api_call_count INTEGER,
              input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER
            );
            CREATE TABLE sessions (
              model TEXT, billing_provider TEXT, api_call_count INTEGER,
              input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER
            );
            INSERT INTO session_model_usage VALUES ('modern', 'ollama-cloud', 2, 200, 40, 600);
            INSERT INTO sessions VALUES ('legacy', 'ollama-cloud', 1, 9999, 9999, 9999);
        """)
        conn.commit()
        conn.close()
        real_state_db = mod.STATE_DB
        mod.STATE_DB = db_dir / "state.db"
        try:
            avgs = real_token_averages()
            assert avgs == {"modern": (100.0, 20.0, 300.0)}, avgs
            assert real_global_token_average() == (100.0, 20.0, 300.0)
        finally:
            mod.STATE_DB = real_state_db

    print("✅ parser smoke test passed")
    print(f"   plan={data['plan']} session={data['session_used_pct']}% weekly={data['weekly_used_pct']}%")
    print(f"   session models={len(s)} weekly models={len(w)}")
    return 0


def test_deepseek_official_pricing() -> None:
    """The official DeepSeek-V4 peak/off-peak table must match the announcement."""
    # Peak hours: 01:00–04:00 and 06:00–10:00 UTC.
    assert mod._DEEPSEEK_PEAK_WINDOWS == ((1, 4), (6, 10))
    assert mod._DEEPSEEK_OFFICIAL_PEAK == {
        "deepseek-v4-flash": (0.014, 0.44, 1.32),
        "deepseek-v4-pro": (0.044, 1.32, 3.96),
    }
    assert mod._DEEPSEEK_OFFICIAL_OFFPEAK == {
        "deepseek-v4-flash": (0.007, 0.22, 0.66),
        "deepseek-v4-pro": (0.022, 0.66, 1.98),
    }
    # Returned tuple is (input=cache-miss, output, cached_input=cache-hit).
    real_peak_fn = mod._is_deepseek_peak_now
    real_ts = mod._DEEPSEEK_PRICING_EFFECTIVE_TS
    mod._DEEPSEEK_PRICING_EFFECTIVE_TS = 0  # force effective
    try:
        mod._is_deepseek_peak_now = lambda: True
        assert mod._deepseek_official_price("deepseek-v4-pro") == (1.32, 3.96, 0.044)
        assert mod._deepseek_official_price("deepseek-v4-flash:0731") == (0.44, 1.32, 0.014)
        mod._is_deepseek_peak_now = lambda: False
        assert mod._deepseek_official_price("deepseek-v4-pro") == (0.66, 1.98, 0.022)
        assert mod._deepseek_official_price("deepseek-v4-flash") == (0.22, 0.66, 0.007)
        # Non-DeepSeek models are untouched.
        assert mod._deepseek_official_price("glm-5.2") is None
    finally:
        mod._is_deepseek_peak_now = real_peak_fn
        mod._DEEPSEEK_PRICING_EFFECTIVE_TS = real_ts
    print("✅ deepseek official pricing test passed")


if __name__ == "__main__":
    try:
        main_ok = main() == 0
        pricing_ok = test_deepseek_official_pricing() is not False
        sys.exit(0 if (main_ok and pricing_ok) else 1)
    except AssertionError as e:
        print(f"❌ smoke test failed: {e}", file=sys.stderr)
        sys.exit(1)
