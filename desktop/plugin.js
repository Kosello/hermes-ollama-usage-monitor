/**
 * Ollama Cloud Usage — Hermes desktop plugin.
 *
 * Shows Ollama Cloud session/weekly usage in a statusbar chip and a pane.
 * Data comes from the official Ollama usage API through the plugin backend;
 * settings-page scraping is only a fallback.
 */

import { cn, haptic, host, PALETTE_AREA, Tip, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ollama-usage-monitor'
const REFRESH_MS = 60000
const SETTINGS_VERSION = 2

// ── section settings (persisted via ctx.storage) ──────────────────────────
const SECTION_KEYS = [
  'savings', 'limits', 'session_models', 'weekly_models',
  'cache_hit', 'token_volume',
  'api_cost', 'cost_efficiency',
  'usage_pct', 'price_comparison',
  'history', 'reports',
]
const SECTION_LABELS = {
  savings:        'Subscription vs API headline',
  limits:         'Limits',
  session_models:'Session — per model',
  weekly_models: 'Weekly — per model',
  cache_hit:      'Cache hit % per model',
  token_volume:   'Estimated token volume',
  api_cost:       'API equivalent cost',
  cost_efficiency:'Plan vs API',
  usage_pct:      'API usage percentage',
  price_comparison:'Price comparison',
  history:        'Weekly history',
  reports:        'Reports',
}
const DEFAULT_SETTINGS = Object.fromEntries(SECTION_KEYS.map(k => [k, true]))

function normalizeSettings(value) {
  if (!value || value._version !== SETTINGS_VERSION) {
    return { ...DEFAULT_SETTINGS, _version: SETTINGS_VERSION }
  }
  return { ...DEFAULT_SETTINGS, ...value, _version: SETTINGS_VERSION }
}

// ── data hooks ────────────────────────────────────────────────────────────
function useUsage(ctx) {
  return useQuery({
    queryKey: [ctx.source, 'usage'],
    queryFn: () => ctx.rest('/usage'),
    refetchInterval: REFRESH_MS,
    staleTime: 30000,
    retry: 1
  })
}
function useHistory(ctx) {
  return useQuery({
    queryKey: [ctx.source, 'history'],
    queryFn: () => ctx.rest('/usage/history'),
    staleTime: 300000,
    retry: 1
  })
}
function useLifetime(ctx) {
  return useQuery({
    queryKey: [ctx.source, 'lifetime'],
    queryFn: () => ctx.rest('/usage/lifetime'),
    staleTime: 300000,
    retry: 1
  })
}

// ── helpers ───────────────────────────────────────────────────────────────
function pctColor(p) {
  if (p == null) return 'text-(--ui-text-tertiary)'
  if (p >= 90) return 'text-(--ui-badge-error)'
  if (p >= 75) return 'text-(--ui-badge-warning)'
  return 'text-(--ui-text-secondary)'
}

function trendArrow(weeks) {
  if (!weeks || weeks.length < 2) return null
  const cur = weeks[0]?.weekly_used_pct
  const prev = weeks[1]?.weekly_used_pct
  if (cur == null || prev == null) return null
  if (cur > prev) return '↑'
  if (cur < prev) return '↓'
  return '→'
}

function fmtTokens(n) {
  if (n == null) return '?'
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`
  return String(n)
}

function fmtUsd(n, digits = 4) {
  if (n == null) return '?'
  const d = Math.abs(n) < 0.0001 && n !== 0 ? 6 : digits
  return Number(n).toFixed(d)
}

function fmtPerMillion(n) {
  if (n == null) return '?'
  if (Math.abs(n) >= 10) return Number(n).toFixed(1)
  if (Math.abs(n) >= 1) return Number(n).toFixed(2)
  return Number(n).toFixed(3)
}

// ── collapsible section ───────────────────────────────────────────────────
function Collapsible({ title, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return jsxs('div', {
    className: 'mt-2',
    children: [
      jsx('button', {
        type: 'button',
        className: cn(
          'flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-left',
          'font-medium text-xs text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
        ),
        onClick: () => { haptic('tap'); setOpen(o => !o) },
        children: [
          jsx('span', { className: 'truncate', children: title }),
          jsx('span', { className: 'shrink-0 text-(--ui-text-quaternary) transition-transform', children: open ? '▾' : '▸' })
        ]
      }),
      open ? jsx('div', { className: 'mt-1 flex flex-col gap-0.5 text-xs', children }) : null
    ]
  })
}

// ── settings toggle ──────────────────────────────────────────────────────
function Toggle({ label, checked, onChange }) {
  return jsxs('button', {
    type: 'button',
    'aria-pressed': checked,
    className: 'flex items-center gap-2 px-1 py-0.5 text-left hover:bg-(--chrome-action-hover) rounded w-full',
    onClick: () => { haptic('tap'); onChange(!checked) },
    children: [
      jsx('span', {
        className: cn(
          'inline-flex h-3.5 w-6 shrink-0 items-center rounded-full transition-colors'
        ),
        style: {
          backgroundColor: checked ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
          border: '1px solid var(--ui-stroke-secondary)'
        },
        children: jsx('span', {
          className: cn(
            'inline-block h-2.5 w-2.5 rounded-full bg-white transition-transform'
          ),
          style: { transform: checked ? 'translateX(12px)' : 'translateX(2px)' }
        })
      }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-secondary)', children: label }),
      jsx('span', {
        className: 'shrink-0 text-[0.625rem] text-(--ui-text-quaternary)',
        children: checked ? 'On' : 'Off'
      })
    ]
  })
}

// ── statusbar chip ────────────────────────────────────────────────────────
function UsageChip({ ctx }) {
  const { data, isLoading } = useUsage(ctx)
  const session = data?.session_used_pct
  const weekly = data?.weekly_used_pct
  const plan = data?.plan

  let label = 'ollama: …'
  if (!isLoading && data) {
    if (data.ok === false) {
      label = 'ollama: n/a'
    } else {
      const s = session != null ? `${Math.round(session)}%` : '?'
      const w = weekly != null ? `${Math.round(weekly)}%` : '?'
      label = `${plan ? plan + ' ' : ''}S${s} W${w}`
    }
  }

  return jsx(Tip, {
    label: jsxs('div', {
      className: 'flex flex-col gap-1 p-1 text-xs',
      children: [
        jsx('div', { className: 'font-medium', children: 'Ollama Cloud' }),
        jsx('div', {
          className: 'text-(--ui-text-secondary)',
          children: data?.ok === false
            ? (data.error === 'cookie_not_configured'
                ? 'Cookie not configured — add __Secure-session to ~/.hermes/ollama_cookie.txt'
                : `Unavailable: ${data.error}`)
            : `${session != null ? `Session ${session.toFixed(1)}% used` : 'Session n/a'} · ${weekly != null ? `Weekly ${weekly.toFixed(1)}% used` : 'Weekly n/a'}`
        }),
        jsx('div', {
          className: 'text-(--ui-text-quaternary)',
          children: data?.reset_unavailable
            ? 'Reset timestamps are not exposed by the usage API'
            : (data?.session_reset ? `Session resets ${data.session_reset}` : '')
        })
      ]
    }),
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] transition-colors',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      ),
      type: 'button',
      title: 'Ollama Cloud usage — click to refresh',
      onClick: () => { haptic('tap'); host.notify({ kind: 'info', message: label }) },
      children: jsx('span', { className: pctColor(Math.max(session ?? 0, weekly ?? 0)), children: label })
    })
  })
}

// ── pane ──────────────────────────────────────────────────────────────────
function UsagePane({ ctx }) {
  const { data, isLoading, refetch } = useUsage(ctx)
  const { data: hist } = useHistory(ctx)
  const { data: lifetime } = useLifetime(ctx)
  const [reports, setReports] = useState(null)
  const qc = useQueryClient()

  // Load settings from storage (persists across reloads)
  const [settings, setSettings] = useState(null)
  const [showSettings, setShowSettings] = useState(false)
  const [planOverride, setPlanOverride] = useState(null)

  // Load on mount — ctx.storage may not exist, use localStorage fallback
  useEffect(() => {
    let done = false
    const loadSettings = () => {
      try {
        const raw = localStorage.getItem('ollama-usage-settings')
        if (raw) return normalizeSettings(JSON.parse(raw))
      } catch (e) { /* ignore */ }
      return normalizeSettings(null)
    }
    if (ctx.storage && typeof ctx.storage.get === 'function') {
      const result = ctx.storage.get('settings')
      if (result && typeof result.then === 'function') {
        result.then(s => {
          if (!done) setSettings(normalizeSettings(s))
        }).catch(() => {
          if (!done) setSettings(loadSettings())
        })
      } else {
        setSettings(normalizeSettings(result))
      }
    } else {
      setSettings(loadSettings())
    }
    // Also load reports
    ctx.rest('/usage/report').then(r => {
      if (r?.ok) setReports(r)
    }).catch(() => {})
    // Load current plan
    ctx.rest('/usage/plan').then(r => {
      if (r?.ok) setPlanOverride(r.plan)
    }).catch(() => {})
    return () => { done = true }
  }, [])

  const saveSettings = next => {
    const normalized = { ...next, _version: SETTINGS_VERSION }
    setSettings(normalized)
    try {
      if (ctx.storage && typeof ctx.storage.set === 'function') {
        ctx.storage.set('settings', normalized)
      }
      localStorage.setItem('ollama-usage-settings', JSON.stringify(normalized))
    } catch (e) { /* ignore */ }
  }

  const saveSetting = (key, val) => saveSettings({ ...settings, [key]: val })

  const setAllSections = enabled => {
    saveSettings({
      ...settings,
      ...Object.fromEntries(SECTION_KEYS.map(key => [key, enabled]))
    })
  }

  const refresh = () => {
    haptic('tap')
    qc.invalidateQueries({ queryKey: [ctx.source, 'usage'] })
    refetch()
  }

  if (!settings) return jsx('div', { className: 'p-3 text-(--ui-text-quaternary)', children: 'Loading…' })

  const rows = []

  // ── settings panel ──
  if (showSettings) {
    rows.push(jsxs('div', {
      className: 'mt-2 border border-(--ui-stroke-secondary) rounded p-2 flex flex-col gap-0.5',
      children: [
        // Plan selector
        jsxs('div', {
          className: 'flex items-center justify-between gap-2 py-1',
          children: [
            jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: 'Plan' }),
            jsxs('div', { className: 'flex gap-0.5', children: [
              ...['Free', 'Pro', 'Max'].map(p =>
                jsx('button', {
                  type: 'button',
                  className: cn(
                    'rounded px-2 py-0.5 text-xs transition-colors',
                    (planOverride || 'Pro') === p
                      ? 'bg-(--ui-accent) text-white'
                      : 'bg-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
                  ),
                  onClick: () => {
                    haptic('tap')
                    setPlanOverride(p)
                    ctx.rest('/usage/plan', { method: 'POST', body: { plan: p.toLowerCase() } })
                      .then(r => { if (r?.ok) { setPlanOverride(r.plan); refresh() } })
                      .catch(() => {})
                  },
                  children: p
                }, p)
              )
            ]})
          ]
        }),
        jsx('div', { className: 'h-px bg-(--ui-stroke-secondary) my-1' }),
        jsxs('div', {
          className: 'mb-1 flex items-center justify-between gap-2',
          children: [
            jsx('div', { className: 'font-medium text-xs', children: 'Sections' }),
            jsxs('div', { className: 'flex gap-2', children: [
              jsx('button', {
                type: 'button',
                className: 'text-xs text-(--ui-accent) hover:underline',
                onClick: () => setAllSections(true),
                children: 'Show all'
              }),
              jsx('button', {
                type: 'button',
                className: 'text-xs text-(--ui-text-quaternary) hover:underline',
                onClick: () => setAllSections(false),
                children: 'Hide all'
              })
            ]})
          ]
        }),
        ...SECTION_KEYS.map(k =>
          jsx(Toggle, {
            label: SECTION_LABELS[k],
            checked: settings[k] ?? true,
            onChange: v => saveSetting(k, v)
          }, k)
        ),
        jsx('button', {
          className: 'mt-1 text-xs text-(--ui-accent) hover:underline self-end',
          onClick: () => { haptic('tap'); setShowSettings(false) },
          children: 'Done'
        })
      ]
    }))
  }

  if (isLoading) {
    rows.push(jsx('div', { className: 'text-(--ui-text-quaternary)', children: 'Loading…' }))
  } else if (!data || data.ok === false) {
    const msg = data?.error === 'cookie_not_configured'
      ? 'Cookie not configured.\n\nRun:\n  echo \'__Secure-session=<value>\' > ~/.hermes/ollama_cookie.txt'
      : `Unavailable: ${data?.error || 'no data'}`
    rows.push(jsx('div', {
      className: 'whitespace-pre-wrap text-(--ui-text-secondary) text-xs',
      children: msg
    }))
  } else {
    const s = data.session_used_pct
    const w = data.weekly_used_pct
    const planEquivalent = Number(data.subscription_weekly_equivalent ?? 0)
    const apiComparisonTotal = data.api_window_total ?? data.api_known_window_total
    const apiComparisonComplete = data.api_window_total != null

    // ── header + settings button ──
    rows.push(jsxs('div', {
      className: 'flex items-center justify-between',
      children: [
        jsx('div', { className: 'font-medium', children: `Ollama Cloud — ${data.plan || 'Unknown'} plan` }),
        jsx('button', {
          className: 'text-xs text-(--ui-text-quaternary) hover:text-(--ui-text-secondary) px-1',
          onClick: () => { haptic('tap'); setShowSettings(s2 => !s2) },
          children: '⚙'
        })
      ]
    }))

    // ── subscription vs API headline ──
    if (settings.savings && apiComparisonTotal != null) {
      const difference = apiComparisonTotal - planEquivalent
      const subscriptionWins = difference >= 0
      const title = apiComparisonComplete
        ? (subscriptionWins ? 'Plan equivalent below API estimate' : 'API estimate below plan equivalent so far')
        : (subscriptionWins ? 'Plan equivalent below known API subtotal' : 'API comparison is incomplete')
      rows.push(jsxs('div', {
        className: 'mt-2 border border-(--ui-stroke-secondary) rounded p-2 text-xs',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('span', {
                className: 'text-(--ui-text-secondary)',
                children: title
              }),
              jsx('span', {
                className: 'font-bold text-(--ui-text-primary)',
                children: subscriptionWins
                  ? `${apiComparisonComplete ? '' : '≥ '}$${Math.abs(difference).toFixed(2)}`
                  : `$${Math.abs(difference).toFixed(2)}`
              })
            ]
          }),
          jsx('div', {
            className: 'text-(--ui-text-quaternary) mt-0.5',
            children: `${apiComparisonComplete ? 'API estimate' : 'Known API subtotal'} $${apiComparisonTotal.toFixed(2)} · ${data.plan || 'Plan'} 7-day equivalent $${planEquivalent.toFixed(2)}${apiComparisonComplete ? '' : ` · ${data.api_price_coverage_pct?.toFixed(2) ?? '?'}% price coverage`}`
          })
        ]
      }))
    }

    // ── limits ──
    if (settings.limits) {
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.limits,
        defaultOpen: true,
        children: [
          jsx('div', { className: 'flex items-center justify-between gap-2', children: [
            jsxs('span', { className: 'truncate', children: [
              'Session',
              data.reset_unavailable
                ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: ' · reset not exposed' })
                : jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` · ${data.reset_estimated ? 'est. reset in' : 'resets in'} ${data.session_reset || '?'}` })
            ]}),
            jsx('span', { className: pctColor(s), children: s != null ? `${s.toFixed(1)}% used` : 'n/a' })
          ]}),
          jsx('div', { className: 'flex items-center justify-between gap-2', children: [
            jsxs('span', { className: 'truncate', children: [
              'Weekly',
              data.reset_unavailable
                ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: ' · reset not exposed' })
                : jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` · ${data.reset_estimated ? 'est. reset in' : 'resets in'} ${data.weekly_reset || '?'}` })
            ]}),
            jsx('span', { className: pctColor(w), children: w != null ? `${w.toFixed(1)}% used` : 'n/a' })
          ]})
        ]
      }))
    }

    // ── session per model ──
    if (settings.session_models && data.session_models && data.session_models.length > 0) {
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.session_models,
        defaultOpen: true,
        children: data.session_models.map(m =>
          jsx('div', {
            className: 'flex items-center justify-between gap-2',
            children: [
              jsx('span', { className: 'truncate', children: m.model }),
              jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0 tabular-nums', children: [
                `${m.requests} req · ${m.share_pct != null ? m.share_pct.toFixed(1) : '?'}% ${data.share_basis === 'ollama_usage_bar' ? 'usage bar' : 'req share'}`
              ]})
            ]
          }, m.model)
        )
      }))
    }

    // ── weekly per model ──
    if (settings.weekly_models && data.weekly_models && data.weekly_models.length > 0) {
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.weekly_models,
        defaultOpen: true,
        children: data.weekly_models.map(m =>
          jsx('div', {
            className: 'flex items-center justify-between gap-2',
            children: [
              jsx('span', { className: 'truncate', children: m.model }),
              jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0 tabular-nums', children: [
                `${m.requests} req · ${m.share_pct != null ? m.share_pct.toFixed(1) : '?'}% ${data.share_basis === 'ollama_usage_bar' ? 'usage bar' : 'req share'}`
              ]})
            ]
          }, m.model)
        )
      }))
    }

    // ── effective subscription cost for current quota window ──
    // ── cache hit % ──
    if (settings.cache_hit && data.weekly_models && data.weekly_models.length > 0) {
      const known = data.weekly_models.filter(m => m.cache_hit_pct != null)
      if (known.length > 0) {
        rows.push(jsx(Collapsible, {
          title: SECTION_LABELS.cache_hit,
          defaultOpen: false,
          children: [
            known.map(m =>
              jsxs('div', {
                className: 'flex items-center justify-between gap-2',
                children: [
                  jsx('span', { className: 'truncate', children: m.model }),
                  jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                    m.cache_hit_pct != null ? `${m.cache_hit_pct.toFixed(0)}% cache hit` : '?',
                    ' · ', fmtTokens(m.avg_in_tokens), ' in · ', fmtTokens(m.avg_cache_tokens), ' cached'
                  ]})
                ]
              }, m.model)
            ),
            jsx('div', {
              className: 'text-(--ui-text-quaternary) mt-1',
              children: 'Historical Hermes averages, not current-window Ollama telemetry. Missing cache prices use the normal input rate.'
            })
          ]
        }))
      }
    }

    // ── token volume ──
    if (settings.token_volume && data.weekly_models && data.weekly_models.length > 0) {
      const known = data.weekly_models.filter(m => m.total_in_tokens != null)
      if (known.length > 0) {
        rows.push(jsx(Collapsible, {
          title: SECTION_LABELS.token_volume,
          defaultOpen: false,
          children: [
            known.map(m =>
              jsxs('div', {
                className: 'flex items-center justify-between gap-2 tabular-nums',
                children: [
                  jsx('span', { className: 'truncate', children: m.model }),
                  jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                    fmtTokens(m.total_in_tokens), ' in · ', fmtTokens(m.total_out_tokens), ' out'
                  ]})
                ]
              }, m.model)
            ),
            jsx('div', {
              className: 'text-(--ui-text-quaternary) mt-1',
              children: 'Estimated current quota-window volume: Ollama requests × historical average tokens/request'
            })
          ]
        }))
      }
    }

    // ── API equivalent cost ──
    const sessionTotal = data.api_session_total ?? data.api_session_known_total
    const weeklyTotal = data.api_window_total ?? data.api_known_window_total
    if (settings.api_cost && (sessionTotal != null || weeklyTotal != null)) {
      const weeklyModels = (data.weekly_models || []).filter(m => m.api_weekly_cost != null)
      const sessionModels = (data.session_models || []).filter(m => m.api_session_cost != null)
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.api_cost,
        defaultOpen: true,
        children: jsxs('div', { className: 'space-y-1', children: [
          sessionTotal != null ? jsx('div', {
            className: 'text-(--ui-text-primary) tabular-nums text-sm',
            children: `Session: $${sessionTotal.toFixed(4)}`
          }) : null,
          sessionModels.length > 0 ? jsxs('div', { className: 'space-y-0.5 ml-2', children: [
            ...sessionModels.map(m => jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate text-(--ui-text-quaternary)', children: m.model }),
                jsx('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: `$${m.api_session_cost.toFixed(4)}` })
              ]
            }, 's-' + m.model))
          ]}) : null,
          weeklyTotal != null ? jsx('div', {
            className: 'text-(--ui-text-primary) tabular-nums text-sm',
            children: `Weekly:  $${weeklyTotal.toFixed(4)}`
          }) : null,
          weeklyModels.length > 0 ? jsxs('div', { className: 'space-y-0.5 ml-2', children: [
            ...weeklyModels.map(m => jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate text-(--ui-text-quaternary)', children: m.model }),
                jsx('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: `$${m.api_weekly_cost.toFixed(4)}` })
              ]
            }, 'w-' + m.model))
          ]}) : null,
          jsx('div', {
            className: 'text-(--ui-text-quaternary) text-[0.6875rem] mt-1',
            children: 'Estimated API cost of tokens already consumed on Ollama Cloud this window.'
          })
        ]})
      }))
    }

    // ── Plan vs API: compact per-model $/1M comparison ──
    if (settings.cost_efficiency && apiComparisonTotal != null) {
      const pricedModels = (data.weekly_models || []).filter(
        m => m.api_effective_per_1m != null
      )
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.cost_efficiency,
        defaultOpen: true,
        children: jsxs('div', { className: 'space-y-1', children: [
          jsx('div', {
            className: 'text-(--ui-text-quaternary) text-xs',
            children: data.source === 'cookie'
              ? `Settings usage bars · ${pricedModels.length} API-priced models`
              : 'API fallback · per-model Ollama price unavailable'
          }),
          jsx('div', {
            className: 'rounded border border-(--ui-stroke-secondary) overflow-hidden',
            children: pricedModels.map(m => {
              const inP = m.api_input_per_1m != null ? `$${fmtPerMillion(m.api_input_per_1m)}` : '?'
              const cacheP = m.api_cache_per_1m != null ? `$${fmtPerMillion(m.api_cache_per_1m)}` : 'n/a'
              const cacheN = m.api_cache_price_published ? '' : '*'
              const outP = m.api_output_per_1m != null ? `$${fmtPerMillion(m.api_output_per_1m)}` : '?'
              const ollamaP = m.plan_effective_per_1m != null ? `$${fmtPerMillion(m.plan_effective_per_1m)}` : 'n/a'
              const pct = m.plan_pct_of_api != null ? `${m.plan_pct_of_api.toFixed(0)}%` : 'n/a'
              return jsxs('div', {
                className: 'px-2 py-1.5 border-b last:border-b-0 border-(--ui-stroke-secondary) tabular-nums',
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-between gap-2',
                    children: [
                      jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: m.model }),
                      jsxs('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: [
                        'Ollama ', ollamaP, ' · API $', fmtPerMillion(m.api_effective_per_1m), ' · ',
                        jsx('b', { className: m.plan_pct_of_api != null && m.plan_pct_of_api < 20 ? 'text-(--ui-badge-success)' : '', children: pct })
                      ]})
                    ]
                  }),
                  jsx('div', { className: 'text-right text-(--ui-text-quaternary) text-[0.625rem]', children: `API input/cache/output: ${inP} / ${cacheP}${cacheN} / ${outP}` })
                ]
              }, m.model)
            })
          }),
          jsx('div', { className: 'text-(--ui-text-quaternary) text-[0.625rem]', children: 'Ollama $/1M = 7-day plan price × observed quota fraction × normalized bar share ÷ estimated tokens. * cache discount not published.' })
        ]})
      }))
    }

    // ── API usage percentage: just the Ollama/API ratio per model ──
    if (settings.usage_pct && data.source === 'cookie') {
      const pricedModels = (data.weekly_models || []).filter(
        m => m.plan_pct_of_api != null
      )
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.usage_pct,
        defaultOpen: false,
        children: jsxs('div', { className: 'space-y-1', children: [
          jsx('div', {
            className: 'text-(--ui-text-quaternary) text-xs',
            children: 'How much of the API pay-per-token price you effectively pay via the subscription'
          }),
          jsx('div', {
            className: 'rounded border border-(--ui-stroke-secondary) overflow-hidden',
            children: pricedModels.map(m => {
              const pct = m.plan_pct_of_api != null ? m.plan_pct_of_api.toFixed(0) : '?'
              const pctNum = m.plan_pct_of_api
              return jsxs('div', {
                className: 'px-2 py-1.5 border-b last:border-b-0 border-(--ui-stroke-secondary) tabular-nums flex items-center justify-between gap-2',
                children: [
                  jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: m.model }),
                  jsxs('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: [
                    jsx('b', { className: pctNum != null && pctNum < 20 ? 'text-(--ui-badge-success)' : pctNum != null && pctNum > 80 ? 'text-(--ui-badge-warning)' : '', children: `${pct}%` }),
                    ' of API'
                  ]})
                ]
              }, m.model)
            })
          }),
          jsx('div', { className: 'text-(--ui-text-quaternary) text-[0.625rem]', children: 'Lower = better subscription value. <20% green, >80% yellow.' })
        ]})
      }))
    }

    // ── Price comparison: raw $/1M numbers side by side ──
    if (settings.price_comparison) {
      const pricedModels = (data.weekly_models || []).filter(
        m => m.api_effective_per_1m != null
      )
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.price_comparison,
        defaultOpen: false,
        children: jsxs('div', { className: 'space-y-1', children: [
          jsx('div', {
            className: 'text-(--ui-text-quaternary) text-xs',
            children: data.source === 'cookie'
              ? 'Effective $/1M — subscription allocation vs pay-per-token API'
              : 'API fallback — per-model Ollama price unavailable'
          }),
          jsx('div', {
            className: 'rounded border border-(--ui-stroke-secondary) overflow-hidden',
            children: pricedModels.map(m => {
              const ollamaP = m.plan_effective_per_1m != null ? `$${fmtPerMillion(m.plan_effective_per_1m)}` : 'n/a'
              return jsxs('div', {
                className: 'px-2 py-1.5 border-b last:border-b-0 border-(--ui-stroke-secondary) tabular-nums',
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-between gap-2',
                    children: [
                      jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: m.model }),
                      jsxs('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: [
                        'Ollama ', ollamaP, ' · API $', fmtPerMillion(m.api_effective_per_1m)
                      ]})
                    ]
                  }, m.model),
                  jsx('div', { className: 'text-right text-(--ui-text-quaternary) text-[0.625rem]', children: '/1M tokens estimated' })
                ]
              })
            })
          }),
          jsx('div', { className: 'text-(--ui-text-quaternary) text-[0.625rem]', children: 'Ollama: subscription allocated by usage-bar share. API: pay-per-token estimate.' })
        ]})
      }))
    }

  }

  // ── weekly history (outside the ok check — works even if fetch fails) ──
  const weeks = hist?.weeks || []
  if (settings.history && weeks.length > 0) {
    const arrow = trendArrow(weeks)
    rows.push(jsx(Collapsible, {
      title: SECTION_LABELS.history,
      defaultOpen: false,
      children: [
        weeks.map((w, i) => {
          const pct = w.weekly_used_pct
          const isCur = i === 0
          return jsxs('div', {
            className: 'flex items-center justify-between gap-2 tabular-nums',
            children: [
              jsxs('span', { className: 'truncate', children: [
                w.week,
                isCur && arrow ? jsx('span', { className: 'ml-1', children: arrow }) : null
              ]}),
              jsxs('span', { className: 'shrink-0 text-(--ui-text-secondary)', children: [
                pct != null ? `${pct.toFixed(1)}%` : '?',
                ' · $', String((w.subscription_weekly_equivalent ?? w.est_cost_consumed) != null ? (w.subscription_weekly_equivalent ?? w.est_cost_consumed).toFixed(2) : '?'),
                ' plan eq.',
                w.top_model ? ` · ${w.top_model}` : ''
              ]})
            ]
          }, w.week + i)
        }),
        jsx('div', { className: 'text-(--ui-text-quaternary) mt-1', children: 'Weekly snapshots — kept locally, survives Ollama resets' })
      ]
    }))
  }

  // ── reports (monthly + lifetime links) ──
  if (settings.reports && reports && reports.ok !== false) {
    const months = reports.months || []
    rows.push(jsx(Collapsible, {
      title: 'Reports',
      defaultOpen: false,
      children: [
        months.length > 0 ? months.map(m =>
          jsxs('div', {
            className: 'flex items-center justify-between gap-2',
            children: [
              jsx('span', { className: 'truncate', children: m.month }),
              jsx('button', {
                className: 'text-xs text-(--ui-accent) hover:underline shrink-0',
                onClick: () => {
                  haptic('tap')
                  ctx.rest('/usage/report/open-month', { method: 'POST', body: { path: m.path } }).then(r => {
                    host.notify({ kind: r?.opened ? 'info' : 'warning', message: r?.opened ? `Opened ${m.month}` : m.path })
                  }).catch(() => host.notify({ kind: 'warning', message: 'Could not open' }))
                },
                children: 'Open'
              })
            ]
          }, m.month)
        ) : jsx('div', { className: 'text-(--ui-text-quaternary)', children: 'No monthly reports yet' }),
        reports.lifetime_path ? jsxs('div', {
          className: 'flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-1 mt-1',
          children: [
            jsx('span', { className: 'font-medium', children: 'Lifetime' }),
            jsx('button', {
              className: 'text-xs text-(--ui-accent) hover:underline shrink-0',
              onClick: () => {
                haptic('tap')
                ctx.rest('/usage/report/open-month', { method: 'POST', body: { path: reports.lifetime_path } }).then(r => {
                  host.notify({ kind: r?.opened ? 'info' : 'warning', message: r?.opened ? 'Opened lifetime' : reports.lifetime_path })
                }).catch(() => host.notify({ kind: 'warning', message: 'Could not open' }))
              },
              children: 'Open'
            })
          ]
        }) : null
      ]
    }))
  }

  rows.push(jsxs('div', {
    className: 'mt-3 flex items-center gap-2',
    children: [
      jsx('button', {
        className: cn(
          'rounded-md border px-2 py-1 text-xs',
          'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
        ),
        type: 'button',
        onClick: refresh,
        children: 'Refresh'
      }),
      jsx('button', {
        className: cn(
          'rounded-md border px-2 py-1 text-xs',
          'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
        ),
        type: 'button',
        onClick: () => {
          haptic('tap')
          ctx.os.openExternal('http://localhost:8642/')
        },
        children: 'Dashboard ↗'
      })
    ]
  }))

  return jsx('div', { className: 'flex h-full flex-col gap-2 overflow-y-auto p-3 text-sm', children: rows })
}

export default {
  id: ID,
  name: 'Ollama Usage',
  register(ctx) {

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Ollama Usage',
      data: { placement: 'right', width: '240px' },
      render: () => jsx(UsagePane, { ctx })
    })
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(UsageChip, { ctx })
    })
    ctx.register({
      id: 'refresh',
      area: PALETTE_AREA,
      data: {
        id: 'ollama-usage.refresh',
        label: 'Refresh Ollama Cloud usage',
        keywords: ['ollama', 'usage', 'quota'],
        run: () => { haptic('tap'); host.notify({ kind: 'info', message: 'Ollama usage refreshed (check the pane)' }) }
      }
    })
  }
}