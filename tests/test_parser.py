#!/usr/bin/env python3
"""Smoke test for the Ollama usage parser — runs on CI without a real cookie.

Loads a saved HTML fixture and asserts the parser extracts:
  - plan tier
  - session / weekly usage %
  - per-model segments with requests + share (session and weekly)
  - cost estimates
"""
import importlib.util
import sys
import types
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "settings_page.html"
BACKEND = HERE.parent / "backend" / "dashboard" / "plugin_api.py"

# plugin_api imports fastapi for the router — stub it so the parser can be
# tested without installing fastapi (works in CI too).
_fastapi = types.ModuleType("fastapi")
_RouterStub = type("APIRouter", (), {"get": lambda self, path: (lambda f: f)})
_fastapi.APIRouter = _RouterStub
sys.modules["fastapi"] = _fastapi

spec = importlib.util.spec_from_file_location("plugin_api", BACKEND)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main() -> int:
    html = FIXTURE.read_text()
    data = mod._parse_usage(html)

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

    # Cost estimates present and sane
    assert data["est_weekly_budget"] is not None
    assert data["est_cost_consumed"] > 0
    assert by["glm-5.2"]["est_cost_per_req"] > 0
    assert by["glm-5.2"]["est_cost_per_req_pct"] > 0

    # Real API price comparison (fixture: glm-5.2, deepseek-v4-flash:0731, minimax-m3)
    assert data["api_weekly_total"] > 0, data["api_weekly_total"]
    assert by["glm-5.2"]["api_cost_per_req"] == 0.0036, by["glm-5.2"]["api_cost_per_req"]
    assert abs(by["glm-5.2"]["api_weekly_cost"] - 1.9944) < 0.001, by["glm-5.2"]["api_weekly_cost"]
    assert by["minimax-m3"]["api_cost_per_req"] == 0.00072, by["minimax-m3"]["api_cost_per_req"]
    assert data["api_assumption"] == "~1000 in + 500 out tokens/req", data["api_assumption"]

    print("✅ parser smoke test passed")
    print(f"   plan={data['plan']} session={data['session_used_pct']}% weekly={data['weekly_used_pct']}%")
    print(f"   session models={len(s)} weekly models={len(w)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"❌ smoke test failed: {e}", file=sys.stderr)
        sys.exit(1)
