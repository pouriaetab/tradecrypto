import React from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

/* THE OPERATOR'S SWITCH -- first card on the page, because the day you need it
 * you need it immediately. Built 2026-09-21: burst_catch lost $14.39 net across
 * 14 trades in about three hours, and the only way to stop it was to edit a
 * Python list. Off, a daily budget, a per-trade cap -- all live, no restart.
 *
 * Turning a strategy OFF refuses new entries only. Positions it already holds
 * keep managing themselves to their own exits; stranding money in a position
 * with nothing watching it would be worse than the problem this solves. The
 * card says so out loud, because a switch whose scope is unclear gets used
 * wrongly in the moment it matters. */
function ControlPanel() {
  const ctl = useAsync(() => api.strategyControl(), [], 20000)
  const [busy, setBusy] = React.useState(null)
  const [said, setSaid] = React.useState(null)
  const [draft, setDraft] = React.useState({})
  // Retiring is two clicks on purpose. The first one only ASKS the server what
  // would move; nothing changes until the numbers are on the screen and the
  // operator confirms them. "Remove its trades" deserves to be seen before it
  // is done, not described afterwards.
  const [pending, setPending] = React.useState(null)
  const retired = useAsync(() => api.retiredStrategies(), [], 60000)

  const askRetire = async (name) => {
    setBusy(name); setSaid(null)
    try { setPending({ name, p: await api.retirePreview(name) }) }
    catch (e) { setSaid({ name, msg: String(e?.message || e), bad: true }) }
    finally { setBusy(null) }
  }
  const doRetire = async (name, reason) => {
    setBusy(name); setSaid(null)
    try {
      const r = await api.retireStrategy(name, reason)
      setSaid({ name, msg: r?.message || 'retired', bad: false })
      setPending(null); ctl.reload?.(); retired.reload?.()
    } catch (e) {
      setSaid({ name, msg: String(e?.message || e), bad: true })
    } finally { setBusy(null) }
  }
  const doRestore = async (name) => {
    setBusy(name); setSaid(null)
    try {
      const r = await api.restoreStrategy(name)
      setSaid({ name, msg: r?.message || 'restored', bad: false })
      ctl.reload?.(); retired.reload?.()
    } catch (e) { setSaid({ name, msg: String(e?.message || e), bad: true }) }
    finally { setBusy(null) }
  }

  const send = async (name, patch) => {
    setBusy(name); setSaid(null)
    try {
      const r = await api.setStrategyControl(name, patch)
      setSaid({ name, msg: r?.message || 'saved', bad: false })
      ctl.reload?.()
    } catch (e) {
      setSaid({ name, msg: String(e?.message || e), bad: true })
    } finally {
      setBusy(null)
    }
  }

  const money = (v) => (v == null ? '—' : `${v < 0 ? '−' : ''}$${Math.abs(Number(v)).toFixed(2)}`)

  return (
    <Card title="strategy control — switch, budget, per-trade cap">
      <p className="mut" style={{ fontSize: 12, marginTop: 0 }}>
        Yours, live, no restart. Switching a strategy <b>off</b> refuses new entries only — anything it already holds keeps running its own exit, so nothing is ever left unmanaged.
        <Info text="Budget and cap are both in dollars of notional opened; 0 means no limit. The daily budget counts filled opening orders since midnight Austin, not signals. A setting you make by hand outranks anything automatic: the allocator cannot switch back on what you switched off." />
      </p>
      {ctl.err ? <Banner tone="neg">{ctl.err}</Banner>
        : !ctl.data ? <div className="mut">loading…</div>
        : (
          <>
            {said && (
              <Banner tone={said.bad ? 'neg' : 'pos'}>
                {said.bad ? `${said.name}: ${said.msg}` : said.msg}
              </Banner>
            )}
            <Table
              cols={[
                { key: 'strategy', label: 'strategy' },
                { key: 'standing', label: 'standing' },
                { key: 'sw', label: 'switch' },
                { key: 'record', label: 'record so far' },
                // A raw float is not a figure. These render through money()
                // rather than falling through as 18.462663010795094.
                { key: 'net_usd', label: 'net', num: true,
                  render: (r) => <span className={r.net_usd > 0 ? 'pos' : r.net_usd < 0 ? 'neg' : 'mut'}>{money(r.net_usd)}</span> },
                { key: 'toll', label: 'of which toll', num: true,
                  render: (r) => <span className="mut">{money(r.toll)}</span> },
                { key: 'open_positions', label: 'open', num: true },
                { key: 'spend', label: 'opened today' },
                { key: 'budget', label: 'daily budget $' },
                { key: 'cap', label: 'per-trade cap $' },
                { key: 'retire', label: 'lab' },
              ]}
              rows={(ctl.data.strategies || []).map((s) => {
                const d = draft[s.strategy] || {}
                const num = (v) => (v === '' || v == null ? '' : String(v))
                const field = (key, val, ph) => (
                  <input
                    id={`ctl-${key}-${s.strategy}`}
                    className="inp"
                    type="number" min="0" step="1" inputMode="decimal"
                    style={{ width: 92 }}
                    placeholder={ph}
                    value={d[key] !== undefined ? d[key] : num(val || '')}
                    onChange={(e) => setDraft((p) => ({ ...p, [s.strategy]: { ...p[s.strategy], [key]: e.target.value } }))}
                    onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur() }}
                    onBlur={(e) => {
                      const raw = e.target.value.trim()
                      const next = raw === '' ? 0 : Number(raw)
                      if (!Number.isFinite(next) || next < 0) return
                      if (next === Number(val || 0)) return
                      send(s.strategy, { [key === 'budget' ? 'daily_budget_usd' : 'max_position_usd']: next })
                    }}
                  />
                )
                const STANDING = {
                  active: ['', 'on the roster — it can open new positions'],
                  'in the lab': ['mut', 'its trades are out of the book; the lab still reads them'],
                }
                const [stCls, stTitle] = STANDING[s.standing]
                  || ['neg', 'this strategy is no longer on the active roster, but its trades and positions are still in the book — retire it to move them to the lab']
                return {
                  strategy: s.strategy,
                  standing: <span className={stCls} title={stTitle}>{s.standing}</span>,
                  sw: (
                    <button
                      className={s.enabled ? 'btn' : 'btn warn'}
                      disabled={busy === s.strategy}
                      aria-pressed={!s.enabled}
                      title={s.enabled ? 'stop this strategy opening new positions' : 'let this strategy open positions again'}
                      onClick={() => send(s.strategy, { enabled: !s.enabled })}
                    >
                      {busy === s.strategy ? '…' : s.enabled ? 'on' : 'OFF'}
                    </button>
                  ),
                  record: s.closed
                    ? `${s.wins}/${s.closed} profitable`
                    : (s.open_positions ? 'nothing closed yet' : 'no trades'),
                  net_usd: s.net_usd,
                  toll: -Math.abs(s.cost_usd || 0),
                  open_positions: s.open_positions,
                  spend: s.daily_budget_usd > 0
                    ? `${money(s.spent_today_usd)} / ${money(s.daily_budget_usd)}`
                    : money(s.spent_today_usd),
                  budget: field('budget', s.daily_budget_usd),
                  cap: field('cap', s.max_position_usd),
                  retire: (
                    <button className="btn" disabled={busy === s.strategy}
                            title="move this strategy's trades out of the book and into the lab — nothing is deleted"
                            onClick={() => askRetire(s.strategy)}>
                      {busy === s.strategy ? '…' : 'retire →'}
                    </button>
                  ),
                }
              })}
            />
            {pending && (
              <div className="card" style={{ marginTop: 10, padding: 12, border: '1px solid var(--warning)' }}>
                <b>Retire {pending.name} to the lab?</b>
                <ul style={{ fontSize: 13, margin: '8px 0', paddingLeft: 18 }}>
                  <li>{pending.p.closed_trades} closed trade{pending.p.closed_trades === 1 ? '' : 's'} move out of the book
                      ({pending.p.wins} profitable), taking {money(pending.p.net_usd)} with them.</li>
                  <li>The book goes from {money(pending.p.book_net_before_usd)} to <b>{money(pending.p.book_net_after_usd)}</b>.</li>
                  {pending.p.open_positions?.length > 0 && (
                    <li><b>{pending.p.open_positions.length} open position{pending.p.open_positions.length === 1 ? '' : 's'} will be closed first</b> at the current price:
                        {' '}{pending.p.open_positions.map((o) => o.symbol).join(', ')}.</li>
                  )}
                  <li><b>Nothing is deleted.</b> Every trade, order and fill stays in the database in the lab,
                      where the exit lab still reads them — so it can be tuned against its own real fills.</li>
                </ul>
                <div className="row" style={{ gap: 8 }}>
                  <button className="btn warn" disabled={busy === pending.name}
                          onClick={() => doRetire(pending.name, 'retired by the operator from the Strategies tab')}>
                    {busy === pending.name ? 'retiring…' : `yes, retire ${pending.name}`}
                  </button>
                  <button className="btn" onClick={() => setPending(null)}>cancel</button>
                </div>
              </div>
            )}
            {retired.data?.retired?.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div className="mut" style={{ fontSize: 11, marginBottom: 4 }}>
                  in the lab — out of the book, still being studied
                </div>
                <Table
                  cols={[
                    { key: 'strategy', label: 'strategy' },
                    { key: 'closed_trades', label: 'trades kept', num: true },
                    { key: 'net_usd', label: 'net it had cost', num: true },
                    { key: 'back', label: '' },
                  ]}
                  rows={retired.data.retired.map((r) => ({
                    strategy: r.strategy,
                    closed_trades: r.closed_trades,
                    net_usd: r.net_usd,
                    back: (
                      <button className="btn" disabled={busy === r.strategy}
                              title="put this strategy's trades back into the book"
                              onClick={() => doRestore(r.strategy)}>
                        {busy === r.strategy ? '…' : '← restore'}
                      </button>
                    ),
                  }))} />
              </div>
            )}
            <p className="mut" style={{ fontSize: 11, marginBottom: 0 }}>
              Type a number and press Enter or click away to save it. 0 removes the limit.
            </p>
          </>
        )}
    </Card>
  )
}

export default function Strategies({ onModel }) {
  const strats = useAsync(() => api.strategies(), [])
  const alloc = useAsync(() => api.allocations('paper'), [], 20000)
  const roll = useAsync(() => api.rollingEntry(), [], 300000)
  const vers = useAsync(() => api.strategyVersions(), [], 300000)
  const reg = useAsync(() => api.regimes(), [], 300000)
  const late = useAsync(() => api.entryLateness(), [], 600000)
  const eq = useAsync(() => api.entryQuality(), [], 600000)

  const pct = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`)
  const stat = (d) => (!d || !d.n ? '—' : `${d.n} entries · ${pct(d.mean_net_pct)} ± ${Number(d.se_pct).toFixed(2)} · ${Number(d.win_rate).toFixed(0)}% win`)

  return (
    <>
      <h1>Strategies</h1>

      <ControlPanel />

      {/* THE CLOCK TEST. Same trigger, two clocks, same exit, same coins. This
          is what decides whether pump_catch replaces volume_build -- not the
          PENGU anecdote that prompted it. */}
      <Card title="clock test — pump_catch (rolling 60 min) vs volume_build (calendar hour)">
        <p className="mut" style={{ fontSize: 12, marginTop: 0 }}>
          The identical trigger and the identical exit, replayed over the same span of 15-minute bars; the only difference is whether the volume window is a calendar hour read every 10 minutes or a rolling hour read every 5.
          <Info text="Matched = the same coin fired on both clocks within two hours; the lead is how many minutes earlier the rolling clock bought and at what price. Extra = rolling entries the hourly clock never made — the 'false entries' question. Missed = hourly entries the rolling clock did not make. All scored with volume_build's own exit on 15-minute bars, Robinhood's spread both sides. Re-run daily by the rolling_entry job." />
        </p>
        {roll.err ? <div className="neg">{roll.err}</div>
          : !roll.data || !roll.data.available ? <div className="mut">{roll.data?.why || 'waiting for the first daily run'}</div>
          : (() => {
            const d = roll.data
            return (
              <>
                <div className="grid c3" style={{ marginBottom: 8 }}>
                  <Stat small label="rolling led by" value={d.matched?.median_lead_min == null ? '—' : `${Number(d.matched.median_lead_min).toFixed(0)} min`}
                        meta={`${d.matched?.n ?? 0} matched · bought ${pct(d.matched?.median_price_improvement_pct)} lower`} />
                  <Stat small label="on the matched entries" value={`${pct(d.matched?.rolling_net_on_matched?.mean_net_pct)} vs ${pct(d.matched?.hourly_net_on_matched?.mean_net_pct)}`}
                        meta="rolling vs hourly, net per trade" tone={(d.matched?.rolling_net_on_matched?.mean_net_pct ?? 0) >= (d.matched?.hourly_net_on_matched?.mean_net_pct ?? 0) ? 'pos' : 'neg'} />
                  <Stat small label="extra rolling entries" value={pct(d.extra_rolling_entries?.mean_net_pct)}
                        meta={`${d.extra_rolling_entries?.n ?? 0} entries the hour never made`}
                        tone={(d.extra_rolling_entries?.mean_net_pct ?? 0) >= (d.hourly?.mean_net_pct ?? 0) ? 'pos' : 'neg'} />
                </div>
                <Table cols={[{ key: 'k', label: 'entry set' }, { key: 'v', label: 'n · net/trade · win' }]}
                       rows={[
                         { k: 'volume_build (calendar hour)', v: stat(d.hourly) },
                         { k: 'pump_catch (rolling hour)', v: stat(d.rolling) },
                         { k: 'extra — rolling only', v: stat(d.extra_rolling_entries) },
                         { k: 'missed — hourly only', v: stat(d.hourly_entries_rolling_missed) },
                       ]} />
                <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>
                  <b>{d.verdict}</b> · {Number(d.span_days).toFixed(0)} days, {d.coins} coins · {d.rolling_minus_hourly_pct != null && `overall ${pct(d.rolling_minus_hourly_pct)} (${Number(d.sigmas).toFixed(1)}σ)`}
                  {d.ran_at && ` · ran ${fmt.time(d.ran_at)}`}
                </div>
                <div className="mut" style={{ fontSize: 11, marginTop: 4 }}>{d.assumptions}</div>
              </>
            )
          })()}
      </Card>

      <p className="sub">
        Each strategy is a falsifiable hypothesis with a fitted coefficient and a confidence interval.<Info text="When the interval includes zero the coefficient is forced to zero, the expected edge becomes zero, and the engine declines to trade it. No edge claimed means no trades placed." />
      </p>

      <div className="grid c2">
        {(strats.data || []).map((s) => (
          <Card key={s.name} title={s.name}
                right={<button className="why" onClick={() => onModel(s.model_card)}>model card</button>}>
            <div className="mut mono" style={{ fontSize: 12, marginBottom: 8 }}>v{s.version}</div>
            <Table
              cols={[{ key: 'k', label: 'parameter' }, { key: 'v', label: 'value', num: true }]}
              rows={Object.entries(s.params).map(([k, v]) => ({ k, v: Array.isArray(v) ? `[${v.map((x) => fmt.num(x, 2)).join(', ')}]` : String(v) }))}
            />
          </Card>
        ))}
      </div>

      <h2>Entry quality — can anything knowable at the signal tell a winner from a loser?<Info text="For each strategy, every historical entry it would have made (its own exit, Robinhood's spread both sides) with everything knowable at that bar: the strategy's own features, lateness (last-hour move, distance from the 2h high), the coin's 24h and day-so-far moves, breadth, the leader, dispersion, hour, and the day's regime. An L2 logistic regression is fitted on the first three quarters of entries and read ONCE on the last quarter. 'Usable for ranking' needs the held-out AUC interval above 0.5 and the top predicted quintile ahead of the bottom by 2σ in money. It changes no trade; promotion into the ranking is a separate step." /></h2>
      <div className="grid c2">
        {Object.entries(eq.data?.strategies || {}).map(([name, r]) => (
          <Card key={name} title={name}>
            {!r ? <div className="mut">not fitted yet — the entry_quality job runs daily</div>
              : !r.available ? <div className="mut">{r.why}</div>
              : (() => {
                const h = r.holdout
                return (
                  <>
                    <div className="row" style={{ gap: 12, flexWrap: 'wrap', marginBottom: 6 }}>
                      <Stat small label="held-out AUC" value={h.auc.toFixed(3)} meta={`95% ${h.auc_ci95[0]?.toFixed(3)}–${h.auc_ci95[1]?.toFixed(3)} · ${r.n_holdout} entries`}
                            tone={h.auc_ci95[0] > 0.5 ? 'pos' : 'neg'} />
                      <Stat small label="top vs bottom quintile" value={h.top_vs_bottom ? `${h.top_vs_bottom.top_minus_bottom_pct >= 0 ? '+' : ''}${h.top_vs_bottom.top_minus_bottom_pct.toFixed(2)}%` : '—'}
                            meta={h.top_vs_bottom ? `${h.top_vs_bottom.sigmas.toFixed(1)}σ · net per trade` : ''} tone={h.top_vs_bottom?.sigmas >= 2 ? 'pos' : 'neg'} />
                      <Stat small label="trade only the better half" value={h.better_half.mean_net_pct == null ? '—' : `${h.better_half.mean_net_pct >= 0 ? '+' : ''}${h.better_half.mean_net_pct.toFixed(2)}%`}
                            meta={`vs ${h.better_half.all_mean_net_pct >= 0 ? '+' : ''}${h.better_half.all_mean_net_pct.toFixed(2)}% taking all`} />
                    </div>
                    <Table cols={[
                      { key: 'quintile', label: 'predicted quintile', num: true, render: (q) => `Q${q.quintile} (p ${q.p_range[0].toFixed(2)}–${q.p_range[1].toFixed(2)})` },
                      { key: 'n', label: 'n', num: true },
                      { key: 'mean_net_pct', label: 'net / trade', num: true, render: (q) => <span className={q.mean_net_pct >= 0 ? 'pos' : 'neg'}>{q.mean_net_pct >= 0 ? '+' : ''}{q.mean_net_pct.toFixed(2)}% <span className="mut">±{q.se_pct.toFixed(2)}</span></span> },
                      { key: 'win_rate', label: 'win %', num: true, render: (q) => `${q.win_rate.toFixed(0)}%` },
                    ]} rows={h.quintiles} />
                    <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
                      <b className={r.usable_for_ranking ? 'pos' : ''}>{r.verdict}</b> · strongest coefficients: {r.coefficients.slice(0, 5).map((c) => `${c.feature} (z ${c.z >= 0 ? '+' : ''}${c.z.toFixed(1)})`).join(', ')}
                      {r.ran_at && ` · ran ${fmt.time(r.ran_at)}`}
                    </div>
                  </>
                )
              })()}
          </Card>
        ))}
      </div>

      <h2>Lateness — does buying late predict a worse trade?<Info text="Every historical entry per strategy, bucketed by how much of the move happened in the final hour before entry and by how close to the prior two-hour high it bought, with the strategy's own exit and Robinhood's spread both sides. Spearman rank correlation with a permutation p-value, so one fat bucket cannot fake a trend." /></h2>
      <div className="grid c2">
        {Object.entries(late.data?.strategies || {}).map(([name, r]) => (
          <Card key={name} title={name}>
            {!r ? <div className="mut">not measured yet — the entry_lateness job runs daily</div>
              : !r.available ? <div className="mut">{r.why}</div>
              : (
                <>
                  <div className="mut" style={{ fontSize: 12, marginBottom: 6 }}>
                    {r.n_entries.toLocaleString()} entries over {Math.round(r.span_days)} days · all: {r.all.mean_net_pct >= 0 ? '+' : ''}{r.all.mean_net_pct.toFixed(2)}%/trade, {r.all.win_rate.toFixed(0)}% win
                  </div>
                  <Table cols={[
                    { key: 'bucket', label: 'move in the last hour' }, { key: 'n', label: 'n', num: true },
                    { key: 'mean_net_pct', label: 'net / trade', num: true, render: (b) => (b.n ? <span className={b.mean_net_pct >= 0 ? 'pos' : 'neg'}>{b.mean_net_pct >= 0 ? '+' : ''}{b.mean_net_pct.toFixed(2)}% <span className="mut">±{b.se_pct.toFixed(2)}</span></span> : '—') },
                    { key: 'win_rate', label: 'win %', num: true, render: (b) => (b.n ? `${b.win_rate.toFixed(0)}%` : '—') },
                  ]} rows={r.by_last_hour_move} />
                  <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
                    last-hour move: {r.verdicts.last_hour_pct.note} (ρ {r.verdicts.last_hour_pct.rho?.toFixed(3)}, p {r.verdicts.last_hour_pct.p_value?.toFixed(2)}) · distance from the 2h high: {r.verdicts.vs_high_pct.note} (ρ {r.verdicts.vs_high_pct.rho?.toFixed(3)}, p {r.verdicts.vs_high_pct.p_value?.toFixed(2)})
                    {r.ran_at && ` · ran ${fmt.time(r.ran_at)}`}
                  </div>
                </>
              )}
          </Card>
        ))}
      </div>

      <h2>The day's weather — regimes found in the data, and what each strategy earns in them<Info text="Every day since 2022 is read at 09:00 Austin (breadth, the median coin's 24h return, BTC's 24h return, dispersion, BTC volatility, the move since midnight) and clustered by a Gaussian mixture; the number of day types is chosen by BIC and each type is named from its own centroid. Every strategy is then replayed with its own exit and every entry is filed under the weather of its day. The router turns a cell that is ≥2σ from the strategy's overall into a size multiplier between 0.5 and 1.5, shrunk by n/(n+40); an indistinguishable cell leaves size alone." /></h2>
      <Card>
        {reg.err ? <div className="neg">{reg.err}</div>
          : !reg.data?.available ? <div className="mut">{reg.data?.why || 'waiting for the first daily run'}</div>
          : (() => {
            const d = reg.data
            const regimes = Object.keys(d.counts || {}).sort()
            /* `n` being present does not mean every figure is. The OVERALL cell
             * legitimately carries no `sigmas` -- it is the baseline the regime
             * cells are measured against, so there is nothing for it to be two
             * sigma away from. Guarding only on `n` crashed the whole tab with
             * "Cannot read properties of undefined (reading 'toFixed')" the day
             * the scorecard first had data in it. Guard each figure, and let a
             * missing one degrade to a dash instead of taking the page down. */
            const num = (x, dp, sign) => (typeof x !== 'number' || !Number.isFinite(x) ? null
              : `${sign && x >= 0 ? '+' : ''}${x.toFixed(dp)}`)
            const cell = (v) => {
              if (!v || !v.n) return <span className="mut">—</span>
              const net = num(v.mean_net_pct, 2, true)
              const sig = num(v.sigmas, 1, true)
              const win = num(v.win_rate, 0, false)
              const tone = typeof v.sigmas === 'number' ? (v.sigmas >= 2 ? 'pos' : v.sigmas <= -2 ? 'neg' : '') : ''
              const title = [`${v.n} entries`, win && `${win}% win`,
                             sig ? `${sig}σ vs overall` : 'this is the baseline']
                .filter(Boolean).join(' · ')
              return (
                <span className={tone} title={title}>
                  {net == null ? <span className="mut">—</span> : `${net}%`}
                  <span className="mut" style={{ fontSize: 11 }}> n{v.n}</span>
                </span>
              )
            }
            return (
              <>
                <div className="row" style={{ gap: 14, flexWrap: 'wrap', marginBottom: 8 }}>
                  <Stat small label="today" value={d.today?.regime || 'not read yet'}
                        meta={d.today ? `p ${Number(d.today.p).toFixed(2)} · breadth ${Number(d.today.breadth).toFixed(2)} · median 24h ${Number(d.today.median_24h) >= 0 ? '+' : ''}${Number(d.today.median_24h).toFixed(1)}%` : `read at ${String(d.read_hour).padStart(2, '0')}:00 Austin`} />
                  {regimes.map((r) => (
                    <Stat key={r} small label={r} value={`${d.counts[r]} days`}
                          meta={d.rest_of_day_by_regime?.[r]?.n ? `median coin rest-of-day ${d.rest_of_day_by_regime[r].mean_net_pct >= 0 ? '+' : ''}${d.rest_of_day_by_regime[r].mean_net_pct.toFixed(2)}%` : ''} />
                  ))}
                </div>
                <Table
                  cols={[
                    { key: 'strategy', label: 'strategy' },
                    { key: 'overall', label: 'overall', num: true, render: (r) => cell(r.overall) },
                    ...regimes.map((rg) => ({ key: rg, label: rg, num: true, render: (r) => cell(r[rg]) })),
                    { key: 'router', label: 'router today', num: true,
                      render: (r) => (r.router ? <span title={r.router.why} className={r.router.multiplier > 1.001 ? 'pos' : r.router.multiplier < 0.999 ? 'neg' : 'mut'}>×{Number(r.router.multiplier).toFixed(2)}</span> : '—') },
                  ]}
                  rows={Object.entries(d.scorecard || {}).map(([name, card]) => ({
                    strategy: name, overall: card.overall,
                    ...Object.fromEntries(regimes.map((rg) => [rg, card.by_regime?.[rg]])),
                    router: d.router?.[name],
                  }))} />
                <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
                  {d.definition} · k={d.k} by BIC · {d.days} days · green/red = ≥2σ better/worse than the strategy's own overall
                  {d.ran_at && ` · ran ${fmt.time(d.ran_at)}`}
                </div>
              </>
            )
          })()}
      </Card>

      <h2>Versions — which rule made which trade, and how old rules are weighted<Info text="A version is a distinct (class version, hash of the parameters in force). Every closed trade is stamped with it. The learning loop weights a trade from an earlier version by λ^(versions since), where λ is the share of parameters that version shares with the rule running now — a one-parameter tuning keeps ~90% of its weight, a rewrite keeps little, a removed rule counts zero. The three stability readings decide when to stop discounting altogether." /></h2>
      {(vers.data?.strategies || []).map((L) => (
        <Card key={L.strategy} title={`${L.strategy} — ${L.versions.length} version${L.versions.length === 1 ? '' : 's'} traded · now ${L.current.version} / ${L.current.params_hash}`}>
          <Table
            cols={[
              { key: 'version', label: 'version', render: (v) => <span>{v.version} <span className="mut mono" style={{ fontSize: 11 }}>{v.params_hash}</span>{v.is_current && <span className="pill ok" style={{ marginLeft: 6 }}>current</span>}{v.backfilled && <span className="mut" title="tagged after the fact with today's rule hash"> *</span>}</span> },
              { key: 'n', label: 'trades', num: true },
              { key: 'mean_net_pct', label: 'net / trade', num: true, render: (v) => (v.mean_net_pct == null ? '—' : <span className={v.mean_net_pct >= 0 ? 'pos' : 'neg'}>{v.mean_net_pct >= 0 ? '+' : ''}{v.mean_net_pct.toFixed(2)}%{v.se_pct != null ? <span className="mut"> ±{v.se_pct.toFixed(2)}</span> : null}</span>) },
              { key: 'win_rate', label: 'win %', num: true, render: (v) => (v.win_rate == null ? '—' : `${v.win_rate.toFixed(0)}%`) },
              { key: 'total_usd', label: 'total', num: true, render: (v) => fmt.usd(v.total_usd) },
              { key: 'similarity_to_current', label: 'λ vs current', num: true, info: 'Share of parameters this version shares with the rule running now — the weight one version-step of its trades keeps.', render: (v) => (v.similarity_to_current == null ? '—' : v.similarity_to_current.toFixed(2)) },
              { key: 'vs_previous', label: 'vs previous', render: (v) => (!v.vs_previous ? <span className="mut">first</span> : v.vs_previous.t == null ? <span className="mut">{v.vs_previous.verdict}</span> : <span className={v.vs_previous.verdict === 'better' ? 'pos' : v.vs_previous.verdict === 'worse' ? 'neg' : 'mut'}>{v.vs_previous.diff_pct >= 0 ? '+' : ''}{v.vs_previous.diff_pct.toFixed(2)}% ({v.vs_previous.t.toFixed(1)}σ) {v.vs_previous.verdict}</span>) },
              { key: 'first_ts', label: 'first', render: (v) => fmt.time(v.first_ts) },
              { key: 'last_ts', label: 'last', render: (v) => fmt.time(v.last_ts) },
            ]}
            rows={L.versions} empty="no closed trades yet" />
          {L.stability && (() => {
            const st = L.stability
            const tick = (ok) => <span className={ok ? 'pos' : 'mut'}>{ok ? '✓' : '—'}</span>
            return (
              <div className="mut" style={{ fontSize: 12, marginTop: 8 }}>
                <b className={st.stable ? 'pos' : ''}>{st.stable ? 'stable — time decay only' : 'not yet stable — version discounting in force'}</b>
                {' · '}{tick(st.a_promotion_rate_per_100.ok)} (a) promotions per 100 trades: {st.a_promotion_rate_per_100.value == null ? '—' : st.a_promotion_rate_per_100.value.toFixed(1)} over the last {st.a_promotion_rate_per_100.window_trades}
                {' · '}{tick(st.b_param_drift.ok)} (b) parameter drift: {st.b_param_drift.median_grid_steps == null ? 'no refits' : `${st.b_param_drift.median_grid_steps.toFixed(2)} grid steps (median, last ${st.b_param_drift.refits_considered})`}
                {' · '}{tick(st.c_holdout_consistency.ok)} (c) held-out net inside the previous CI: {st.c_holdout_consistency.last.length ? st.c_holdout_consistency.last.map((x) => (x ? 'in' : 'out')).join(', ') : 'fewer than 2 refits'}
                {st.time_decay_half_life_months != null && <> · half-life {st.time_decay_half_life_months.toFixed(1)} months (from monthly autocorrelation)</>}
                <div style={{ marginTop: 3 }}>{st.why}</div>
              </div>
            )
          })()}
        </Card>
      ))}

      <h2>Capital allocation (the feedback loop)</h2>
      <Banner kind="info" title="How capital is earned, not assigned">
        After every closed trade the system updates a posterior over that strategy's true net edge.
        Capital only flows to a strategy once the probability that its edge exceeds the measured cost
        hurdle passes 0.75 — and the size is computed from the lower bound of the edge interval, at
        quarter-Kelly. A strategy that stops working is defunded automatically.
      </Banner>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={() => api.refreshAllocations('paper').then(alloc.reload)}>recompute posteriors</button>
      </div>

      {(alloc.data || []).length === 0 ? (
        <Card><div className="mut">No strategy has closed a trade yet, so every posterior is still the prior
          and every allocation is zero. That is the correct state on day one.</div></Card>
      ) : (
        (alloc.data || []).map((a) => (
          <Card key={a.id} title={a.strategy}
                right={<span className={`pill ${a.status === 'live' ? 'ok' : a.status === 'halted' ? 'bad' : 'warn'}`}>{a.status}</span>}>
            <div className="grid c4">
              <Stat label="Allocation" value={fmt.pct(a.allocation_frac * 100)} small
                    model="position_sizing" onModel={onModel} />
              <Stat label="P(edge > cost hurdle)" value={fmt.num(a.posterior?.prob_edge_above_hurdle, 2)} small
                    meta="needs ≥ 0.75" model="edge_posterior" onModel={onModel} />
              <Stat label="Posterior mean edge" n={a.posterior?.n_trades_considered}
                    value={fmt.bps(a.posterior?.return_posterior?.mean_bps, 1)}
                    meta={`CI ${(a.posterior?.return_posterior?.mean_ci90_bps || []).map((x) => fmt.num(x, 0)).join(' to ')}`} />
              <Stat label="Hit rate posterior" n={a.posterior?.n_trades_considered}
                    value={fmt.pct((a.posterior?.hit_rate_posterior?.mean ?? 0) * 100, 1)} />
            </div>
            <div className="mut" style={{ marginTop: 8 }}>{a.reason}</div>
          </Card>
        ))
      )}
    </>
  )
}
