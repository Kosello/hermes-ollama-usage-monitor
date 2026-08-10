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
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
KEYCHAIN_SERVICE = "hermes-ollama-cookie"
KEYCHAIN_ACCOUNT = "ollama"
SETTINGS_URL = "https://ollama.com/settings"
API_USAGE_URL = "https://ollama.com/api/usage"
API_KEY_FILE = Path.home() / ".hermes" / "ollama_api_key.txt"
API_KEY_SOURCE = os.environ.get("OLLAMA_API_KEY_SOURCE", "file")
TIMEOUT = 15
CACHE_TTL_SECONDS = 60
HISTORY_FILE = Path.home() / ".hermes" / "ollama-usage-history.jsonl"
HISTORY_MAX_WEEKS = 8
SESSION_FILE = Path.home() / ".hermes" / "ollama-usage-sessions.jsonl"
REPORT_FILE = Path.home() / ".hermes" / "ollama-usage-report.md"
REPORTS_DIR = Path.home() / ".hermes" / "ollama-usage-reports"
SESSION_LOG_CAP = 500

# ── Price / token fallback chain ──────────────────────────────────────────
# Priority: manual override file  →  live OpenRouter (24h cache)  →  builtin.
PRICE_OVERRIDE_FILE = Path.home() / ".hermes" / "ollama-usage-prices.json"
PRICE_CACHE_FILE = Path.home() / ".hermes" / "ollama-usage-price-cache.json"
STATE_DB = Path.home() / ".hermes" / "state.db"
PRICE_CACHE_TTL_SECONDS = 24 * 3600
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_cache: dict = {"ts": 0, "data": None}
_prices_cache: dict = {"ts": 0, "prices": None, "source": None}


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


# ── Official API (primary source; cookie scrape is the fallback) ────────────

def _load_api_key() -> str | None:
    """Read the official API key — env first, then the file."""
    env_key = os.environ.get("OLLAMA_API_KEY")
    if env_key:
        return env_key
    if API_KEY_SOURCE == "file" and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text().strip()
        if key and not key.startswith("__Secure-session"):
            return key
    return None


def _fetch_usage_api(api_key: str) -> dict:
    """GET https://ollama.com/api/usage — no cookie, no HTML parsing."""
    req = urllib.request.Request(
        API_USAGE_URL,
        headers={"Authorization": api_key, "User-Agent": "hermes-ollama-usage-monitor/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


PLAN_FILE = Path.home() / ".hermes" / "ollama-usage-plan.txt"
VALID_PLANS = ("free", "pro", "max")
PLAN_MONTHLY_USD = {"free": 0.0, "pro": 20.0, "max": 100.0}
WEEKS_PER_MONTH = 365.2425 / 12 / 7  # calendar-average 4.348 weeks/month


def _infer_plan() -> str | None:
    """Resolve the Ollama Cloud plan tier.

    1. config file ~/.hermes/ollama-usage-plan.txt (user's manual choice)
    2. env OLLAMA_PLAN
    3. cookie scrape of settings page (if cookie available)
    4. None (caller defaults to Pro budget)
    """
    # 1. Manual config file — highest priority, works without cookie/browser.
    try:
        if PLAN_FILE.exists():
            val = PLAN_FILE.read_text().strip().lower()
            if val in VALID_PLANS:
                return val.capitalize()
    except OSError:
        pass

    # 2. Environment variable.
    env_plan = os.environ.get("OLLAMA_PLAN", "").strip().lower()
    if env_plan in VALID_PLANS:
        return env_plan.capitalize()

    # 3. Cookie scrape — only for the plan label.
    try:
        cookie = _load_cookie()
        html = _fetch_settings_page(cookie)
        m = re.search(r'Cloud usage</span>\s*\n?\s*<span[^>]*>\s*(pro|free|max)\s*<',
                      html, re.IGNORECASE)
        if m:
            return m.group(1).capitalize()
    except Exception:
        pass

    return None


def _api_to_usage(api_data: dict) -> dict:
    """Convert the official API response into the internal usage dict shape.

    The API gives us session/weekly usage as 0–1 floats and per-model request
    counts, but no plan tier, exact reset timestamps, or per-model quota usage.
    We compute *request share* from request counts. Reset times are only rough
    linear-use estimates; the UI labels them as estimates. Then both API and
    cookie paths run the same cost enrichment.
    """
    limits = api_data.get("limits", {})
    session = limits.get("session", {})
    weekly = limits.get("weekly", {})

    def _models(block: dict) -> list:
        models = [
            {
                "model": m.get("name", "?"),
                "requests": m.get("request_count", 0),
                "share_pct": None,
            }
            for m in block.get("models", [])
        ]
        total = sum(m["requests"] for m in models)
        if total > 0:
            for m in models:
                m["share_pct"] = round(m["requests"] / total * 100, 1)
        return models

    session_models = _models(session)
    weekly_models = _models(weekly)

    session_usage = session.get("usage", 0)
    weekly_usage = weekly.get("usage", 0)

    data = {
        "plan": _infer_plan(),  # API doesn't expose plan tier; infer from cookie/env
        "session_used_pct": round(session_usage * 100, 1),
        "weekly_used_pct": round(weekly_usage * 100, 1),
        "session_reset": None,
        "weekly_reset": None,
        "session_reset_iso": None,
        "weekly_reset_iso": None,
        "session_models": session_models,
        "weekly_models": weekly_models,
        "models": session_models,  # backward-compat
        "source": "api",
        "share_basis": "requests",
        "reset_estimated": False,
        "reset_unavailable": True,
    }

    # Run the same cost enrichment as the cookie path.
    _enrich_with_costs(data)
    return data


def _load_manual_price_overrides() -> dict:
    """Read manual price/token overrides from ~/.hermes/ollama-usage-prices.json.

    Format (all optional):
    {
      "models": {
        "glm-5.2": {"input": 1.40, "output": 4.40, "cache_read": 0.26},
        "deepseek-v4-flash": {"input": 0.14, "output": 0.28, "cache_read": 0.0028}
      },
      "tokens_per_request": {
        "glm-5.2": [100000, 3000, 20000]   # [in, out, cache_read]
      }
    }
    Returns {"prices": {...}, "tokens": {...}}.
    """
    result = {"prices": {}, "tokens": {}}
    try:
        if not PRICE_OVERRIDE_FILE.exists():
            return result
        data = json.loads(PRICE_OVERRIDE_FILE.read_text())

        # Normalize the documented object form to the tuple used internally.
        # Also accept a 3-item list/tuple for backward compatibility.
        for model, value in (data.get("models", {}) or {}).items():
            try:
                if isinstance(value, dict):
                    cache = value.get("cache_read")
                    result["prices"][model] = (
                        float(value["input"]),
                        float(value["output"]),
                        float(cache) if cache is not None else None,
                    )
                else:
                    result["prices"][model] = (
                        float(value[0]), float(value[1]),
                        float(value[2]) if value[2] is not None else None,
                    )
            except (KeyError, TypeError, ValueError, IndexError):
                logger.warning("ignoring invalid price override for %s", model)

        result["tokens"] = data.get("tokens_per_request", {}) or {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("price override file unreadable: %s", e)
    return result


def _fetch_openrouter_prices() -> dict:
    """Fetch official API list prices from OpenRouter's public model list.

    Returns {model_id: (input_per_1M, output_per_1M, cache_read_per_1M)}.
    OpenRouter mirrors vendor list prices (z.ai, DeepSeek, MiniMax, ...).
    Raises on network/parse failure so callers fall through the chain.
    """
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "ollama-usage-monitor/1.1"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode())
    prices = {}
    for m in payload.get("data", []):
        pid = m.get("id", "")
        pricing = m.get("pricing", {}) or {}
        inp = pricing.get("prompt")
        out = pricing.get("completion")
        cache = pricing.get("input_cache_read") or pricing.get("cache_read")
        try:
            # OpenRouter reports per-token prices; our internal convention is
            # USD per 1M tokens (matches the builtin table) — normalize here.
            prices[pid] = (float(inp) * 1e6, float(out) * 1e6,
                           float(cache) * 1e6 if cache is not None else None)
        except (TypeError, ValueError):
            continue
    if not prices:
        raise RuntimeError("OpenRouter model list returned no pricing")
    return prices


def _resolve_api_prices() -> tuple[dict, str]:
    """Resolve per-1M-token prices with per-model manual overrides.

    Automatic base: live OpenRouter (24h cache) → builtin defaults.
    Manual entries from ~/.hermes/ollama-usage-prices.json are merged on top,
    so a partial override does not make every other model disappear.
    """
    manual = _load_manual_price_overrides()["prices"]

    def _with_manual(base: dict, source: str) -> tuple[dict, str]:
        if not manual:
            return base, source
        merged = dict(base)
        merged.update(manual)
        return merged, f"{source} + manual overrides"

    now = time.time()
    if _prices_cache["prices"] and (now - _prices_cache["ts"]) < PRICE_CACHE_TTL_SECONDS:
        return _with_manual(_prices_cache["prices"], _prices_cache["source"])
    if PRICE_CACHE_FILE.exists():
        try:
            cached = json.loads(PRICE_CACHE_FILE.read_text())
            age = now - cached.get("fetched_at", 0)
            if age < PRICE_CACHE_TTL_SECONDS and cached.get("prices"):
                _prices_cache["ts"] = now
                _prices_cache["prices"] = cached["prices"]
                _prices_cache["source"] = "OpenRouter (cached)"
                return _with_manual(cached["prices"], "OpenRouter (cached)")
        except (json.JSONDecodeError, OSError):
            pass
    try:
        live = _fetch_openrouter_prices()
        _prices_cache["ts"] = now
        _prices_cache["prices"] = live
        _prices_cache["source"] = "OpenRouter (live)"
        try:
            PRICE_CACHE_FILE.write_text(json.dumps(
                {"fetched_at": now, "prices": live}, indent=2))
        except OSError:
            pass
        return _with_manual(live, "OpenRouter (live)")
    except Exception as e:
        logger.warning("OpenRouter price fetch failed: %s", e)

    return _with_manual(_BUILTIN_PRICES, "builtin defaults")


_BUILTIN_PRICES = {
    # USD per 1M tokens: input, output, cached input (OpenRouter snapshot Aug 2026).
    "glm-5.2": (0.07, 0.22, 0.013),
    "glm-5.2:cloud": (0.07, 0.22, 0.013),
    "glm-5": (1.40, 4.40, 0.26),
    "deepseek-v4-flash:0731": (0.09, 0.18, 0.018),
    "deepseek-v4-flash": (0.14, 0.28, 0.028),
    "deepseek-v4-pro": (0.435, 0.87, 0.003625),
    "minimax-m3": (0.30, 1.20, 0.06),
    "gemma4:31b": (0.10, 0.34, 0.10),
    "kimi-k2.7-code": (0.70, 3.50, 0.15),
    "kimi-k2.6": (0.95, 4.00, 0.16),
    "gpt-5.5": (5.00, 30.00, 0.50),
    "gpt-oss:120b": (0.037, 0.17, None),
    "nemotron-3-ultra": (0.60, 3.60, 0.20),
    "nemotron-3-super": (0.30, 0.90, None),
}


def _lookup_price(API_PRICES: dict, model: str) -> tuple | None:
    """Resolve a dashboard model name to a price tuple.

    Matching order (first hit wins):
    1. Exact key match
    2. OpenRouter suffix match with ':' → '-' conversion
       (e.g. 'deepseek-v4-flash:0731' → 'deepseek-v4-flash-0731'
        matches 'deepseek/deepseek-v4-flash-0731')
    3. Normalized suffix match (strip separators, ignore '-it' suffix)
       (e.g. 'gemma4:31b' → 'gemma431b' matches 'google/gemma-4-31b-it')
    4. Base name (strip ':variant') exact + suffix
       (e.g. 'deepseek-v4-flash:0731' → 'deepseek-v4-flash'
        matches 'deepseek/deepseek-v4-flash')
    """
    if model in API_PRICES:
        return API_PRICES[model]

    # Ollama sometimes omits architecture/parameter suffixes used by
    # OpenRouter. Keep these explicit rather than fuzzy-matching the wrong SKU.
    aliases = {
        "nemotron-3-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
        "nemotron-3-super": "nvidia/nemotron-3-super-120b-a12b",
    }
    alias = aliases.get(model)
    if alias and alias in API_PRICES:
        return API_PRICES[alias]

    # Ollama uses ':' for variants (e.g. 'model:0731'), OpenRouter uses '-'.
    hyphen_model = model.replace(":", "-")

    # 2. Try the full model name with ':' → '-' before stripping the variant.
    for key, val in API_PRICES.items():
        if key.endswith(f"/{hyphen_model}"):
            return val

    # 3. Normalized match: strip all separators, ignore common OpenRouter suffixes.
    def _norm(s: str) -> str:
        s = s.lower().replace("-", "").replace(":", "").replace(".", "")
        s = s.replace("_", "")
        # Strip OpenRouter trailing tags like ':free', '-it', '-instruct'
        for tag in ("it", "instruct", "free", "latest"):
            if s.endswith(tag):
                s = s[: -len(tag)]
        return s
    norm_model = _norm(hyphen_model)
    for key, val in API_PRICES.items():
        key_base = key.rsplit("/", 1)[-1] if "/" in key else key
        if _norm(key_base) == norm_model:
            return val

    # 4. Strip the variant and try base name.
    base = model.split(":")[0]
    if base in API_PRICES:
        return API_PRICES[base]
    for key, val in API_PRICES.items():
        if key.endswith(f"/{base}"):
            return val
    # Final offline gap-fill. Live/manual data still wins every earlier match.
    return _BUILTIN_PRICES.get(model) or _BUILTIN_PRICES.get(base)


def _resolve_token_averages(overrides: dict) -> tuple[dict, str]:
    """Resolve per-model average tokens/request.

    Hermes state.db is the automatic base. Manual entries are merged on top
    per model, so a partial override does not discard real data for all other
    models. The caller uses a weighted global state.db average for unknown
    models, then a fixed 1000/500 fallback only when no local data exists.
    """
    from_db = _real_token_averages()
    merged = dict(from_db)
    manual = {}
    for model, value in (overrides.get("tokens") or {}).items():
        try:
            manual[model] = (float(value[0]), float(value[1]), float(value[2]))
        except (TypeError, ValueError, IndexError):
            logger.warning("ignoring invalid token override for %s", model)
    merged.update(manual)

    if manual and from_db:
        return merged, "Hermes state.db + manual overrides"
    if manual:
        return merged, "manual overrides"
    if from_db:
        return merged, "Hermes state.db"
    return {}, "no token data"


def _parse_usage(html: str) -> dict:
    """Parse the settings page HTML for usage data."""
    result = {
        "plan": None,
        "session_used_pct": None,
        "session_reset": None,
        "weekly_used_pct": None,
        "weekly_reset": None,
        "models": [],
        "source": "cookie",
        "share_basis": "ollama_usage_bar",
        "reset_estimated": False,
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

    _enrich_with_costs(result)
    return result


def _enrich_with_costs(data: dict) -> None:
    """Add honest subscription and pay-per-token estimates in place.

    Ollama quota percentage is not money spent. The fixed subscription is
    therefore represented only as a monthly price and its 7-day equivalent.
    API-equivalent values use request counts from Ollama, historical average
    token mix from Hermes, and per-token prices from the configured resolver.
    """
    weekly_segs = data.get("weekly_models") or []
    total_reqs = sum(max(int(s.get("requests") or 0), 0) for s in weekly_segs)

    # Fixed subscription economics. Never multiply the fee by quota usage:
    # 80% quota used does not mean 80% of the subscription fee was consumed.
    plan_key = (data.get("plan") or "pro").lower()
    monthly_cost = PLAN_MONTHLY_USD.get(plan_key, PLAN_MONTHLY_USD["pro"])
    weekly_equivalent = monthly_cost / WEEKS_PER_MONTH
    effective_sub_cpr = weekly_equivalent / total_reqs if total_reqs else 0.0

    data["subscription_monthly_cost"] = round(monthly_cost, 2)
    data["subscription_weekly_equivalent"] = round(weekly_equivalent, 4)
    data["effective_subscription_cost_per_req"] = round(effective_sub_cpr, 6)
    data["total_weekly_requests"] = total_reqs
    # Backward-compatible names for older frontends/history records.
    data["est_weekly_budget"] = round(weekly_equivalent, 2)
    data["est_cost_consumed"] = round(weekly_equivalent, 2)
    data["est_avg_cost_per_req"] = round(effective_sub_cpr, 6)
    data["quota_weighted_plan_value"] = round(
        weekly_equivalent * ((data.get("weekly_used_pct") or 0.0) / 100.0), 2
    )

    for seg in weekly_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        request_share = reqs / total_reqs if total_reqs else 0.0
        seg["request_share_pct"] = round(request_share * 100.0, 1)
        seg["est_cost"] = round(weekly_equivalent * request_share, 4)
        seg["est_cost_per_req"] = round(effective_sub_cpr, 6) if reqs else 0.0
        # Per-model quota cost is not exposed by Ollama; do not invent it.
        seg["est_cost_per_req_pct"] = None

    api_prices, price_source = _resolve_api_prices()
    overrides = _load_manual_price_overrides()
    token_avgs, token_source = _resolve_token_averages(overrides)

    fallback_avg = _real_global_token_average()
    fallback_basis = "request-weighted all-model history" if fallback_avg else None
    if fallback_avg is None and token_avgs:
        vals = list(token_avgs.values())
        fallback_avg = (
            round(sum(v[0] for v in vals) / len(vals)),
            round(sum(v[1] for v in vals) / len(vals)),
            round(sum(v[2] for v in vals) / len(vals)),
        )
        fallback_basis = "cross-model mean"

    api_total_raw = 0.0
    priced_requests = 0
    unpriced_models = []
    token_bases = set()
    manual_token_models = set(overrides.get("tokens", {}))

    def _token_profile(model: str):
        avg = token_avgs.get(model)
        if avg and model in manual_token_models:
            basis = "manual model override"
        elif avg:
            basis = "model history"
        elif fallback_avg:
            avg = fallback_avg
            basis = fallback_basis
        else:
            avg = (1000.0, 500.0, 0.0)
            basis = "fixed fallback"
        return (
            max(float(avg[0] or 0), 0.0),
            max(float(avg[1] or 0), 0.0),
            max(float(avg[2] or 0), 0.0),
            basis,
        )

    for seg in weekly_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        in_t, out_t, cache_t, token_basis = _token_profile(seg["model"])
        prompt_t = in_t + cache_t
        token_bases.add(token_basis)
        seg["token_estimate_basis"] = token_basis
        seg["avg_uncached_input_tokens"] = round(in_t)
        seg["avg_cache_tokens"] = round(cache_t)
        seg["avg_prompt_tokens"] = round(prompt_t)
        seg["avg_in_tokens"] = round(prompt_t)
        seg["avg_out_tokens"] = round(out_t)
        seg["cache_hit_pct"] = round(cache_t / prompt_t * 100.0, 1) if prompt_t > 0 else None
        seg["total_uncached_input_tokens"] = round(in_t * reqs)
        seg["total_cache_read_tokens"] = round(cache_t * reqs)
        seg["total_prompt_tokens"] = round(prompt_t * reqs)
        seg["total_in_tokens"] = round(prompt_t * reqs)
        seg["total_out_tokens"] = round(out_t * reqs)

        prices = _lookup_price(api_prices, seg["model"])
        if not prices:
            unpriced_models.append(seg["model"])
            for field in (
                "api_cost_per_req", "api_effective_per_1m", "api_weekly_cost",
                "api_cost_pct", "api_input_cost", "api_cache_cost",
                "api_output_cost", "api_input_per_1m", "api_output_per_1m",
                "api_cache_per_1m",
            ):
                seg[field] = None
            continue

        p_in, p_out, p_cache_published = prices
        p_in = float(p_in)
        p_out = float(p_out)
        # Missing cache pricing means no published discount, not free input.
        p_cache = p_in if p_cache_published is None else float(p_cache_published)

        per_req = (
            (in_t / 1e6) * p_in
            + (cache_t / 1e6) * p_cache
            + (out_t / 1e6) * p_out
        )
        processed_per_req = prompt_t + out_t
        effective_per_1m = (
            per_req * 1e6 / processed_per_req if processed_per_req > 0 else None
        )
        model_window_cost = per_req * reqs
        api_total_raw += model_window_cost
        priced_requests += reqs

        seg["api_cost_per_req"] = round(per_req, 6)
        seg["api_effective_per_1m"] = round(effective_per_1m, 6) if effective_per_1m is not None else None
        seg["api_weekly_cost"] = round(model_window_cost, 4)  # compatibility/internal totals
        seg["api_cost_pct"] = None  # per-model subscription comparison is unknowable
        seg["api_input_per_1m"] = round(p_in, 6)
        seg["api_output_per_1m"] = round(p_out, 6)
        seg["api_cache_per_1m"] = round(p_cache, 6)
        seg["api_cache_price_published"] = p_cache_published is not None
        seg["api_input_cost"] = round((in_t / 1e6) * p_in * reqs, 4)
        seg["api_cache_cost"] = round((cache_t / 1e6) * p_cache * reqs, 4)
        seg["api_output_cost"] = round((out_t / 1e6) * p_out * reqs, 4)

    api_known_window_total = round(api_total_raw, 4) if priced_requests else None
    pricing_complete = bool(total_reqs) and priced_requests == total_reqs
    api_window_total = api_known_window_total if pricing_complete else None
    data["api_known_window_total"] = api_known_window_total
    data["api_window_total"] = api_window_total
    data["api_weekly_total"] = api_window_total  # backward compatibility: complete totals only
    data["api_price_coverage_pct"] = round(
        priced_requests / total_reqs * 100.0, 2
    ) if total_reqs else None
    data["api_unpriced_models"] = unpriced_models
    data["api_assumption"] = (
        "Token estimate basis: " + ", ".join(sorted(token_bases))
        if token_bases else "No priced requests"
    )
    data["price_source"] = price_source
    data["token_source"] = token_source
    data["ollama_monthly"] = round(monthly_cost, 2)

    # ── session-level API equivalent cost ──
    session_segs = data.get("session_models") or []
    session_api_total = 0.0
    session_priced_requests = 0
    session_unpriced = []
    for seg in session_segs:
        reqs = max(int(seg.get("requests") or 0), 0)
        in_t, out_t, cache_t, _ = _token_profile(seg["model"])
        prompt_t = in_t + cache_t
        seg["avg_uncached_input_tokens"] = round(in_t)
        seg["avg_cache_tokens"] = round(cache_t)
        seg["avg_prompt_tokens"] = round(prompt_t)
        seg["avg_in_tokens"] = round(prompt_t)
        seg["avg_out_tokens"] = round(out_t)
        seg["total_uncached_input_tokens"] = round(in_t * reqs)
        seg["total_cache_read_tokens"] = round(cache_t * reqs)
        seg["total_prompt_tokens"] = round(prompt_t * reqs)
        seg["total_in_tokens"] = round(prompt_t * reqs)
        seg["total_out_tokens"] = round(out_t * reqs)

        prices = _lookup_price(api_prices, seg["model"])
        if not prices:
            session_unpriced.append(seg["model"])
            seg["api_session_cost"] = None
            continue
        p_in, p_out, p_cache_published = prices
        p_in = float(p_in)
        p_out = float(p_out)
        p_cache = p_in if p_cache_published is None else float(p_cache_published)
        per_req = (in_t / 1e6) * p_in + (cache_t / 1e6) * p_cache + (out_t / 1e6) * p_out
        model_session_cost = per_req * reqs
        session_api_total += model_session_cost
        session_priced_requests += reqs
        seg["api_session_cost"] = round(model_session_cost, 4)

    total_session_reqs = sum(max(int(s.get("requests") or 0), 0) for s in session_segs)
    session_complete = bool(total_session_reqs) and session_priced_requests == total_session_reqs
    data["api_session_known_total"] = round(session_api_total, 4) if session_priced_requests else None
    data["api_session_total"] = data["api_session_known_total"] if session_complete else None
    data["api_session_coverage_pct"] = round(
        session_priced_requests / total_session_reqs * 100.0, 2
    ) if total_session_reqs else None
    data["api_session_unpriced"] = session_unpriced

    # Per-model Ollama effective $/1M. The settings page exposes each model's
    # share of the weekly usage bar. Allocate the fixed 7-day plan equivalent
    # by that share, then divide by estimated model tokens. The official API
    # lacks usage-bar shares,
    # so API-only mode must not fabricate per-model Ollama prices.
    is_cookie = data.get("source") == "cookie"
    positive_share_total = sum(
        max(float(seg.get("share_pct") or 0), 0.0) for seg in weekly_segs
    )

    weekly_used_fraction = max(float(data.get("weekly_used_pct") or 0), 0.0) / 100.0

    for seg in weekly_segs:
        seg["plan_rate_basis"] = None
        if is_cookie:
            gpu_share = seg.get("share_pct")
            reqs = max(int(seg.get("requests") or 0), 0)
            if gpu_share is not None and gpu_share > 0 and reqs > 0 and positive_share_total > 0:
                model_tokens = (
                    seg.get("avg_uncached_input_tokens", 0)
                    + seg.get("avg_cache_tokens", 0)
                    + seg.get("avg_out_tokens", 0)
                )
                seg["plan_effective_per_1m"] = round(
                    weekly_equivalent * weekly_used_fraction * (gpu_share / positive_share_total)
                    / (model_tokens * reqs) * 1e6,
                    9,
                ) if model_tokens > 0 else None
                seg["plan_rate_basis"] = "ollama_usage_bar+historical_tokens"
            else:
                seg["plan_effective_per_1m"] = None
        else:
            seg["plan_effective_per_1m"] = None
        if seg.get("plan_effective_per_1m") is not None and seg.get("api_effective_per_1m"):
            seg["plan_pct_of_api"] = round(
                seg["plan_effective_per_1m"] / seg["api_effective_per_1m"] * 100, 1
            )
        else:
            seg["plan_pct_of_api"] = None
    data["plan_rate_allocation_basis"] = (
        "7-day plan equivalent × observed weekly quota fraction × normalized usage-bar share, "
        "divided by estimated model tokens"
        if is_cookie else
        "Unavailable in API-only mode: /api/usage has no per-model quota weights"
    )

    if api_window_total is not None and api_window_total > 0:
        savings = api_window_total - weekly_equivalent
        data["api_savings_vs_plan"] = round(savings, 2)
        data["api_savings"] = round(savings, 2)  # compatibility
        data["api_monthly_proj"] = None
        data["api_monthly_projection_reason"] = "weekly elapsed time is not exposed by the API"
        data["api_total_pct"] = round(weekly_equivalent / api_window_total * 100.0, 1)
        data["api_vs_plan_ratio"] = (
            round(api_window_total / weekly_equivalent, 3) if weekly_equivalent > 0 else None
        )
        data["break_even_usage_multiple"] = round(weekly_equivalent / api_window_total, 3)
        data["break_even_pct"] = round(weekly_equivalent / api_window_total * 100.0, 1)
    else:
        for field in (
            "api_savings_vs_plan", "api_savings", "api_monthly_proj",
            "api_total_pct", "api_vs_plan_ratio", "break_even_usage_multiple",
            "break_even_pct",
        ):
            data[field] = None


def _real_token_averages() -> dict:
    """Canonical per-model averages from Hermes usage accounting.

    ``input_tokens`` is uncached input; ``cache_read_tokens`` is a separate
    bucket. Modern ``session_model_usage`` is authoritative because one chat
    may route calls to several models. Older Hermes schemas fall back to the
    aggregate ``sessions`` table.
    """
    state_db = STATE_DB
    if not state_db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            for table in ("session_model_usage", "sessions"):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT model,
                               ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                        FROM {table}
                        WHERE billing_provider = 'ollama-cloud'
                          AND api_call_count > 0
                          AND (input_tokens > 0 OR cache_read_tokens > 0 OR output_tokens > 0)
                        GROUP BY model
                        """,
                    ).fetchall()
                    if rows or table == "sessions":
                        return {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in rows if r[0]}
                except sqlite3.Error:
                    if table == "sessions":
                        raise
            return {}
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return {}


def _real_global_token_average() -> tuple | None:
    """Request-weighted canonical average, modern schema first."""
    state_db = STATE_DB
    if not state_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            for table in ("session_model_usage", "sessions"):
                try:
                    row = conn.execute(
                        f"""
                        SELECT ROUND(SUM(input_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(output_tokens)*1.0/SUM(api_call_count), 0),
                               ROUND(SUM(cache_read_tokens)*1.0/SUM(api_call_count), 0)
                        FROM {table}
                        WHERE billing_provider = 'ollama-cloud'
                          AND api_call_count > 0
                          AND (input_tokens > 0 OR cache_read_tokens > 0 OR output_tokens > 0)
                        """,
                    ).fetchone()
                    if row and row[0] is not None:
                        return (row[0] or 0, row[1] or 0, row[2] or 0)
                except sqlite3.Error:
                    if table == "sessions":
                        raise
            return None
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


def _week_key(_reset_iso: str | None = None) -> str:
    """Current observation week's Monday; never key by a future reset date."""
    dt = _utc_now()
    monday = dt - timedelta(days=dt.weekday())
    return monday.date().isoformat()


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
            "plan": data.get("plan"),
            "subscription_monthly_cost": data.get("subscription_monthly_cost"),
            "subscription_weekly_equivalent": data.get("subscription_weekly_equivalent"),
            "effective_subscription_cost_per_req": data.get("effective_subscription_cost_per_req"),
            # Legacy keys retained so old readers can still open new records.
            "est_cost_consumed": data.get("subscription_weekly_equivalent"),
            "est_weekly_budget": data.get("subscription_weekly_equivalent"),
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
        now = _utc_now()
        bucket_epoch = int(now.timestamp()) // (5 * 3600) * (5 * 3600)
        win = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).isoformat()
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
    Also generates per-month reports and a lifetime report.
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

    # Generate main report
    _write_main_report(weeks, sessions)
    # Generate per-month reports
    _write_monthly_reports(weeks, sessions)
    # Generate lifetime report
    _write_lifetime_report(weeks, sessions)
    return str(REPORT_FILE)


def _write_main_report(weeks: list, sessions: list) -> None:
    lines = [
        "# Ollama Cloud Usage Stats",
        "",
        f"_Generated {_utc_now().strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Weekly overview",
        "",
    ]
    if weeks:
        lines.append("| Week | Weekly used | Plan equivalent | Top model | Requests |")
        lines.append("|------|-------------|-----------------|-----------|----------|")
        for w in weeks:
            models = w.get("models") or []
            top = max(models, key=lambda m: m.get("share_pct") or 0) if models else {}
            total = sum(m.get("requests") or 0 for m in models)
            cost = (w.get("subscription_weekly_equivalent")
                    if w.get("subscription_weekly_equivalent") is not None
                    else w.get("est_weekly_budget"))
            cost_s = f"${cost:.2f}" if cost is not None else "?"
            lines.append(f"| {w.get('week')} | {w.get('weekly_used_pct')}% | {cost_s} | {top.get('model') or '-'} | {total} |")
    else:
        lines.append("_No weekly snapshots yet._")
    lines += ["", "## All 5h sessions", ""]
    if sessions:
        for s in reversed(sessions[-50:]):
            ts = s.get("ts", "")[:16].replace("T", " ")
            lines.append(f"### {ts} — session {s.get('session_used_pct')}% · weekly {s.get('weekly_used_pct')}%")
            lines.append("")
            for label, key in [("Session", "session_models"), ("Weekly", "weekly_models")]:
                mods = s.get(key) or []
                if mods:
                    lines += [f"{label} per model:", "", "| Model | Requests | Share |", "|-------|----------|-------|"]
                    for m in mods:
                        lines.append(f"| {m.get('model')} | {m.get('requests')} | {m.get('share_pct')}% |")
                    lines.append("")
    else:
        lines.append("_No session snapshots yet._")
        lines.append("")
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(lines))


def _write_monthly_reports(weeks: list, sessions: list) -> list:
    """Generate one MD file per month in ~/.hermes/ollama-usage-reports/.
    Returns list of {month, path} for the pane to link to."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Group weeks by month (week key is YYYY-MM-DD, month is YYYY-MM)
    months_w: dict[str, list] = {}
    for w in weeks:
        wk = w.get("week") or ""
        month = wk[:7]  # YYYY-MM
        if month:
            months_w.setdefault(month, []).append(w)

    # Group sessions by month (ts starts with YYYY-MM)
    months_s: dict[str, list] = {}
    for s in sessions:
        ts = s.get("ts") or ""
        month = ts[:7]
        if month:
            months_s.setdefault(month, []).append(s)

    all_months = sorted(set(list(months_w.keys()) + list(months_s.keys())), reverse=True)
    result = []
    for month in all_months:
        mw = months_w.get(month, [])
        ms = months_s.get(month, [])
        lines = [
            f"# Ollama Cloud Usage — {month}",
            "",
            f"_Generated {_utc_now().strftime('%Y-%m-%d %H:%M UTC')}_",
            "",
            "## Weekly overview",
            "",
        ]
        if mw:
            lines.append("| Week | Weekly used | Plan equivalent | Top model | Requests |")
            lines.append("|------|-------------|-----------------|-----------|----------|")
            for w in mw:
                models = w.get("models") or []
                top = max(models, key=lambda m: m.get("share_pct") or 0) if models else {}
                total = sum(m.get("requests") or 0 for m in models)
                cost = (w.get("subscription_weekly_equivalent")
                    if w.get("subscription_weekly_equivalent") is not None
                    else w.get("est_weekly_budget"))
                cost_s = f"${cost:.2f}" if cost is not None else "?"
                lines.append(f"| {w.get('week')} | {w.get('weekly_used_pct')}% | {cost_s} | {top.get('model') or '-'} | {total} |")
        else:
            lines.append("_No weekly snapshots._")
        lines += ["", "## 5h sessions", ""]
        if ms:
            for s in ms:
                ts = s.get("ts", "")[:16].replace("T", " ")
                lines.append(f"### {ts} — session {s.get('session_used_pct')}% · weekly {s.get('weekly_used_pct')}%")
                lines.append("")
                for label, key in [("Session", "session_models"), ("Weekly", "weekly_models")]:
                    mods = s.get(key) or []
                    if mods:
                        lines += [f"{label} per model:", "", "| Model | Requests | Share |", "|-------|----------|-------|"]
                        for m in mods:
                            lines.append(f"| {m.get('model')} | {m.get('requests')} | {m.get('share_pct')}% |")
                        lines.append("")
        else:
            lines.append("_No session snapshots._")
            lines.append("")

        filepath = REPORTS_DIR / f"{month}.md"
        filepath.write_text("\n".join(lines))
        result.append({"month": month, "path": str(filepath)})

    return result


def _write_lifetime_report(weeks: list, sessions: list) -> str:
    """Generate a lifetime report without inventing per-model quota cost."""
    lt = _lifetime_from_records(weeks)
    lines = [
        "# Ollama Cloud Usage — Lifetime Stats",
        "",
        f"_Generated {_utc_now().strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        f"**{lt.get('weeks_count', 0)} weeks recorded · {lt.get('total_requests', 0)} total requests**",
        "",
        "## Effective subscription cost",
        "",
        f"- Recorded plan equivalent: **${lt.get('subscription_total_equivalent', 0):.2f}**",
        f"- Effective average: **${lt.get('effective_subscription_cost_per_req', 0):.4f}/request**",
        "- This allocates the fixed subscription price across recorded requests; Ollama does not expose per-model quota cost.",
        "",
        "## Requests by model",
        "",
    ]
    models = lt.get("models") or []
    if models:
        lines.append("| Model | Requests |")
        lines.append("|-------|----------|")
        for model_row in models:
            lines.append(f"| {model_row['model']} | {model_row['requests']} |")
        lines.append("")
    else:
        lines += ["_No data yet._", ""]

    # Monthly summary table
    months_w: dict[str, list] = {}
    for w in weeks:
        month = (w.get("week") or "")[:7]
        if month:
            months_w.setdefault(month, []).append(w)
    if months_w:
        lines += ["## Monthly summary", "", "| Month | Weeks | Total requests | Avg weekly used | Plan equivalent |", "|-------|-------|----------------|-----------------|-----------------|"]
        for month in sorted(months_w.keys(), reverse=True):
            mw = months_w[month]
            reqs = sum(sum((m.get("requests") or 0) for m in (w.get("models") or [])) for w in mw)
            valid_used = [float(w["weekly_used_pct"]) for w in mw if isinstance(w.get("weekly_used_pct"), (int, float))]
            avg_used = sum(valid_used) / len(valid_used) if valid_used else 0
            cost = sum(
                (w.get("subscription_weekly_equivalent")
                 if w.get("subscription_weekly_equivalent") is not None
                 else w.get("est_weekly_budget") or 0)
                for w in mw
            )
            lines.append(f"| {month} | {len(mw)} | {reqs} | {avg_used:.1f}% | ${cost:.2f} |")
        lines.append("")

    filepath = REPORTS_DIR / "lifetime.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath.write_text("\n".join(lines))
    return str(filepath)


def _lifetime_from_records(weeks: list) -> dict:
    """Aggregate effective fixed-plan economics from saved weekly windows."""
    if not weeks:
        return {"ok": True, "weeks_count": 0, "models": []}

    model_reqs: dict[str, int] = {}
    total_plan_equivalent = 0.0
    for rec in weeks:
        weekly_equivalent = rec.get("subscription_weekly_equivalent")
        if weekly_equivalent is None:
            # Old records stored the full plan allocation under this key.
            weekly_equivalent = rec.get("est_weekly_budget")
        if weekly_equivalent is None:
            weekly_equivalent = rec.get("est_cost_consumed") or 0.0
        total_plan_equivalent += float(weekly_equivalent or 0.0)

        for model_row in rec.get("models") or []:
            model = model_row.get("model")
            reqs = int(model_row.get("requests") or 0)
            if model and reqs > 0:
                model_reqs[model] = model_reqs.get(model, 0) + reqs

    total_reqs = sum(model_reqs.values())
    effective_cpr = total_plan_equivalent / total_reqs if total_reqs else 0.0
    models = [
        {"model": model, "requests": reqs}
        for model, reqs in sorted(model_reqs.items(), key=lambda item: item[1], reverse=True)
    ]
    return {
        "ok": True,
        "weeks_count": len(weeks),
        "total_requests": total_reqs,
        "subscription_total_equivalent": round(total_plan_equivalent, 4),
        "effective_subscription_cost_per_req": round(effective_cpr, 6),
        # Backward-compatible summary names.
        "est_total_cost": round(total_plan_equivalent, 4),
        "est_avg_cost_per_req": round(effective_cpr, 6),
        "est_avg_cost_per_req_pct": None,
        "models": models,
    }


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
            "subscription_weekly_equivalent": (
                rec.get("subscription_weekly_equivalent")
                if rec.get("subscription_weekly_equivalent") is not None
                else rec.get("est_weekly_budget")
            ),
            "est_cost_consumed": (
                rec.get("subscription_weekly_equivalent")
                if rec.get("subscription_weekly_equivalent") is not None
                else rec.get("est_weekly_budget")
            ),
            "top_model": top.get("model") if top else None,
            "total_requests": sum(m.get("requests") or 0 for m in (rec.get("models") or [])),
        })
    weeks.sort(key=lambda w: w.get("week") or "", reverse=True)
    return {"ok": True, "weeks": weeks[:HISTORY_MAX_WEEKS]}


def _lifetime_stats() -> dict:
    """Aggregate all saved weekly records with current honest semantics."""
    if not HISTORY_FILE.exists():
        return {"ok": True, "weeks_count": 0, "models": []}
    weeks = []
    for line in HISTORY_FILE.read_text().splitlines():
        try:
            weeks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return _lifetime_from_records(weeks)


def _fetch_usage() -> dict:
    """Fetch Ollama Cloud usage — returns dict with data or error."""
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        data = dict(_cache["data"])
        data["cached"] = True
        data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return data

    # Primary: cookie scrape (has GPU-weighted bar widths).
    try:
        cookie = _load_cookie()
        html = _fetch_settings_page(cookie)
        data = _parse_usage(html)
        if data.get("session_used_pct") is None or data.get("weekly_used_pct") is None:
            raise ValueError("settings page did not contain usage limits")
        data["cached"] = False
        data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["ok"] = True
        _cache["ts"] = now
        _cache["data"] = data
        _record_history(data)
        _record_session(data)
        return data
    except FileNotFoundError:
        pass  # no cookie file, try API
    except Exception as e:
        logger.warning("Cookie scrape failed (%s), trying API fallback", e)

    # Fallback: official API (no GPU bar widths).
    api_key = _load_api_key()
    if api_key:
        try:
            api_data = _fetch_usage_api(api_key)
            data = _api_to_usage(api_data)
            data["cached"] = False
            data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            data["ok"] = True
            _cache["ts"] = now
            _cache["data"] = data
            _record_history(data)
            _record_session(data)
            return data
        except Exception as e:
            logger.warning("API fallback also failed: %s", e)

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
    """Generate (or refresh) all reports; returns paths."""
    _generate_report()
    # Build the response with monthly + lifetime paths
    reports_dir = REPORTS_DIR
    months = []
    if reports_dir.exists():
        for f in sorted(reports_dir.glob("*.md"), reverse=True):
            if f.name != "lifetime.md":
                months.append({"month": f.stem, "path": str(f)})
    lifetime_path = str(reports_dir / "lifetime.md") if (reports_dir / "lifetime.md").exists() else None
    return {"ok": True, "path": str(REPORT_FILE), "months": months, "lifetime_path": lifetime_path}


@router.post("/usage/report/open")
async def usage_report_open():
    """Generate all reports and open the main one in the default app."""
    _generate_report()
    try:
        import subprocess as _sp
        _sp.Popen(["open", str(REPORT_FILE)])
        return {"ok": True, "path": str(REPORT_FILE), "opened": True}
    except (OSError, FileNotFoundError):
        return {"ok": True, "path": str(REPORT_FILE), "opened": False}


@router.post("/usage/report/open-month")
async def usage_report_open_month(path: str = ""):
    """Open a specific monthly or lifetime report by path."""
    if not path:
        return {"ok": False, "error": "no path"}
    try:
        import subprocess as _sp
        _sp.Popen(["open", path])
        return {"ok": True, "path": path, "opened": True}
    except (OSError, FileNotFoundError):
        return {"ok": True, "path": path, "opened": False}


class _PlanBody(BaseModel):
    plan: str = ""


@router.get("/usage/plan")
async def usage_get_plan():
    """Return the current plan tier and its source."""
    plan = _infer_plan()
    source = "default (Pro)" if plan is None else "config file"
    return {"ok": True, "plan": plan or "Pro", "source": source}


@router.post("/usage/plan")
async def usage_set_plan(body: _PlanBody):
    """Set the plan tier manually. Valid values: free, pro, max."""
    plan_lower = body.plan.strip().lower()
    if plan_lower not in VALID_PLANS:
        return {"ok": False, "error": "invalid_plan",
                "detail": f"Must be one of: {', '.join(VALID_PLANS)}"}
    try:
        PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLAN_FILE.write_text(plan_lower + "\n")
        # Invalidate cache so next fetch uses the new plan
        _cache["ts"] = 0
        _cache["data"] = None
        return {"ok": True, "plan": plan_lower.capitalize(),
                "source": "config file"}
    except OSError as e:
        return {"ok": False, "error": "write_failed", "detail": str(e)}
