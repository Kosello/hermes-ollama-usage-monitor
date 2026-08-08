# Ollama Cloud Usage Stats

Live Ollama Cloud usage in Hermes Agent — session & weekly quotas, per-model request counts, weekly history, average cost per request, and threshold alerts, right in the desktop app.

![Ollama Cloud](https://img.shields.io/badge/ollama-cloud-000000?logo=ollama&logoColor=white)
![CI](https://github.com/Kosello/ollama-cloud-usage-stats/actions/workflows/ci.yml/badge.svg)

## What it shows

- **Statusbar chip** — always-visible `Pro S25% W35%` summary, color-coded (yellow ≥75%, red ≥90%)
- **Desktop pane** — full details:
  - Session & weekly usage % + reset times
  - **Per-model breakdown** — request counts and each model's share of the usage bar (session + weekly)
  - **Avg cost per request** — estimate in $ and % of weekly budget, per model
  - **Weekly history** — last 8 weeks of usage snapshots with trend arrow (↑/↓/→)
- **Threshold alerts** — macOS notification when weekly usage crosses 75% (warning) / 90% (critical), once per threshold per week
- **Daily usage line** — one short summary in chat every morning (`📊 Ollama Cloud: weekly 44.7% · session 81.6% · top: glm-5.2`)
- Auto-refreshes every 60s (manual refresh button + ⌘K palette command)

## Why

Ollama Cloud has **no usage API** — the only source is the web dashboard at [ollama.com/settings](https://ollama.com/settings). This plugin scrapes that page with your session cookie (same approach as the community [`/ollama` slash command](https://github.com/3L0935/hermes-plugins)) and adds a live desktop UI on top. Ollama's dashboard forgets everything when the week resets — this plugin keeps a **local history** that survives resets.

## Install

### 1. Backend (Python plugin)

```bash
mkdir -p ~/.hermes/plugins/ollama-usage-monitor
cp -r backend/* ~/.hermes/plugins/ollama-usage-monitor/
hermes plugins enable ollama-usage-monitor
```

### 2. Desktop plugin

```bash
mkdir -p ~/.hermes/desktop-plugins/ollama-usage-monitor
cp desktop/plugin.js ~/.hermes/desktop-plugins/ollama-usage-monitor/
```

Then in the app: **⌘K → Reload desktop plugins**

### 3. Cookie

The plugin reads your Ollama Cloud session cookie from **either**:

- **macOS Keychain** (recommended on macOS):
  ```bash
  security add-generic-password -s hermes-ollama-cookie -a ollama -w '<cookie>' -U
  ```
- **or a plain file** (works on any OS — Linux, Windows, etc.):
  ```bash
  echo '__Secure-session=<value>' > ~/.hermes/ollama_cookie.txt
  chmod 600 ~/.hermes/ollama_cookie.txt
  ```

Get the value from your browser: open [ollama.com/settings](https://ollama.com/settings) (logged in) → DevTools → Application/Storage → Cookies → `https://ollama.com` → copy `__Secure-session`.

**Storage selection** via env var `OLLAMA_COOKIE_SOURCE`:
- `auto` (default) — Keychain if a cookie is stored there, otherwise the file
- `keychain` — Keychain only (errors if empty)
- `file` — file only (no Keychain dependency)

### 4. Restart

```bash
hermes gateway restart
```

### 5. Optional: alerts + daily line (cron)

```bash
# copy the helper scripts
cp scripts/ollama_usage_watch.py ~/.hermes/scripts/
cp scripts/ollama_usage_daily.py ~/.hermes/scripts/

# then in Hermes:
#   cronjob create:  ollama-usage-watchdog  → 30m → no_agent → ollama_usage_watch.py
#   cronjob create:  ollama-usage-daily     → 0 9 * * * → no_agent → ollama_usage_daily.py
```

The watchdog is **silent** until a threshold is crossed (no token cost, no spam). It also snapshots weekly usage into the history file, so history is recorded even when the desktop app is closed.

## Cost estimate methodology

Ollama Cloud bills by GPU-time utilization, not tokens. The per-request cost is a rough proxy:

```
weekly_budget   = plan_price / 4.33          # Pro $20/mo ≈ $4.62/wk
cost_consumed   = weekly_budget × weekly_used%
model_cost      = cost_consumed × model_share%
cost_per_req    = model_cost / requests
cost_per_req_%  = cost_per_req / weekly_budget × 100
```

The model share% comes straight from Ollama's own usage bar segments, which already reflect the GPU-time weighting — so heavier models (glm-5.2 = High) show higher per-request cost than lighter ones (deepseek-v4-flash = Medium).

## Caveats

- **Cookie scraping is brittle** — if Ollama changes their settings page markup or the cookie expires, the plugin shows "Unavailable". Re-extract the cookie and it's back.
- The cookie is a login token — keep it private (Keychain, or `chmod 600` on the file).
- Works only in the Hermes **desktop** app (the backend loads via the gateway; the chip/pane need the desktop UI). The `/ollama` slash command from the [community plugin](https://github.com/3L0935/hermes-plugins) covers CLI/TUI.

## Development

```bash
python tests/test_parser.py   # parser smoke test (no cookie needed — uses a saved HTML fixture)
```

CI (GitHub Actions) runs the smoke test + syntax checks on every push.

## Files

```
backend/
  plugin.yaml                 # Hermes plugin manifest
  dashboard/
    manifest.json             # backend API manifest
    plugin_api.py             # FastAPI router → scrapes ollama.com/settings
desktop/
  plugin.js                   # desktop plugin (statusbar chip + pane)
scripts/
  ollama_usage_watch.py       # threshold watchdog (cron, silent unless crossed)
  ollama_usage_daily.py       # daily one-line usage summary (cron)
tests/
  test_parser.py              # parser smoke test
  fixtures/settings_page.html # saved page snapshot for CI
```

## License

MIT
