const base = '/api/v1'

const qs = (o) => {
  const p = new URLSearchParams()
  Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') p.set(k, v) })
  return p.toString()
}

// ── connection state ──────────────────────────────────────────────────────────
// A dead or restarting backend used to leave the UI on a spinner forever: fetch
// has no timeout of its own, so a request that never answers never settles, and
// the loading flag never cleared. Every request now carries a deadline, and the
// result feeds a connection flag the shell can show. A spinner is never again
// allowed to be the whole story.
let _online = true
// WHY the backend is not answering: 'down' (nothing listening -- a restart)
// or 'slow' (it is up and answered a timeout ago). 2026-09-21: the banner
// said "it restarts on its own in a few seconds" for forty minutes while the
// backend was up the whole time and answering in 25-45 s -- a queue on the
// database lock, not a restart. Two different facts, two different messages.
let _offlineReason = 'down'
export function offlineReason() { return _offlineReason }
const _watchers = new Set()

export function isOnline() { return _online }

export function onConnection(cb) {
  _watchers.add(cb)
  return () => _watchers.delete(cb)
}

function _setOnline(v, reason) {
  if (reason) _offlineReason = reason
  if (v === _online) return
  _online = v
  _watchers.forEach((f) => { try { f(v) } catch { /* a bad listener must not break requests */ } })
}

// Research and backfill routes genuinely take a while; everything else that
// takes more than a few seconds is a fault, not slowness.
const TIMEOUT_MS = 20000
const SLOW_TIMEOUT_MS = 120000
const SLOW = /^\/(research|model-arena|strategies\/days|backfill|breakout\/train|tests\/run|macro\/(fetch|correlations)|library\/fetch|dayscan\/range|universe\/(review|selection)|robinhood\/(measure-spreads|sync-universe))/

// /scheduler/job/<name>/run blocks until the job FINISHES, and breakout_train
// takes 33 minutes. At the normal 20-second deadline the request aborted while
// the job carried on server-side, so "run now" looked like it did nothing at
// all. The job keeps running either way; this stops the UI giving up first.
const JOB_RUN = /^\/scheduler\/job\/[^/]+\/run/
const JOB_TIMEOUT_MS = 600000

// ── access token (only used when the app is reached over the network) ────────
// On the desktop this is inert: the backend never challenges loopback. From a
// phone it is required, and the flow is one-time — open the link the server
// printed once, the token is taken out of the URL, stored, and the URL cleaned
// so it is not left sitting in history or in a screenshot.
const TOKEN_KEY = 'tc_access_token'

function _standalone() {
  // iOS reports it two ways depending on version; check both.
  try {
    return window.navigator.standalone === true
      || window.matchMedia('(display-mode: standalone)').matches
  } catch {
    return false
  }
}

function _readToken() {
  try {
    const u = new URL(window.location.href)
    const fromUrl = u.searchParams.get('token')
    if (fromUrl) {
      localStorage.setItem(TOKEN_KEY, fromUrl)
      // THE TOKEN STAYS IN THE URL UNTIL THE APP IS INSTALLED, and this is the
      // whole trick on an iPhone.
      //
      // A home-screen web app on iOS gets its OWN storage container. It does not
      // share localStorage with Safari. So the obvious flow -- open the link in
      // Safari, store the token, clean the URL, then Add to Home Screen -- breaks
      // at the last step: the installed app launches a URL with no token, into a
      // storage container that has never seen one, and every request 401s.
      //
      // Leaving the token in the URL while still in Safari means Add to Home
      // Screen captures the full link, so the installed app launches WITH the
      // token, stores it in its own container, and only then cleans the URL.
      if (_standalone()) {
        u.searchParams.delete('token')
        window.history.replaceState({}, '', u.pathname + (u.search || '') + u.hash)
      }
      return fromUrl
    }
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

let _token = typeof window !== 'undefined' ? _readToken() : ''

export function accessToken() { return _token }
export function setAccessToken(t) {
  _token = t || ''
  try { if (_token) localStorage.setItem(TOKEN_KEY, _token); else localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
}
export function needsToken() { return _needsToken }
export function isStandalone() { return _standalone() }

// WHY the backend refused, because it decides what the user can do about it.
//
//   401 -> wrong or missing token. Pasting the right one fixes it.
//   403 -> the backend is bound to localhost and is refusing every outside
//          caller. NO token exists that would help.
//
// These were treated identically, so a phone hitting a localhost-only backend
// was shown a token box and told to paste a token — which it then ignored,
// because it was never going to check one. The advice was a dead end and the
// screen gave no way to tell.
let _needsToken = false
let _denyReason = ''
export function denyReason() { return _denyReason }

// Try a candidate token against a real, token-checked endpoint and report what
// actually happened. `save and reload` used to store whatever was typed and
// reload immediately, so a good token and a bad one produced the same thing: a
// page that came back with an empty box and no message.
export async function verifyToken(candidate) {
  const t = (candidate || '').trim()
  if (!t) return 'empty'
  let res
  try {
    res = await fetch(base + '/mode', { headers: { 'X-TC-Token': t } })
  } catch {
    return 'unreachable'
  }
  if (res.ok) return 'ok'
  if (res.status === 403) return 'closed'
  if (res.status === 401) return 'rejected'
  return 'unreachable'
}
const _tokenWatchers = new Set()
export function onTokenNeeded(cb) {
  _tokenWatchers.add(cb)
  return () => _tokenWatchers.delete(cb)
}

async function req(path, opts = {}) {
  const { timeoutMs, ...rest } = opts
  const limit = timeoutMs
    || (JOB_RUN.test(path) ? JOB_TIMEOUT_MS : SLOW.test(path) ? SLOW_TIMEOUT_MS : TIMEOUT_MS)
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), limit)

  let res
  try {
    res = await fetch(base + path, {
      headers: {
        'Content-Type': 'application/json',
        ...(_token ? { 'X-TC-Token': _token } : {}),
      },
      signal: ctl.signal,
      ...rest,
      body: rest.body ? JSON.stringify(rest.body) : undefined,
    })
  } catch (e) {
    if (e && e.name === 'AbortError') {
      _setOnline(false, 'slow')
      throw new Error(`no answer in ${Math.round(limit / 1000)}s — the backend is busy or restarting`)
    }
    _setOnline(false, 'down')
    throw new Error('cannot reach the backend')
  } finally {
    clearTimeout(timer)
  }

  // A 502/504 here is Vite's proxy telling us the backend is not listening.
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    _setOnline(false, 'down')
    throw new Error('the backend is not answering — it may be restarting')
  }
  if (res.status === 401 || res.status === 403) {
    const reason = res.status === 403 ? 'closed' : 'token'
    if (!_needsToken || _denyReason !== reason) {
      _needsToken = true
      _denyReason = reason
      _tokenWatchers.forEach((f) => { try { f(reason) } catch { /* ignore */ } })
    }
    _setOnline(true)
    const j = await res.json().catch(() => ({}))
    throw new Error(j.how || j.error || 'this device is not allowed to reach the backend')
  }
  if (_needsToken) {
    _needsToken = false
    _denyReason = ''
    _tokenWatchers.forEach((f) => { try { f('') } catch { /* ignore */ } })
  }
  _setOnline(true)

  const json = await res.json().catch(() => ({ success: false, message: res.statusText }))
  if (!res.ok || json.success === false) throw new Error(json.message || json.detail || 'request failed')
  return json.data
}

export const api = {
  // first run: demo or live
  modelArena: (strategy, withCandidates) => req(`/model-arena?strategy=${strategy}` + (withCandidates ? '&with_candidates=true' : '')),
  decisionTree: (strategy) => req('/decision-tree' + (strategy ? `?strategy=${strategy}` : '')),
  firstRun: () => req('/first_run'),
  firstRunDemo: () => req('/first_run/demo', { method: 'POST', body: {} }),
  firstRunLive: () => req('/first_run/live', { method: 'POST', body: {} }),
  firstRunClearDemo: () => req('/first_run/clear_demo', { method: 'POST', body: {} }),
  status: () => req('/system/status'),
  dayScan: (day, opts = {}) => req('/dayscan?' + qs({ day, tradable_only: opts.tradableOnly, as_of_hour: opts.asOfHour, hot_volume: opts.hotVolume })),
  dayScanRange: (start, end, tradableOnly, hotVolume) => req('/dayscan/range?' + qs({ start, end, tradable_only: tradableOnly, hot_volume: hotVolume })),
  dayScanDays: (limit) => req('/dayscan/days?' + qs({ limit })),
  mode: () => req('/mode'),
  setMode: (mode) => req('/mode', { method: 'POST', body: { mode } }),
  setup: () => req('/setup'),
  health: () => req('/health/report'),
  vault: () => req('/vault'),
  liveness: () => req('/liveness'),
  housekeepingPreview: () => req('/housekeeping/preview'),
  housekeepingRun: () => req('/housekeeping/run', { method: 'POST', body: {} }),
  housekeepingBackup: () => req('/housekeeping/backup', { method: 'POST', body: { reason: 'manual' } }),
  housekeepingBackups: () => req('/housekeeping/backups'),
  account: () => req('/account'),
  chartFor: (symbol, day, granularity) => req(`/chart/${symbol}?` + qs({ day, granularity })),
  closeAll: (reason) => req('/positions/close-all', { method: 'POST', body: { reason: reason || 'manual' } }),
  killSwitch: () => req('/risk/kill-switch'),
  paperReset: () => req('/paper/reset', { method: 'POST', body: {} }),
  accountBalance: (mode) => req('/account/balance' + (mode ? `?mode=${mode}` : '')),
  setAccount: (equity_usd, note) => req('/account', { method: 'POST', body: { equity_usd, note } }),

  // guided setup
  setupSteps: () => req('/setup/steps'),
  setupInstall: (pkg) => req('/setup/install', { method: 'POST', body: { package: pkg } }),
  setupKeypair: () => req('/setup/keypair', { method: 'POST', body: {} }),
  setupApiKey: (api_key) => req('/setup/api-key', { method: 'POST', body: { api_key } }),
  experiment: () => req('/experiment'),
  setExperiment: (enabled) => req('/experiment', { method: 'POST', body: { enabled } }),
  setupRecoveredDb: () => req('/setup/recovered-db'),
  setupRestoreDb: () => req('/setup/restore-db', { method: 'POST', body: {} }),
  setupRestart: () => req('/setup/restart', { method: 'POST', body: {} }),

  // robinhood REST API
  rhProbe: () => req('/robinhood/probe'),
  rhSyncUniverse: () => req('/robinhood/sync-universe', { method: 'POST', body: {} }),
  // The last recorded list check, readable without pressing anything. The four
  // tiles used to require a button press in that same browser session, and read
  // field names the pressed endpoint never returned.
  rhListCounts: () => req('/robinhood/list-counts'),
  // What moved enough to pay for itself today, and whether anything could see it.
  coverageToday: () => req('/coverage/today'),
  routing: (mode) => req('/routing?' + qs({ mode })),
  scorecard: () => req('/strategies/scorecard'),
  evolve: () => req('/evolve'),
  // Champion vs challenger: what retrained, what was refused, and why.
  retrain: () => req('/retrain'),
  retrainHistory: (strategy, limit) => req('/retrain/history?' + qs({ strategy, limit })),
  runRetrain: (strategy) => req('/retrain/run', { method: 'POST', body: { strategy } }),
  // What must be true of the data, and whether it currently is.
  invariants: () => req('/invariants'),
  pending: () => req('/pending'),
  exitLab: (start, end, strategy) => req('/exit-lab?' + qs({ start, end, strategy })),
  access: () => req('/access'),
  setPhoneAccess: (enabled) => req('/access/phone', { method: 'POST', body: { enabled } }),
  // How big the next position would be, and what is left after it.
  sizing: (mode) => req('/sizing?' + qs({ mode })),
  reportDay: (date) => req('/report/day?' + qs({ date })),
  reportRange: (start, end) => req('/report/range?' + qs({ start, end })),
  reportDays: (limit) => req('/report/days?' + qs({ limit })),
  // Raw samples from every upstream API, fetched live, nothing renamed.
  dataSources: () => req('/datasources'),
  dataSourceSample: (key, product) => req('/datasources/sample?' + qs({ key, product })),
  dataSourcePeek: (table) => req('/datasources/peek?' + qs({ table })),
  dataSourceQuery: (sql) => req('/datasources/query', { method: 'POST', body: { sql } }),
  rhMeasureSpreads: (symbols) => req('/robinhood/measure-spreads', { method: 'POST', body: { symbols } }),
  rhQuote: (symbol, quantity) => req(`/robinhood/quote?symbol=${symbol}&quantity=${quantity}`),
  rhRoutingCost: () => req('/robinhood/routing-cost'),
  rhFeeStatus: () => req('/robinhood/fee-status'),
  rhFeeTiers: (volume_usd = 0) => req(`/robinhood/fee-tiers?volume_usd=${volume_usd}`),

  // venue cost + price basis
  venues: () => req('/venues'),
  venueUpdate: (body) => req('/venues', { method: 'POST', body }),
  venueCompare: (gross_edge_bps, trades_per_day = 2, equity_usd = 500) =>
    req(`/venues/compare?${qs({ gross_edge_bps, trades_per_day, equity_usd })}`),
  priceBasis: (days = 90) => req(`/price-basis?days=${days}`),
  priceBasisRecord: (symbol, rh_mid, note) =>
    req('/price-basis', { method: 'POST', body: { symbol, rh_mid, note } }),

  // token ledger
  tokens: () => req('/tokens'),
  tokensSetProvider: (feature, provider) => req('/tokens/provider', { method: 'POST', body: { feature, provider } }),

  // test lab
  tests: () => req('/tests'),
  testsRun: (target = '') => req('/tests/run', { method: 'POST', body: { target } }),
  testsVerify: (check = '') => req('/tests/verify', { method: 'POST', body: { check } }),
  testsHistory: (limit = 40) => req(`/tests/history?limit=${limit}`),
  testsOutput: (run_id) => req(`/tests/output?run_id=${run_id}`),
  testsUserRead: (filename = '') => req(`/tests/user?filename=${encodeURIComponent(filename)}`),
  testsUserSave: (filename, source) => req('/tests/user', { method: 'POST', body: { filename, source } }),

  // robinhood spread
  rhSpreads: () => req('/cost/rh-spreads'),
  rhSpreadSet: (symbol, spread_pct, note) => req('/cost/rh-spreads', { method: 'POST', body: { symbol, spread_pct, note } }),
  costHurdle: (symbol) => req(`/cost/hurdle?symbol=${symbol}`),

  scheduler: () => req('/scheduler'),
  schedulerStart: () => req('/scheduler/start', { method: 'POST', body: {} }),
  schedulerStop: () => req('/scheduler/stop', { method: 'POST', body: {} }),
  schedulerPause: (paused) => req('/scheduler/pause', { method: 'POST', body: { paused } }),
  schedulerRun: (name) => req(`/scheduler/job/${name}/run`, { method: 'POST', body: {} }),
  schedulerEnable: (name, enabled) => req(`/scheduler/job/${name}/enabled`, { method: 'POST', body: { enabled } }),

  universeSelection: (dry = true) => req(`/universe/selection?dry_run=${dry}`),
  universeReview: () => req('/universe/review', { method: 'POST', body: {} }),
  universeHistory: (limit = 50) => req(`/universe/history?limit=${limit}`),

  macro: () => req('/macro'),
  macroFetch: () => req('/macro/fetch', { method: 'POST', body: {} }),
  macroCorrelations: (days = 365) => req(`/macro/correlations?days=${days}`),
  predictionMarkets: () => req('/prediction-markets'),
  predictionMarketsFetch: () => req('/prediction-markets/fetch', { method: 'POST' }),

  libraryFetch: () => req('/library/fetch', { method: 'POST', body: {} }),
  safety: () => req('/system/safety'),
  engineStart: (strategies) => req('/engine/start', { method: 'POST', body: { strategies } }),
  engineStop: () => req('/engine/stop', { method: 'POST' }),
  engineTick: () => req('/engine/tick', { method: 'POST', body: {} }),

  universe: () => req('/universe'),
  refreshUniverse: () => req('/universe/refresh', { method: 'POST' }),
  movers: (limit = 25) => req(`/movers?limit=${limit}`),

  cost: (symbol) => req(`/cost${symbol ? `?symbol=${symbol}` : ''}`),
  costSymbols: () => req('/cost/symbols'),
  costCoefficients: () => req('/cost/coefficients'),
  costBreakdown: (symbol) => req(`/cost/breakdown${symbol ? `?symbol=${symbol}` : ''}`),
  addCostObs: (body) => req('/cost/observation', { method: 'POST', body }),

  models: () => req('/models'),
  operatorStrategies: () => req('/strategies/operator'),
  regime: () => req('/regime'),
  flow: () => req('/flow'),
  seasonality: () => req('/research/seasonality'),
  rollingEntry: () => req('/research/rolling-entry'),
  strategyVersions: () => req('/strategies/versions'),
  regimes: () => req('/research/regimes'),
  entryLateness: () => req('/research/entry-lateness'),
  entryQuality: () => req('/research/entry-quality'),
  strategyDays: (start, end) => req(`/strategies/days?start=${start}&end=${end}`),
  dayShape: () => req('/research/day-shape'),
  modelLab: (strategy, wf = false) => req(`/research/model-lab/${strategy}?walk_forward=${wf}`),
  desk: (mode = 'advisory') => req(`/desk?mode=${mode}`),

  backfillStart: (granularity, days) => req('/backfill/start', { method: 'POST', body: { granularity, days } }),
  backfillCancel: () => req('/backfill/cancel', { method: 'POST', body: {} }),
  backfillStatus: () => req('/backfill/status'),
  coverage: () => req('/backfill/coverage'),
  storage: (symbols = 80) => req(`/backfill/storage?symbols=${symbols}`),
  schema: () => req('/schema'),
  architecture: () => req('/architecture'),
  threads: () => req('/system/threads'),
  rawData: (symbol, granularity, start_ts, end_ts) => {
    const q = new URLSearchParams({ symbol, granularity })
    if (start_ts) q.set('start_ts', start_ts)
    if (end_ts) q.set('end_ts', end_ts)
    return req(`/data/raw?${q}`)
  },

  attention: () => req('/attention'),

  breakoutStatus: () => req('/breakout/status'),
  breakoutReport: () => req('/breakout/report'),
  breakoutTrain: () => req('/breakout/train', { method: 'POST', body: { force: true } }),
  breakoutVeto: (symbol) => req(`/breakout/veto/${symbol}`),
  breakoutThreshold: (threshold) => req('/breakout/threshold', { method: 'POST', body: { threshold } }),
  breakoutEvents: (symbol) => req(`/breakout/events/${symbol}`),

  news: (limit = 60, symbol) => req(`/news?limit=${limit}${symbol ? `&symbol=${symbol}` : ''}`),
  newsFetch: () => req('/news/fetch', { method: 'POST', body: {} }),
  newsAlerts: (hours = 24) => req(`/news/alerts?hours=${hours}`),
  library: (status, tag) => {
    const q = new URLSearchParams()
    if (status) q.set('status', status)
    if (tag) q.set('tag', tag)
    return req(`/library?${q}`)
  },
  attentionPersistence: () => req('/attention/persistence'),

  budgets: (mode) => req(`/budget${mode ? `?mode=${mode}` : ''}`),
  createBudget: (body) => req('/budget', { method: 'POST', body }),
  cancelBudget: (id) => req(`/budget/${id}/cancel`, { method: 'POST', body: {} }),

  ledger: (strategy, mode, start_ts, end_ts) => {
    const q = new URLSearchParams({ mode })
    if (start_ts) q.set('start_ts', start_ts)
    if (end_ts) q.set('end_ts', end_ts)
    return req(`/ledger/${strategy}?${q}`)
  },
  ledgerCompare: (mode, start_ts, end_ts) => {
    const q = new URLSearchParams({ mode })
    if (start_ts) q.set('start_ts', start_ts)
    if (end_ts) q.set('end_ts', end_ts)
    return req(`/ledger/compare?${q}`)
  },
  ledgerDaily: (mode, days = 30) => req(`/ledger/daily?mode=${mode}&days=${days}`),
  deskSkip: (cid) => req(`/desk/${cid}/skip`, { method: 'POST', body: {} }),
  reportFill: (cid, body) => req(`/orders/${cid}/fill`, { method: 'POST', body }),
  strategies: () => req('/strategies'),
  // The operator's own switch: off, a daily budget, a per-trade cap. Built
  // 2026-09-21 so a bad strategy can be stopped from the app in one click.
  strategyControl: () => req('/strategies/control'),
  // Peak/trough P&L per open position, net of both sides. See research/position_path.py.
  positionsPath: () => req('/positions/path'),
  tradePlans: () => req('/trade-plans'),
  tradePlansAccuracy: () => req('/trade-plans/accuracy'),
  signalRaces: () => req('/signal-races'),
  // The paused coin_stacking behaviour, still measured in the lab.
  coinStacking: () => req('/research/coin-stacking'),
  // Retire to the lab: the rows move to mode='lab', so the book stops counting
  // them and exit_lab keeps reading them. Nothing is deleted.
  retirePreview: (name) => req(`/strategies/${encodeURIComponent(name)}/retire-preview`),
  retireStrategy: (name, reason) =>
    req(`/strategies/${encodeURIComponent(name)}/retire`, { method: 'POST', body: { reason } }),
  restoreStrategy: (name) =>
    req(`/strategies/${encodeURIComponent(name)}/restore`, { method: 'POST', body: {} }),
  retiredStrategies: () => req('/strategies/retired'),
  setStrategyControl: (name, patch) =>
    req(`/strategies/control/${encodeURIComponent(name)}`, { method: 'POST', body: patch }),
  allocations: (mode = 'paper') => req(`/allocations?mode=${mode}`),
  refreshAllocations: (mode = 'paper') => req('/allocations/refresh', { method: 'POST', body: { mode } }),

  signals: (limit = 100, f = {}) => req(`/signals?${qs({ limit, ...f })}`),
  orders: (limit = 100, f = {}) => req(`/orders?${qs({ limit, ...f })}`),
  trades: (mode = 'paper', f = {}) => req(`/trades?${qs({ mode, ...f })}`),
  performance: (mode = 'paper') => req(`/performance?mode=${mode}`),
  equity: (mode = 'paper') => req(`/equity?mode=${mode}`),
  events: (limit = 100) => req(`/events?limit=${limit}`),

  riskStatus: (mode = 'paper') => req(`/risk/status?mode=${mode}`),
  kill: (reason) => req('/risk/kill', { method: 'POST', body: { reason } }),
  release: () => req('/risk/release', { method: 'POST', body: {} }),

  backtest: (body) => req('/research/backtest', { method: 'POST', body }),
  costSensitivity: (body) => req('/research/cost-sensitivity', { method: 'POST', body }),
  walkforward: (body) => req('/research/walkforward', { method: 'POST', body }),
}

export const fmt = {
  usd: (v) => (v == null || Number.isNaN(v) ? '—' : `$${Number(v).toFixed(2)}`),
  pct: (v, d = 2) => (v == null || Number.isNaN(v) ? '—' : `${Number(v).toFixed(d)}%`),
  bps: (v, d = 0) => (v == null || Number.isNaN(v) ? '—' : `${Number(v).toFixed(d)} bps`),
  num: (v, d = 2) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d)),
  // A PRICE keeps every digit the coin is actually quoted in. Six decimals made
  // PEPE read 0.000004 for an entry of 0.00000412 and a stop of 0.00000398 --
  // three different numbers shown as the same one. Decimals follow magnitude
  // so a $0.000004 coin gets eight and BTC gets two, and a per-coin
  // `price_decimals` (from the venue's quote increment, when synced) can only
  // raise that, never lower it.
  px: (v, decimals) => {
    if (v == null || Number.isNaN(v)) return '—'
    const a = Math.abs(Number(v))
    let d = a === 0 ? 2 : a < 0.0001 ? 9 : a < 0.001 ? 8 : a < 0.01 ? 7 : a < 1 ? 6 : a < 100 ? 4 : 2
    if (Number.isFinite(decimals) && decimals > d) d = decimals
    return Number(v).toFixed(d)
  },
  time: (t) => (t ? new Date(t * 1000).toLocaleString() : '—'),
}
