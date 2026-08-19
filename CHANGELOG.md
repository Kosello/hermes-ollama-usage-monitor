# Changelog

All notable changes to **Ollama Usage Monitor** (Hermes plugin).

The format follows [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.3.0] — 2026-08-19

### Added
- **Statusbar reset popup** — hovering the statusbar chip now shows only the reset limits (session + weekly) in a solid, non-transparent tooltip.
- **Official DeepSeek-V4 API pricing** — the new rate table (effective 2026-08-16 16:00 UTC) with **peak/off-peak windows** (peak: 01:00–04:00 and 06:00–10:00 UTC). The tool picks the right rate for the current UTC hour automatically. Official rates override stale OpenRouter snapshots; manual overrides still win.
- **Cache break-even section** — per model, the cache hit rate at which the pay-per-token API becomes cheaper than the subscription. Values >100% mean the plan always wins; 0% means the API always wins.
- **Lifetime break-even & price comparison** — the same analysis aggregated over all saved weekly history.
- **Real provider cache rates** — cache hit rates pulled from the user's actual API usage (state.db, non-Ollama providers). Largest sample wins per model; OpenRouter is the fallback when no native key recorded the model.
- **Cache-aware API cost** — the API equivalent cost section now shows a second line per model: what the used tokens would cost at the real provider cache rate.
- **Plan vs API with cache** — a second comparison table under Plan vs API, with the API side priced at the real cache rate.
- **Lifetime line in API equivalent cost** — per-model lifetime API cost (with cache-aware variant), under the Weekly block.

### Fixed
- **Cache-rate resolution for model variants** — `deepseek-v4-flash:0731` was matching a 1-call/0-cache row (0.0% cache) instead of the base model's 361-call/97.8% sample. Variant suffixes are now merged into the base name during aggregation, so the largest sample wins.
- **Transparent statusbar popup** — the two-line tooltip used a nested flex layout that left transparent gaps; flattened to a single text node with `whitespace-pre-line` so the marker-style background fills continuously.

## [1.2.0] — 2026-08-12

### Added
- **Cache break-even analysis** — per-model cache hit rate at which the API becomes cheaper than the subscription.
- **Lifetime break-even & price comparison** — aggregated over all saved weekly history.
- **Real provider cache rates** — from the user's actual API usage, largest sample wins, OpenRouter as fallback.
- **Cache-aware API cost** — second line per model with the real cache rate applied.
- **Plan vs API with cache** — comparison table with the API side priced at the real cache rate.

## [1.1.0] — 2026-08-10

### Fixed
- **Reliable per-model Ollama $/1M** — plan allocation scaled by the observed weekly quota fraction. A freshly-reset week no longer allocates the full $4.60 weekly fee across a handful of requests (which produced absurd "GLM costs 2076% of the API price" ratios).

## [1.0.0] — 2026-08-09

### Added
- **Official Ollama API as primary source** — no cookie needed; cookie demoted to plan-tier fallback.
- **Manual plan selector** — Free/Pro/Max buttons in pane settings.
- **Price fallback chain** — manual override → live OpenRouter (24h cache) → builtin offline defaults; the active source is shown in the UI.
- **Token fallback chain** — manual override → Hermes `session_model_usage` → legacy `sessions` → request-weighted global history → fixed fallback.
- **Dashboard button** in the pane — opens the standalone dashboard at `localhost:8642`.

### Fixed
- **Correct usage economics and API cost estimates** — weekly fee no longer scaled by `weekly_used_fraction` in the wrong direction; API-only mode no longer fabricates per-model Ollama prices or reset timestamps; cookie fallback validates the parsed page.
- **Price matching for model variants** (`:0731`) and naming conventions.
- **Crash when an OpenRouter price has no `cache_read`** — falls back to the input rate, labeled `n/a`.

## [0.9.0] — 2026-08-08

### Added
- **Statusbar chip** — always-visible `Pro S25% W35%` summary, color-coded (yellow ≥75%, red ≥90%).
- **Desktop pane** — session & weekly usage with relative reset times, per-model breakdowns, collapsible sections, settings toggles.
- **Plan vs API comparison** — per-model effective Ollama $/1M vs real API $/1M with the Ollama/API ratio.
- **Weekly history** — collapsible at the bottom of the pane.
- **Monthly + lifetime MD reports** — with Open buttons in the pane.
- **Real token averages** from state.db for the API comparison — cache-aware pricing.
- **Threshold alerts** — warnings when session/weekly usage crosses configurable thresholds.
- **Keychain cookie support** — the Ollama cookie can be stored in the macOS keychain instead of a plaintext file.
- **Standalone CLI** — `ollama-cloud-watch.py` (no Hermes needed, stdlib only), kept byte-identical to the standalone repo.

### Fixed
- Settings persistence (async storage load, `ctx.storage` fallback).
- API comparison used 7-day-only token averages — models not used in the last 7 days got the wrong fallback.
