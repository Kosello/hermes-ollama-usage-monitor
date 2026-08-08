/**
 * Ollama Cloud Usage — Hermes desktop plugin.
 *
 * Shows Ollama Cloud session/weekly usage in a statusbar chip and a pane.
 * Data comes from the plugin Python backend at /api/plugins/ollama-usage/usage
 * (cookie-based scrape of ollama.com/settings, same as the /ollama slash command).
 *
 * Save as: ~/.hermes/desktop-plugins/ollama-usage/plugin.js
 * Then: ⌘K → Reload desktop plugins
 */

import { cn, haptic, host, PALETTE_AREA, Tip, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ollama-usage-monitor'
const REFRESH_MS = 60000 // poll backend every 60s

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
        onClick: () => {
          haptic('tap')
          setOpen(o => !o)
        },
        children: [
          jsx('span', { className: 'truncate', children: title }),
          jsx('span', { className: 'shrink-0 text-(--ui-text-quaternary) transition-transform', children: open ? '▾' : '▸' })
        ]
      }),
      open ? jsx('div', { className: 'mt-1 flex flex-col gap-0.5 text-xs', children }) : null
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
      onClick: () => {
        haptic('tap')
        host.notify({ kind: 'info', message: label })
      },
      children: jsx('span', { className: pctColor(session), children: label })
    })
  })
}

// ── pane ──────────────────────────────────────────────────────────────────
function UsagePane({ ctx }) {
  const { data, isLoading, refetch } = useUsage(ctx)
  const { data: hist } = useHistory(ctx)
  const qc = useQueryClient()

  const refresh = () => {
    haptic('tap')
    qc.invalidateQueries({ queryKey: [ctx.source, 'usage'] })
    refetch()
  }

  const rows = []
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
    rows.push(jsx('div', { className: 'font-medium', children: `Ollama Cloud — ${data.plan || 'Unknown'} plan` }))
    rows.push(jsx(Collapsible, {
      title: 'Limits',
      defaultOpen: true,
      children: [
        jsx('div', { className: 'flex items-center justify-between', children: [
          jsx('span', { children: 'Session' }),
          jsx('span', { className: pctColor(s), children: s != null ? `${s.toFixed(1)}% used · resets in ${data.session_reset || '?'}` : 'n/a' })
        ]}),
        jsx('div', { className: 'flex items-center justify-between', children: [
          jsx('span', { children: 'Weekly' }),
          jsx('span', { className: pctColor(w), children: w != null ? `${w.toFixed(1)}% used · resets in ${data.weekly_reset || '?'}` : 'n/a' })
        ]})
      ]
    }))
    if ((data.session_models && data.session_models.length > 0) || (data.weekly_models && data.weekly_models.length > 0)) {
      const segRows = (segs, withCost) => {
        if (!segs || segs.length === 0) return null
        return segs.map(m =>
          jsx('div', {
            className: 'flex items-center justify-between gap-2',
            children: [
              jsx('span', { className: 'truncate', children: m.model }),
              jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0 tabular-nums', children: [
                `${m.requests} req · ${m.share_pct != null ? m.share_pct.toFixed(1) : ''}%`,
                withCost && m.est_cost_per_req != null ? ` · $${m.est_cost_per_req.toFixed(4)} (${m.est_cost_per_req_pct != null ? m.est_cost_per_req_pct.toFixed(3) : ''}%/req)` : ''
              ]})
            ]
          }, m.model)
        )
      }
      const sessionRows = segRows(data.session_models, false)
      const weeklyRows = segRows(data.weekly_models, false)
      if (sessionRows) rows.push(jsx(Collapsible, { title: 'Session — per model', defaultOpen: true, children: sessionRows }))
      if (weeklyRows) rows.push(jsx(Collapsible, { title: 'Weekly — per model', defaultOpen: true, children: weeklyRows }))
    }

    // average cost per request per model
    if (data.weekly_models && data.weekly_models.length > 0 && data.est_weekly_budget != null) {
      rows.push(jsx(Collapsible, {
        title: 'Avg cost per request',
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
          jsx('div', {
            className: 'text-(--ui-text-quaternary) mt-1',
            children: [
              'Budget: $', String(data.est_weekly_budget),
              '/wk · consumed: $', String(data.est_cost_consumed),
              ' · avg $', String(data.est_avg_cost_per_req ?? ''),
              '/req across all models'
            ]
          })
        ]
      }))
    }
  }

  // Weekly history — last 8 weeks with trend vs previous week
  const weeks = hist?.weeks || []
  if (weeks.length > 0) {
    const arrow = trendArrow(weeks)
    rows.push(jsxs('div', {
      className: 'mt-3 border-t border-(--ui-stroke-secondary) pt-2 text-xs',
      children: [
        jsx('div', { className: 'font-medium text-(--ui-text-secondary) mb-1', children: 'Weekly history' }),
        jsx('div', {
          className: 'flex flex-col gap-0.5 tabular-nums',
          children: weeks.map((w, i) => {
            const pct = w.weekly_used_pct
            const isCur = i === 0
            return jsxs('div', {
              className: 'flex items-center justify-between gap-2',
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
          })
        }),
        jsx('div', { className: 'text-(--ui-text-quaternary) mt-1', children: 'Weekly snapshots — kept locally, survives Ollama resets' })
      ]
    }))
  }

  // Real API price comparison — at the complete bottom
  if (data && data.ok !== false && data.api_weekly_total != null && data.weekly_models && data.weekly_models.length > 0) {
    const known = data.weekly_models.filter(m => m.api_cost_per_req != null)
    rows.push(jsx(Collapsible, {
      title: 'vs. real API prices',
      defaultOpen: false,
      children: [
        known.map(m =>
          jsxs('div', {
            className: 'flex items-center justify-between gap-2 tabular-nums',
            children: [
              jsx('span', { className: 'truncate', children: m.model }),
              jsxs('span', { className: 'text-(--ui-text-secondary) shrink-0', children: [
                m.api_cost_pct != null ? `${m.api_cost_pct.toFixed(0)}% of API` : '?',
                ' · $', String(m.api_weekly_cost != null ? m.api_weekly_cost.toFixed(2) : '?'),
                '/wk'
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
              ' · Ollama $', String((data.est_cost_consumed ?? 0).toFixed(2)),
              data.api_total_pct != null ? ` (${data.api_total_pct.toFixed(0)}%)` : ''
            ]})
          ]
        }),
        jsx('div', { className: 'text-(--ui-text-quaternary) mt-1', children: `Rough: assumes ${data.api_assumption || '~1000 in + 500 out tokens/req'}` })
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
    // A layout pane — auto-placed right; user can drag it anywhere.
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Ollama Usage',
      data: { placement: 'right', width: '240px' },
      render: () => jsx(UsagePane, { ctx })
    })

    // A statusbar chip.
    ctx.register({
      id: 'chip',
      area: 'statusBar.right',
      order: 130,
      render: () => jsx(UsageChip, { ctx })
    })

    // ⌘K palette command to refresh usage.
    ctx.register({
      id: 'refresh',
      area: PALETTE_AREA,
      data: {
        id: 'ollama-usage.refresh',
        label: 'Refresh Ollama Cloud usage',
        keywords: ['ollama', 'usage', 'quota'],
        run: () => {
          haptic('tap')
          host.notify({ kind: 'info', message: 'Ollama usage refreshed (check the pane)' })
        }
      }
    })
  }
}
