/**
 * Offline behavioral regression tests: node --test tests/test_frontend.mjs
 *
 * Evaluate the complete desktop plugin with only its three import surfaces
 * stubbed. Exercise registered chip/pane/palette handlers, not copied helpers.
 * The small hook/query harness verifies orchestration and element props, NOT
 * browser layout, real React scheduling, or visual tooltip opacity.
 * No dependencies, network, credentials, installation, or desktop required.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const PLUGIN_URL = new URL('../desktop/plugin.js', import.meta.url)
const FETCHED_AT = '2026-10-01T10:00:00+00:00'
const FRESH = {
  ok: true, stale: false, cached: false, fetched_at: FETCHED_AT,
  plan: 'Pro', session_used_pct: 12.4, weekly_used_pct: 34.6,
  source: 'cookie', session_reset: '2h', weekly_reset: '3d',
  session_models: [], weekly_models: []
}
const clean = value => JSON.parse(JSON.stringify(value))
const deferred = () => {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function makeAtom(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set(next) { value = next; for (const listener of listeners) listener(next) },
    listen(listener) { listeners.add(listener); return () => listeners.delete(listener) }
  }
}

function nodes(tree, predicate = () => true) {
  if (Array.isArray(tree)) return tree.flatMap(child => nodes(child, predicate))
  if (!tree || typeof tree !== 'object') return []
  return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)]
}
function text(tree) {
  if (Array.isArray(tree)) return tree.map(text).join('')
  if (tree == null || typeof tree === 'boolean') return ''
  if (typeof tree !== 'object') return String(tree)
  return text(tree.props?.children)
}
function button(tree, label) {
  const result = nodes(tree, node => node.type === 'button' && text(node) === label)
  assert.equal(result.length, 1, `Expected one ${label} button`)
  return result[0]
}

function harness({ connectionAtom = true, source = 'ollama-test-source' } = {}) {
  const profile = makeAtom('default')
  const connection = makeAtom('local')
  const cache = new Map(), overrides = new Map(), contributions = new Map()
  const requests = [], invalidations = [], cancellations = [], notifications = []
  const queryOptions = [], subscriptions = [], haptics = [], cacheListeners = new Set()
  let active = null
  let respond = () => Promise.resolve({ ...FRESH })

  const scope = () => [source, profile.get(), connectionAtom ? connection.get() : profile.get()]
  const key = name => [...scope(), name]
  const cacheKey = queryKey => JSON.stringify(queryKey)
  const qc = {
    getQueryData(queryKey) { return cache.get(cacheKey(queryKey)) },
    setQueryData(queryKey, updater) {
      const id = cacheKey(queryKey)
      const next = typeof updater === 'function' ? updater(cache.get(id)) : updater
      if (next !== undefined) cache.set(id, next)
      overrides.delete(id)
      for (const listener of cacheListeners) listener()
      return next
    },
    cancelQueries(options) { cancellations.push(clean(options)); return Promise.resolve() },
    invalidateQueries(options) { invalidations.push(clean(options)); return Promise.resolve() }
  }
  const host = {
    state: { profile, ...(connectionAtom ? { connectionId: connection } : {}) },
    notify(notification) { notifications.push(clean(notification)) }
  }
  const ctx = {
    source,
    register(contribution) { contributions.set(contribution.id, contribution) },
    storage: { get: () => null, set() {} },
    os: { openExternal() {} },
    rest(path, options) {
      requests.push({ path, options: clean(options || {}), scope: scope() })
      if (path === '/usage/refresh') return respond(path, options)
      if (path === '/usage/report') return Promise.resolve({ ok: true, months: [] })
      if (path === '/usage/plan') return Promise.resolve({ ok: true, plan: 'Pro' })
      if (path === '/usage') return Promise.resolve(qc.getQueryData(key('usage')))
      if (path === '/usage/history') return Promise.resolve({ ok: true, weeks: [] })
      if (path === '/usage/lifetime') return Promise.resolve({ ok: true, models: [] })
      if (path === '/usage/lifetime-break-even') return Promise.resolve({ ok: true, models: [] })
      throw new Error(`Unstubbed REST request: ${path}`)
    }
  }
  const jsx = (type, props, key) => ({ type, props: props || {}, key })
  const react = {
    useState(initial) {
      assert.ok(active, 'useState outside a component')
      const { instance, root } = active
      const index = instance.index++
      if (!(index in instance.slots)) {
        instance.slots[index] = typeof initial === 'function' ? initial() : initial
      }
      return [instance.slots[index], next => {
        const value = typeof next === 'function' ? next(instance.slots[index]) : next
        if (value !== instance.slots[index]) { instance.slots[index] = value; root.dirty = true }
      }]
    },
    useEffect(effect, dependencies) {
      const { instance, root } = active
      const index = instance.index++
      const previous = instance.slots[index]
      if (!previous || !dependencies || dependencies.some((item, i) => item !== previous.dependencies[i])) {
        previous?.cleanup?.()
        const slot = { dependencies }
        instance.slots[index] = slot
        root.effects.push(() => {
          const cleanup = effect()
          if (typeof cleanup === 'function') slot.cleanup = cleanup
          return cleanup
        })
      }
    }
  }
  const sdk = {
    atom: makeAtom,
    cn: (...parts) => parts.filter(Boolean).join(' '),
    haptic: value => haptics.push(value),
    host, PALETTE_AREA: 'palette', queryClient: qc, Tip: 'tip',
    useQueryClient: () => qc,
    useValue(atom) {
      assert.ok(active, 'useValue outside a component')
      subscriptions.push(atom)
      const { root } = active
      if (!root.subscribed.has(atom)) {
        root.subscribed.add(atom)
        root.cleanup.push(atom.listen(() => { root.dirty = true }))
      }
      return atom.get()
    },
    useQuery(options) {
      assert.ok(active, 'useQuery outside a component')
      queryOptions.push(options)
      const id = cacheKey(options.queryKey)
      const data = cache.get(id)
      return {
        data, isLoading: data === undefined, isError: false, error: null,
        ...overrides.get(id),
        refetch() { throw new Error('Manual refresh must not use the cached /usage query') }
      }
    }
  }

  // Fresh full source read for each isolated test. Only imports/export syntax
  // are adapted; implementation bodies and registered callbacks are unchanged.
  const seenImports = []
  const modules = { '@hermes/plugin-sdk': sdk, react, 'react/jsx-runtime': { jsx, jsxs: jsx } }
  let sourceText = readFileSync(PLUGIN_URL, 'utf8').replace(
    /^import \{([^}]+)\} from '([^']+)'\s*$/gm,
    (_, names, specifier) => {
      assert.ok(specifier in modules, `Unexpected plugin import: ${specifier}`)
      seenImports.push(specifier)
      return `const {${names}} = __modules[${JSON.stringify(specifier)}]\n`
    }
  )
  assert.deepEqual(seenImports, ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
  assert.match(sourceText, /export default /)
  sourceText = sourceText.replace('export default ', 'const plugin = ')
  const plugin = vm.runInNewContext(`${sourceText}\n;plugin`, {
    __modules: modules,
    localStorage: { getItem: () => null, setItem() {} }
  }, { filename: PLUGIN_URL.pathname })
  plugin.register(ctx)

  function mount(id) {
    const contribution = contributions.get(id)
    assert.ok(contribution?.render, `Missing ${id} render contribution`)
    const root = {
      instances: new Map(), subscribed: new Set(), effects: [], cleanup: [], dirty: true,
      tree: null,
      render() {
        let passes = 0
        do {
          assert.ok(++passes <= 10, 'Mock render did not settle')
          root.dirty = false
          root.effects = []
          root.tree = renderNode(contribution.render(), 'root')
          for (const effect of root.effects) {
            const cleanup = effect()
            if (typeof cleanup === 'function') root.cleanup.push(cleanup)
          }
        } while (root.dirty)
        return root.tree
      }
    }
    const listener = () => { root.dirty = true }
    cacheListeners.add(listener)
    root.cleanup.push(() => cacheListeners.delete(listener))
    function renderNode(node, path) {
      if (Array.isArray(node)) return node.map((child, i) => renderNode(child, `${path}.${i}`))
      if (!node || typeof node !== 'object') return node
      if (typeof node.type === 'function') {
        let instance = root.instances.get(path)
        if (!instance || instance.type !== node.type) {
          instance = { type: node.type, index: 0, slots: [] }
          root.instances.set(path, instance)
        }
        instance.index = 0
        const previous = active
        active = { instance, root }
        let result
        try { result = node.type(node.props) } finally { active = previous }
        return renderNode(result, `${path}.render`)
      }
      return { ...node, props: { ...node.props, children: renderNode(node.props.children, `${path}.children`) } }
    }
    root.render()
    return root
  }
  function entrypoint(id) {
    if (id === 'palette') return { run: contributions.get('refresh').data.run }
    const root = mount(id)
    const control = id === 'chip'
      ? nodes(root.tree, node => node.type === 'button')[0]
      : button(root.tree, 'Refresh')
    return { root, run: control.props.onClick }
  }
  return {
    ctx, profile, connection, qc, scope, key, requests, invalidations, cancellations,
    notifications, contributions, subscriptions, queryOptions, haptics, mount, entrypoint,
    setResponse(fn) { respond = fn },
    seed(data, name = 'usage') { qc.setQueryData(key(name), data) },
    setQueryState(state, name = 'usage') { overrides.set(cacheKey(key(name)), state) },
    forcedRequests() { return requests.filter(request => request.path === '/usage/refresh') }
  }
}

function assertRefreshRequest(h, expectedScope = h.scope()) {
  assert.equal(h.forcedRequests().length, 1)
  const request = h.forcedRequests()[0]
  assert.equal(request.options.method, 'GET')
  assert.ok(request.options.timeoutMs >= 60000)
  assert.deepEqual(request.scope, expectedScope)
  assert.deepEqual(h.cancellations, [{ queryKey: [...expectedScope, 'usage'], exact: true }])
}
function assertRelatedInvalidations(h, expectedScope = h.scope()) {
  assert.deepEqual(h.invalidations, ['history', 'lifetime', 'lifetime-break-even'].map(name => ({
    queryKey: [...expectedScope, name], exact: true
  })))
}
function assertWarning(h, detail) {
  assert.equal(h.notifications.length, 1)
  assert.equal(h.notifications[0].kind, 'warning')
  assert.match(h.notifications[0].message, /refresh failed/)
  if (detail) assert.ok(h.notifications[0].message.includes(detail))
  assert.ok(h.notifications.every(notification => !notification.message.includes('usage refreshed')))
}

for (const id of ['chip', 'pane', 'palette']) {
  test(`${id}: real handler forces GET, updates scoped usage, invalidates related queries, then announces success`, async () => {
    const h = harness()
    h.seed({ ...FRESH, session_used_pct: 1, fetched_at: 'old' })
    const reply = { ...FRESH, session_used_pct: 45, fetched_at: '2026-10-01T10:05:00+00:00' }
    const pending = deferred()
    h.setResponse(() => pending.promise)
    const entry = h.entrypoint(id)
    const task = entry.run()
    assertRefreshRequest(h)
    assert.equal(h.notifications.length, 0, 'No success before the network response')
    pending.resolve(reply)
    assert.equal(await task, reply)
    assert.equal(h.qc.getQueryData(h.key('usage')), reply)
    assertRelatedInvalidations(h)
    assert.deepEqual(h.notifications, [{ kind: 'info', message: 'Ollama usage refreshed' }])
  })

  test(`${id}: HTTP-success stale payload preserves fetched time and warns without false success`, async () => {
    const h = harness()
    h.seed(FRESH)
    const stale = { ...FRESH, stale: true, error: 'fetch_failed', detail: 'Cookie source returned HTTP 401.' }
    h.setResponse(() => Promise.resolve(stale))
    await h.entrypoint(id).run()
    assertRefreshRequest(h)
    assert.equal(h.qc.getQueryData(h.key('usage')), stale)
    assert.equal(h.qc.getQueryData(h.key('usage')).fetched_at, FETCHED_AT)
    assert.deepEqual(h.invalidations, [], 'Failed refresh must not refetch unchanged summaries')
    assertWarning(h, stale.detail)
  })

  test(`${id}: ok:false without a last-good snapshot shows safe backend detail, not success`, async () => {
    const h = harness()
    const failure = { ok: false, error: 'fetch_failed', detail: 'Usage API returned HTTP 503.' }
    h.setResponse(() => Promise.resolve(failure))
    await h.entrypoint(id).run()
    assertRefreshRequest(h)
    assert.equal(h.qc.getQueryData(h.key('usage')), failure)
    assert.deepEqual(h.invalidations, [], 'Failed refresh must not refetch unchanged summaries')
    assertWarning(h, failure.detail)
    assert.match(text(h.mount('pane').tree), /Usage API returned HTTP 503/)
    const chip = h.mount('chip').tree
    assert.match(text(chip), /Ollama: n\/a/)
    assert.match(chip.props.label.props.children, /Usage API returned HTTP 503/)
  })

  test(`${id}: rejected transport keeps last-good data and never leaks exception text`, async () => {
    const h = harness()
    h.seed(FRESH)
    h.setResponse(() => Promise.reject(new Error('sensitive-transport-diagnostic-must-not-display')))
    await h.entrypoint(id).run()
    assertRefreshRequest(h)
    const snapshot = h.qc.getQueryData(h.key('usage'))
    assert.equal(snapshot.ok, true)
    assert.equal(snapshot.stale, true)
    assert.equal(snapshot.fetched_at, FETCHED_AT)
    assert.equal(snapshot.session_used_pct, FRESH.session_used_pct)
    assert.deepEqual(h.invalidations, [], 'Failed refresh must not refetch unchanged summaries')
    assertWarning(h, 'Could not reach the Ollama usage backend')
    assert.doesNotMatch(JSON.stringify(h.notifications), /sensitive-transport/)
    assert.doesNotMatch(text(h.mount('pane').tree), /sensitive-transport/)
    assert.match(text(h.mount('chip').tree), /⚠/)
  })
}

test('all three entrypoints dedupe the same pending request and both controls reactively disable', async () => {
  const h = harness()
  h.seed(FRESH)
  const chip = h.entrypoint('chip'), pane = h.entrypoint('pane'), palette = h.entrypoint('palette')
  const response = deferred()
  h.setResponse(() => response.promise)
  const one = chip.run(), two = pane.run(), three = palette.run(), repeat = chip.run()
  assert.equal(one, two)
  assert.equal(one, three)
  assert.equal(one, repeat)
  assertRefreshRequest(h)
  assert.equal(h.haptics.length, 1)
  assert.equal(chip.root.dirty, true, 'Chip subscribed to shared pending atom')
  assert.equal(pane.root.dirty, true, 'Pane subscribed to shared pending atom')
  const chipButton = nodes(chip.root.render(), node => node.type === 'button')[0]
  const paneButton = button(pane.root.render(), 'Refreshing…')
  assert.equal(chipButton.props.disabled, true)
  assert.equal(chipButton.props['aria-busy'], true)
  assert.equal(paneButton.props.disabled, true)
  assert.equal(paneButton.props['aria-busy'], true)
  response.resolve({ ...FRESH })
  await one
  assert.equal(h.notifications.length, 1)
  assert.equal(nodes(chip.root.render(), node => node.type === 'button')[0].props.disabled, false)
  assert.equal(button(pane.root.render(), 'Refresh').props.disabled, false)
  await palette.run()
  assert.equal(h.forcedRequests().length, 2, 'Dedupe releases after settlement')
})

test('failure settlement releases pending and dedupe so a later fresh retry succeeds', async () => {
  const h = harness()
  h.seed(FRESH)
  const pane = h.entrypoint('pane'), chip = h.entrypoint('chip')
  const response = deferred()
  h.setResponse(() => response.promise)
  const task = pane.run()
  assert.equal(button(pane.root.render(), 'Refreshing…').props.disabled, true)
  response.reject(new Error('offline'))
  await task
  assert.equal(button(pane.root.render(), 'Refresh').props.disabled, false)
  assert.equal(nodes(chip.root.render(), node => node.type === 'button')[0].props.disabled, false)
  h.setResponse(() => Promise.resolve({ ...FRESH }))
  await pane.run()
  assert.equal(h.forcedRequests().length, 2)
  assert.equal(h.qc.getQueryData(h.key('usage')).stale, false)
  assert.equal(h.notifications[1].kind, 'info')
  assert.doesNotMatch(text(chip.root.render()), /stale/)
  assert.equal(nodes(pane.root.render(), node => node.props.role === 'status').length, 0)
})

for (const [label, state, snapshot] of [
  ['backend stale', {}, { ...FRESH, stale: true, error: 'fetch_failed', detail: 'Cookie source unavailable.' }],
  ['React Query connection failure retaining data', { isError: true, error: new Error('private-error') }, FRESH]
]) {
  test(`${label}: warning chip and pane retain usage, timestamp and flat newline tooltip`, () => {
    const h = harness()
    h.seed(snapshot)
    h.setQueryState(state)
    const chip = h.mount('chip').tree
    assert.match(text(chip), /⚠$/)
    assert.match(text(chip), /S 12% \/ W 35%/)
    const labelNode = chip.props.label
    assert.equal(labelNode.type, 'span')
    assert.equal(typeof labelNode.props.children, 'string', 'Exactly one flat text child')
    assert.match(labelNode.props.className, /whitespace-pre-line/)
    assert.doesNotMatch(labelNode.props.className, /flex|gap-/)
    assert.match(labelNode.props.children, /Freshness: stale/)
    assert.ok(labelNode.props.children.includes(`Fetched: ${FETCHED_AT}`))
    assert.ok(labelNode.props.children.includes('\nSession resets 2h\nWeekly resets 3d'))
    const pane = h.mount('pane').tree
    const warning = nodes(pane, node => node.props.role === 'status')
    assert.equal(warning.length, 1)
    assert.match(warning[0].props.className, /--ui-badge-warning/)
    assert.match(text(warning[0]), /Stale — showing the last successful snapshot/)
    assert.ok(text(warning[0]).includes(FETCHED_AT))
    assert.match(text(pane), /12\.4% used/)
    assert.doesNotMatch(text(pane) + labelNode.props.children, /private-error/)
  })
}

test('status chip matches Codex typography, spacing, colors and compact label layout', () => {
  const h = harness()
  h.seed(FRESH)
  const chip = h.mount('chip').tree
  const control = nodes(chip, node => node.type === 'button')[0]
  assert.equal(text(control), 'Ollama Pro · S 12% / W 35%')
  assert.deepEqual(clean(control.props.style), {
    height: '100%', padding: '0 6px', fontSize: '11px', whiteSpace: 'nowrap',
    color: 'var(--ui-text-secondary)'
  })
  assert.equal(typeof control.props.children, 'string')
  assert.match(control.props['aria-label'], /percentages used \(session \/ weekly\)/)
  h.seed({ ...FRESH, stale: true })
  const staleControl = nodes(h.mount('chip').tree, node => node.type === 'button')[0]
  assert.equal(text(staleControl), 'Ollama Pro · S 12% / W 35% ⚠')
  assert.equal(staleControl.props.style.color, 'var(--ui-badge-warning)')
  h.seed({ ...FRESH, plan: null, session_used_pct: null, weekly_used_pct: null })
  assert.equal(text(h.mount('chip').tree), 'Ollama · S n/a / W n/a')
})

test('fresh/cached tooltip reports the original fetched timestamp without a stale warning', () => {
  const h = harness()
  for (const cached of [false, true]) {
    h.seed({ ...FRESH, cached })
    const chip = h.mount('chip').tree
    assert.match(chip.props.label.props.children, cached ? /Freshness: cached snapshot/ : /Freshness: latest successful snapshot/)
    assert.ok(chip.props.label.props.children.includes(FETCHED_AT))
    assert.doesNotMatch(text(chip), /stale/)
    assert.equal(nodes(h.mount('pane').tree, node => node.props.role === 'status').length, 0)
  }
})

test('connection failure without data renders unavailable rather than an endless loading label', () => {
  const h = harness()
  h.setQueryState({ isLoading: false, isError: true, error: new Error('private-error') })
  const chip = h.mount('chip').tree
  assert.match(text(chip), /Ollama: n\/a/)
  assert.match(chip.props.label.props.children, /Freshness: unavailable/)
  assert.match(chip.props.label.props.children, /Fetched: unknown/)
  assert.match(text(h.mount('pane').tree), /Could not reach the Ollama usage backend/)
  assert.doesNotMatch(chip.props.label.props.children, /private-error/)
})

test('backend ok:false and empty replies do not discard a retained successful snapshot', async () => {
  for (const reply of [{ ok: false, error: 'fetch_failed', detail: 'Usage unavailable.' }, undefined]) {
    const h = harness()
    h.seed(FRESH)
    h.setResponse(() => Promise.resolve(reply))
    await h.entrypoint('palette').run()
    const snapshot = h.qc.getQueryData(h.key('usage'))
    assert.equal(snapshot.ok, true)
    assert.equal(snapshot.stale, true)
    assert.equal(snapshot.fetched_at, FETCHED_AT)
    assertWarning(h)
  }
})

test('untrusted error identifiers and synchronous transport errors use safe fallback text', async () => {
  for (const response of [
    () => Promise.resolve({ ok: false, error: 'private-error-field' }),
    () => { throw new Error('private-sync-transport-error') }
  ]) {
    const h = harness()
    h.setResponse(response)
    await h.entrypoint('palette').run()
    assertWarning(h)
    const chip = h.mount('chip').tree
    assert.doesNotMatch(JSON.stringify(h.notifications) + chip.props.label.props.children + text(h.mount('pane').tree), /private-/)
  }
})

test('profile and connection atoms reactively scope all four query hooks, preserving literal identifiers', async () => {
  const h = harness()
  h.seed(FRESH)
  const pane = h.mount('pane')
  const names = ['usage', 'history', 'lifetime', 'lifetime-break-even']
  const assertKeys = () => {
    const actual = new Map(h.queryOptions.map(options => [options.queryKey.at(-1), clean(options.queryKey)]))
    for (const name of names) assert.deepEqual(actual.get(name), h.key(name))
    assert.ok(h.subscriptions.includes(h.profile))
    assert.ok(h.subscriptions.includes(h.connection))
  }
  assertKeys()
  h.queryOptions.length = 0
  h.profile.set(' profile:unchanged ')
  h.connection.set('remote:source-B')
  assert.equal(pane.dirty, true)
  h.seed({ ...FRESH, plan: 'Max', weekly_used_pct: 77 })
  assert.match(text(pane.render()), /Max plan/)
  assertKeys()
  const options = new Map(h.queryOptions.map(option => [option.queryKey.at(-1), option]))
  for (const name of names) await options.get(name).queryFn()
  assert.deepEqual(h.requests.filter(request => ['/usage', '/usage/history', '/usage/lifetime', '/usage/lifetime-break-even'].includes(request.path)).map(request => request.scope), names.map(() => h.scope()))
})

test('older SDKs without connectionId still subscribe and scope by profile', async () => {
  const h = harness({ connectionAtom: false })
  h.seed(FRESH)
  const pane = h.mount('pane')
  assert.deepEqual(clean(h.queryOptions.find(option => option.queryKey.at(-1) === 'usage').queryKey), ['ollama-test-source', 'default', 'default', 'usage'])
  h.profile.set('secondary')
  h.seed(FRESH)
  assert.equal(pane.dirty, true)
  pane.render()
  await h.entrypoint('palette').run()
  assertRefreshRequest(h, ['ollama-test-source', 'secondary', 'secondary'])
})

test('handlers mounted before a profile/connection switch read the imperative current owner', async () => {
  for (const id of ['chip', 'pane', 'palette']) {
    const h = harness()
    h.seed(FRESH)
    const entry = h.entrypoint(id)
    const previousKey = h.key('usage')
    h.profile.set('other-profile')
    h.connection.set('remote:other')
    const result = { ...FRESH, plan: 'Max' }
    h.setResponse(() => Promise.resolve(result))
    await entry.run()
    assertRefreshRequest(h)
    assertRelatedInvalidations(h)
    assert.equal(h.qc.getQueryData(h.key('usage')), result)
    assert.equal(h.qc.getQueryData(previousKey), FRESH)
  }
})

test('pending/dedupe and late response cache writes remain isolated across same-profile connections', async () => {
  const h = harness()
  h.seed(FRESH)
  const entry = h.entrypoint('chip')
  const firstOwner = h.scope(), firstKey = h.key('usage')
  const first = deferred(), second = deferred()
  h.setResponse(() => h.forcedRequests().length === 1 ? first.promise : second.promise)
  const taskA = entry.run()
  h.connection.set('remote:second')
  const secondOwner = h.scope(), secondKey = h.key('usage')
  h.seed({ ...FRESH, plan: 'Max' })
  assert.equal(nodes(entry.root.render(), node => node.type === 'button')[0].props.disabled, false)
  const taskB = h.entrypoint('palette').run()
  assert.notEqual(taskA, taskB)
  assert.equal(h.forcedRequests().length, 2)
  assert.equal(nodes(entry.root.render(), node => node.type === 'button')[0].props.disabled, true)
  first.resolve({ ...FRESH, weekly_used_pct: 10 })
  await taskA
  assert.equal(h.qc.getQueryData(firstKey).weekly_used_pct, 10)
  assert.equal(h.qc.getQueryData(secondKey).plan, 'Max')
  assert.equal(nodes(entry.root.render(), node => node.type === 'button')[0].props.disabled, true)
  second.resolve({ ...FRESH, plan: 'Max', weekly_used_pct: 80 })
  await taskB
  assert.equal(h.qc.getQueryData(secondKey).weekly_used_pct, 80)
  assert.equal(nodes(entry.root.render(), node => node.type === 'button')[0].props.disabled, false)
  assert.deepEqual(h.invalidations, [firstOwner, secondOwner].flatMap(owner => ['history', 'lifetime', 'lifetime-break-even'].map(name => ({ queryKey: [...owner, name], exact: true }))))
})

test('old-owner query retries/invalidations fail closed rather than fetching the new owner into an old key', async () => {
  const h = harness()
  h.seed(FRESH)
  h.mount('pane')
  const oldOptions = new Map(h.queryOptions.map(option => [option.queryKey.at(-1), option]))
  const before = h.requests.length
  h.connection.set('remote:new-owner')
  for (const options of oldOptions.values()) await assert.rejects(options.queryFn(), /owner changed/)
  assert.equal(h.requests.length, before, 'No ambient REST calls for retired query keys')
})

test('pane reloads report/plan metadata for new owners and ignores old-owner replies', async () => {
  const h = harness()
  h.seed(FRESH)
  const previousRest = h.ctx.rest
  const oldPlan = deferred(), oldReport = deferred()
  h.ctx.rest = (path, options) => {
    if (h.profile.get() === 'default' && path === '/usage/plan') return oldPlan.promise
    if (h.profile.get() === 'default' && path === '/usage/report') return oldReport.promise
    return previousRest(path, options)
  }
  const pane = h.mount('pane')
  h.profile.set('new-owner')
  h.seed(FRESH)
  pane.render()
  await Promise.resolve()
  await Promise.resolve()
  pane.render()
  oldPlan.resolve({ ok: true, plan: 'Max' })
  oldReport.resolve({ ok: true, months: [{ month: 'old-secret-month', path: 'old-owner-path' }] })
  await Promise.resolve()
  await Promise.resolve()
  button(pane.render(), '⚙').props.onClick()
  const tree = pane.render()
  assert.match(button(tree, 'Pro').props.className, /--ui-accent/)
  assert.doesNotMatch(button(tree, 'Max').props.className, /--ui-accent/)
  assert.doesNotMatch(text(tree), /old-secret-month|old-owner-path/)
  assert.ok(h.requests.some(request => request.path === '/usage/report' && request.scope[1] === 'new-owner'))
  assert.ok(h.requests.some(request => request.path === '/usage/plan' && request.scope[1] === 'new-owner'))
})

test('minimal economics, per-model cost/cache details, percentages, settings and other controls are preserved', () => {
  const h = harness()
  const model = {
    model: 'fixture-model', requests: 20, share_pct: 100,
    api_session_cost: 1.25, api_session_cost_cached: 0.75,
    api_weekly_cost: 4.5, api_weekly_cost_cached: 2.5, api_real_cache_pct: 40,
    api_effective_per_1m: 3, api_effective_per_1m_cached: 2,
    plan_effective_per_1m: 1, plan_pct_of_api: 33, plan_pct_of_api_cached: 50,
    api_break_even_cache_pct: 84, cache_hit_pct: 40
  }
  h.seed({ ...FRESH, api_session_total: 1.25, api_window_total: 4.5, session_models: [model], weekly_models: [model] })
  h.seed({ ok: true, api_lifetime_total: 9, api_lifetime_total_cached: 5, weeks_count: 2,
    models: [{ ...model, api_lifetime_cost: 9, api_lifetime_cost_cached: 5 }] }, 'lifetime-break-even')
  const pane = h.mount('pane')
  const costHeader = button(pane.tree, 'API equivalent cost▾')
  const costSection = nodes(pane.tree, node => node.type === 'div' && node.props.children?.[0] === costHeader)[0]
  assert.ok(costSection)
  const costText = text(costSection)
  for (const expected of ['Session: $1.2500', 'Weekly:  $4.5000', 'Lifetime: $9.0000', 'fixture-model', 'with cache (40%): $0.7500', 'with cache (40%): $2.5000', 'with cache (40%): $5.0000']) {
    assert.ok(costText.includes(expected), `Missing ${expected}`)
  }
  assert.doesNotMatch(costText, /price coverage|API input\/cache\/output|Prices:|Break-even/)
  assert.match(text(pane.tree), /33%/)
  button(pane.tree, 'Cache break-even▸').props.onClick()
  assert.match(text(pane.render()), /API cheaper above 84% cache hit/)
  button(pane.tree, 'Lifetime break-even & price comparison▸').props.onClick()
  assert.match(text(pane.render()), /API cheaper above 84% cache hit/)
  assert.ok(button(pane.tree, 'Dashboard ↗'))
  button(pane.tree, '⚙').props.onClick()
  pane.render()
  assert.ok(button(pane.tree, 'Show all'))
  assert.ok(button(pane.tree, 'Hide all'))
  for (const name of ['Free', 'Pro', 'Max']) assert.ok(button(pane.tree, name))
  assert.ok(button(pane.tree, 'Done'))
})
