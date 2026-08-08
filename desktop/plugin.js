/**
 * Ollama Cloud Usage — Hermes desktop plugin.
 *
 * Shows Ollama Cloud session/weekly usage in a statusbar chip and a pane.
 * Data comes from the plugin Python backend at /api/plugins/ollama-usage-monitor/usage
 * (cookie-based scrape of ollama.com/settings, same as the /ollama slash command).
 */

import { cn, haptic, host, PALETTE_AREA, Tip, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ollama-usage-monitor'
const REFRESH_MS = 60000

// ── section settings (persisted via ctx.storage) ──────────────────────────
const SECTION_KEYS = [
  'savings', 'limits', 'session_models', 'weekly_models',
  'cost_week', 'cost_lifetime', 'cache_hit', 'token_volume',
  'api_cost', 'cost_efficiency', 'break_even', 'monthly_proj',
  'history', 'report',
]
const SECTION_LABELS = {
  savings:        'Your savings headline',
  limits:         'Limits',
  session_models:'Session — per model',
  weekly_models: 'Weekly — per model',
  cost_week:      'Avg cost per request (this week)',
  cost_lifetime:  'Avg cost per request (lifetime)',
  cache_hit:      'Cache hit % per model',
  token_volume:   'Token volume per model',
  api_cost:       'API equivalent cost',
  cost_efficiency:'Cost efficiency',
  break_even:     'Break-even comparison',
  monthly_proj:   'Monthly projection',
  history:        'Weekly history',
  report:         'Open report (MD)',
}
const DEFAULT_SETTINGS = Object.fromEntries(SECTION_KEYS.map(k => [k, true]))

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
    className: 'flex items-center gap-2 px-1 py-0.5 text-left hover:bg-(--chrome-action-hover) rounded w-full',
    onClick: () => { haptic('tap'); onChange(!checked) },
    children: [
      jsx('span', {
        className: cn(
          'inline-flex h-3.5 w-6 shrink-0 items-center rounded-full transition-colors',
          checked ? 'bg-(--ui-accent)' : 'bg-(--ui-stroke-secondary)'
        ),
        children: jsx('span', {
          className: cn(
            'inline-block h-2.5 w-2.5 rounded-full bg-white transition-transform',
            checked ? 'translate-x-3' : 'translate-x-0.5'
          )
        })
      }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary) truncate', children: label })
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
          children: data?.session_reset ? `Session resets ${data.session_reset}` : ''
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
      children: jsx('span', { className: pctColor(session), children: label })
    })
  })
}

// ── pane ──────────────────────────────────────────────────────────────────
function UsagePane({ ctx }) {
  const { data, isLoading, refetch } = useUsage(ctx)
  const { data: hist } = useHistory(ctx)
  const { data: lifetime } = useLifetime(ctx)
  const qc = useQueryClient()

  // Load settings from storage (persists across reloads)
  const [settings, setSettings] = useState(null)
  const [showSettings, setShowSettings] = useState(false)

  // Load on mount — ctx.storage may not exist, use localStorage fallback
  useEffect(() => {
    let done = false
    const loadSettings = () => {
      try {
        const raw = localStorage.getItem('ollama-usage-settings')
        if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) }
      } catch (e) { /* ignore */ }
      return DEFAULT_SETTINGS
    }
    if (ctx.storage && typeof ctx.storage.get === 'function') {
      const result = ctx.storage.get('settings')
      if (result && typeof result.then === 'function') {
        result.then(s => {
          if (!done) setSettings({ ...DEFAULT_SETTINGS, ...s })
        }).catch(() => {
          if (!done) setSettings(loadSettings())
        })
      } else {
        setSettings({ ...DEFAULT_SETTINGS, ...result })
      }
    } else {
      setSettings(loadSettings())
    }
    return () => { done = true }
  }, [])

  const saveSetting = (key, val) => {
    const next = { ...settings, [key]: val }
    setSettings(next)
    try {
      if (ctx.storage && typeof ctx.storage.set === 'function') {
        ctx.storage.set('settings', next)
      }
      localStorage.setItem('ollama-usage-settings', JSON.stringify(next))
    } catch (e) { /* ignore */ }
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
        jsx('div', { className: 'font-medium text-xs mb-1', children: 'Sections' }),
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

    // ── savings headline ──
    if (settings.savings && data.api_savings != null && data.api_savings > 0) {
      rows.push(jsxs('div', {
        className: 'mt-2 border border-(--ui-stroke-secondary) rounded p-2 text-xs',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('span', { className: 'text-(--ui-text-secondary)', children: '💰 You saved this week' }),
              jsx('span', { className: 'font-bold text-(--ui-text-primary)', children: `$${data.api_savings.toFixed(0)}` })
            ]
          }),
          jsxs('div', {
            className: 'text-(--ui-text-quaternary) mt-0.5',
            children: [
              `API $${data.api_weekly_total.toFixed(0)} · Ollama est. $${(data.est_cost_consumed ?? 0).toFixed(2)}`
            ]
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
              jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` · resets in ${data.session_reset || '?'}` })
            ]}),
            jsx('span', { className: pctColor(s), children: s != null ? `${s.toFixed(1)}% used` : 'n/a' })
          ]}),
          jsx('div', { className: 'flex items-center justify-between gap-2', children: [
            jsxs('span', { className: 'truncate', children: [
              'Weekly',
              jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` · resets in ${data.weekly_reset || '?'}` })
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
                `${m.requests} req · ${m.share_pct != null ? m.share_pct.toFixed(1) : ''}%`
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
                `${m.requests} req · ${m.share_pct != null ? m.share_pct.toFixed(1) : ''}%`
              ]})
            ]
          }, m.model)
        )
      }))
    }

    // ── avg cost this week ──
    if (settings.cost_week && data.weekly_models && data.weekly_models.length > 0 && data.est_weekly_budget != null) {
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.cost_week,
        defaultOpen: false,
        children: [
          data.weekly_models.map(m =>
            jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate', children: m.model }),
                jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                  '$', (m.est_cost_per_req ?? 0).toFixed(4),
                  ' · ', (m.est_cost_per_req_pct ?? 0).toFixed(3), '%'
                ]})
              ]
            }, m.model)
          ),
          jsxs('div', {
            className: 'text-(--ui-text-quaternary) mt-1',
            children: [
              'Budget: $', String(data.est_weekly_budget),
              '/wk · consumed: $', String(data.est_cost_consumed),
              ' · avg $', String(data.est_avg_cost_per_req ?? ''),
              '/req'
            ]
          })
        ]
      }))
    }

    // ── avg cost lifetime ──
    if (settings.cost_lifetime && lifetime && lifetime.ok !== false && lifetime.models && lifetime.models.length > 0) {
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.cost_lifetime,
        defaultOpen: false,
        children: [
          lifetime.models.map(m =>
            jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate', children: m.model }),
                jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                  '$', (m.est_cost_per_req ?? 0).toFixed(4),
                  ' · ', (m.est_cost_per_req_pct ?? 0).toFixed(3), '%'
                ]})
              ]
            }, m.model)
          ),
          jsxs('div', {
            className: 'text-(--ui-text-quaternary) mt-1',
            children: [
              String(lifetime.weeks_count ?? 0), ' weeks · ',
              String(lifetime.total_requests ?? 0), ' requests · total $',
              String((lifetime.est_total_cost ?? 0).toFixed(2)),
              ' · avg $', String((lifetime.est_avg_cost_per_req ?? 0).toFixed(4)),
              '/req'
            ]
          })
        ]
      }))
    }

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
              children: 'Higher cache = cheaper on raw APIs (cached input billed at 80-98% discount)'
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
              children: 'Token volume = avg tokens/req × request count. Explains why GLM costs more than Flash.'
            })
          ]
        }))
      }
    }

    // ── API equivalent cost ──
    if (settings.api_cost && data.api_weekly_total != null && data.weekly_models && data.weekly_models.length > 0) {
      const known = data.weekly_models.filter(m => m.api_cost_per_req != null)
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.api_cost,
        defaultOpen: false,
        children: [
          jsx('div', { className: 'text-(--ui-text-quaternary) mb-1', children: 'What the same usage would cost on official pay-per-token APIs' }),
          known.map(m =>
            jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate', children: m.model }),
                jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                  '$', String(m.api_weekly_cost != null ? m.api_weekly_cost.toFixed(2) : '?'),
                  '/wk · $', String(m.api_cost_per_req != null ? m.api_cost_per_req.toFixed(4) : '?'),
                  '/req'
                ]})
              ]
            }, m.model)
          ),
          jsxs('div', {
            className: 'flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-1 mt-1 font-medium',
            children: [
              jsx('span', { children: 'API total this week' }),
              jsxs('span', { children: [
                '$', String(data.api_weekly_total.toFixed(2)),
                ' · Ollama $', String((data.est_cost_consumed ?? 0).toFixed(2))
              ]})
            ]
          }),
          jsx('div', { className: 'text-(--ui-text-quaternary) mt-1', children: `Assumes ${data.api_assumption || ''}` })
        ]
      }))
    }

    // ── cost efficiency ──
    if (settings.cost_efficiency && data.api_weekly_total != null && data.weekly_models && data.weekly_models.length > 0) {
      const known = data.weekly_models.filter(m => m.api_cost_pct != null)
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.cost_efficiency,
        defaultOpen: false,
        children: [
          jsx('div', { className: 'text-(--ui-text-quaternary) mb-1', children: 'Ollama cost as % of API price — lower = better deal' }),
          known.map(m =>
            jsxs('div', {
              className: 'flex items-center justify-between gap-2 tabular-nums',
              children: [
                jsx('span', { className: 'truncate', children: m.model }),
                jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                  m.api_cost_pct != null ? `${m.api_cost_pct.toFixed(0)}% of API` : '?'
                ]})
              ]
            }, m.model)
          ),
          jsxs('div', {
            className: 'flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-1 mt-1 font-medium',
            children: [
              jsx('span', { children: 'Ollama overall' }),
              jsx('span', { children: data.api_total_pct != null ? `${data.api_total_pct.toFixed(0)}% of API total` : '?' })
            ]
          })
        ]
      }))
    }

    // ── break-even ──
    if (settings.break_even && data.api_weekly_total != null && data.api_savings != null) {
      const ratio = data.api_weekly_total > 0 ? (data.api_weekly_total / (data.est_cost_consumed || 1)) : 0
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.break_even,
        defaultOpen: false,
        children: [
          jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
            jsx('span', { children: 'API cost / week' }),
            jsxs('span', { className: 'text-(--ui-text-secondary) tabular-nums', children: [
              '$', data.api_weekly_total.toFixed(2)
            ]})
          ]}),
          jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
            jsx('span', { children: 'Ollama cost / week' }),
            jsxs('span', { className: 'text-(--ui-text-secondary) tabular-nums', children: [
              '$', (data.est_cost_consumed ?? 0).toFixed(2)
            ]})
          ]}),
          jsxs('div', { className: 'flex items-center justify-between gap-2 mt-1', children: [
            jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Break-even at' }),
            jsxs('span', { className: 'text-(--ui-text-quaternary) tabular-nums', children: [
              ratio > 0 ? `${ratio.toFixed(0)}× current usage` : '?'
            ]})
          ]}),
          jsxs('div', { className: 'text-(--ui-text-quaternary) mt-1', children: [
            'Ollama is ', ratio > 0 ? `${ratio.toFixed(0)}×` : '?', ' cheaper than pay-per-token this week'
          ]})
        ]
      }))
    }

    // ── monthly projection ──
    if (settings.monthly_proj && data.api_monthly_proj != null) {
      const saved_m = (data.api_monthly_proj ?? 0) - (data.ollama_monthly ?? 20)
      rows.push(jsx(Collapsible, {
        title: SECTION_LABELS.monthly_proj,
        defaultOpen: false,
        children: [
          jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
            jsx('span', { children: 'API projected / month' }),
            jsxs('span', { className: 'text-(--ui-text-secondary) tabular-nums', children: [
              '$', data.api_monthly_proj.toFixed(0)
            ]})
          ]}),
          jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
            jsx('span', { children: 'Ollama Pro / month' }),
            jsxs('span', { className: 'text-(--ui-text-secondary) tabular-nums', children: [
              '$', String(data.ollama_monthly ?? 20)
            ]})
          ]}),
          jsxs('div', { className: 'flex items-center justify-between gap-2 border-t border-(--ui-stroke-secondary) pt-1 mt-1 font-medium', children: [
            jsx('span', { children: 'You save / month' }),
            jsxs('span', { className: 'text-(--ui-text-primary)', children: [
              '$', saved_m.toFixed(0)
            ]})
          ]})
        ]
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
                ' · $', String(w.est_cost_consumed != null ? w.est_cost_consumed.toFixed(2) : '?'),
                w.top_model ? ` · ${w.top_model}` : ''
              ]})
            ]
          }, w.week + i)
        }),
        jsx('div', { className: 'text-(--ui-text-quaternary) mt-1', children: 'Weekly snapshots — kept locally, survives Ollama resets' }),
        settings.report && jsxs('div', { className: 'flex items-center gap-2 mt-2', children: [
          jsx('button', {
            className: cn(
              'rounded-md border px-2 py-1 text-xs',
              'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
            ),
            type: 'button',
            onClick: () => {
              haptic('tap')
              ctx.rest('/usage/report/open', { method: 'POST' }).then(r => {
                host.notify({ kind: r?.opened ? 'info' : 'warning', message: r?.opened ? `Report opened: ${r.path}` : `Report: ${r?.path}` })
              }).catch(() => host.notify({ kind: 'warning', message: 'Could not open report' }))
            },
            children: 'Open report (MD)'
          }),
          jsx('a', {
            className: 'text-xs text-(--ui-accent) hover:underline truncate',
            href: `file://${data?.report_path || '~/.hermes/ollama-usage-report.md'}`,
            children: '📄 full stats'
          })
        ]})
      ]
    }))
  }

  rows.push(jsx('button', {
    className: cn(
      'mt-3 self-start rounded-md border px-2 py-1 text-xs',
      'border-(--ui-stroke-secondary) text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)'
    ),
    type: 'button',
    onClick: refresh,
    children: 'Refresh'
  }))

  return jsx('div', { className: 'flex h-full flex-col gap-2 p-3 text-sm overflow-y-auto', children: rows })
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