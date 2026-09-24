import React, { useEffect, useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync, clearAsyncCache } from '../components/ui.jsx'
import CoinChart from '../components/CoinChart.jsx'
import LiveStatus from '../components/LiveStatus.jsx'

const fmtPx = (v) => (v == null ? '—' : `$${v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(4) : v.toFixed(2)}`)

function Step({ s }) {
  return (
    <div className="step">
      <div className={`step-dot ${s.done ? 'done' : 'todo'}`}>{s.done ? '✓' : ''}</div>
      <div className="step-body">
        <div className="step-label">
          {s.label}
          {s.why && <Info text={s.why} />}
        </div>
        <div className="step-detail">{s.detail}</div>
      </div>
      {!s.done && s.action && <div className="step-action">{s.action}</div>}
    </div>
  )
}

/* The Setup checklist is the tallest block on this page and the least useful
   once it is done, so it collapses to a single line and remembers the choice.
   localStorage throws in private mode and does not exist in a server render,
   hence the try/catch on every touch. Default is CLOSED. */
const SETUP_KEY = 'tc.overview.setupOpen.v1'
function loadSetupOpen() {
  try { return localStorage.getItem(SETUP_KEY) === '1' } catch { return false }
}
function saveSetupOpen(v) {
  try { localStorage.setItem(SETUP_KEY, v ? '1' : '0') } catch { /* private mode */ }
}

/* Two columns above 1000px, one below. The breakpoint lives here rather than in
   styles.css because the stylesheet is shared and this is the only page that
   wants a side column. matchMedia is absent in a server render -> single column. */
const WIDE = '(min-width: 1000px)'
function useWide() {
  const [wide, setWide] = useState(() => {
    try { return window.matchMedia(WIDE).matches } catch { return false }
  })
  useEffect(() => {
    let mq
    try { mq = window.matchMedia(WIDE) } catch { return undefined }
    const on = () => setWide(mq.matches)
    on()
    if (mq.addEventListener) mq.addEventListener('change', on)
    else if (mq.addListener) mq.addListener(on)
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', on)
      else if (mq.removeListener) mq.removeListener(on)
    }
  }, [])
  return wide
}

export default function Overview({ onModel }) {
  const status = useAsync(() => api.status(), [], 10000)
  const setup = useAsync(() => api.setup(), [], 10000)
  const cov = useAsync(() => api.coverageToday(), [], 120000)
  const perf = useAsync(() => api.performance('paper'), [], 20000)
  const cost = useAsync(() => api.cost(), [], 30000)
  const risk = useAsync(() => api.riskStatus('paper'), [], 10000)
  const bal = useAsync(() => api.accountBalance(), [], 15000)
  const [openDetail, setOpenDetail] = useState(null)
  const [pick, setPick] = useState(null)
  const ks = useAsync(() => api.killSwitch(), [], 15000)
  const acct = useAsync(() => api.account(), [])
  const hp = useAsync(() => api.health(), [], 30000)
  // Two minutes: the vault is checked on a ten-minute job, so polling it faster
  // than that would only ask the same question more often.
  const vault = useAsync(() => api.vault(), [], 120000)
  const live = useAsync(() => api.liveness(), [], 120000)
  const [equity, setEquity] = useState('')
  const [setupOpen, setSetupOpen] = useState(loadSetupOpen)
  const wide = useWide()

  const eng = status.data?.engine
  const c = cost.data
  const next = setup.data?.next
  const steps = setup.data?.steps || []
  const nDone = steps.filter((s) => s.done).length
  const nLeft = steps.length - nDone
  const allDone = steps.length > 0 && nLeft === 0

  const toggleSetup = () => {
    const v = !setupOpen
    setSetupOpen(v)
    saveSetupOpen(v)
  }

  return (
    <>
      <div className="row" style={{ marginBottom: 6 }}>
        <h1 style={{ margin: 0 }}>Overview</h1>
        <span className="mut" style={{ fontSize: 12 }}>
          read straight from the trade database
          <Info text="Read straight from the trade database. A missing number means the evidence for it does not exist yet." />
        </span>
      </div>

      <LiveStatus />

      {next && (
        <Banner kind="warn" title={`Next: ${next.label} — ${next.action}`}>{next.why}</Banner>
      )}

      {/* A stale process is invisible otherwise. This app ran a 2026-09-12 build
          for four days while three sessions of changes sat on disk, and every
          screen looked normal the whole time. */}
      {status.data?.code?.code_changed_since_boot && (
        <Banner kind="warn" title="Restart needed — the code on disk is newer than what is running">
          {status.data.code.message}
        </Banner>
      )}

      {hp.data?.disk?.low_space && (
        <Banner kind="bad" title={`Low disk — ${hp.data.disk.free_gb.toFixed(1)} GB free`}>
          {hp.data.disk.warning}
        </Banner>
      )}

      {ks.data?.engaged && (
        <Banner kind="warn" title="Kill switch is engaged — no orders can be placed">
          <div className="row">
            <span>
              {ks.data.reason}
              {ks.data.tripped_ts && <> · tripped {fmt.time(ks.data.tripped_ts)}</>}
            </span>
            <button onClick={() => api.release().then(() => { clearAsyncCache(); ks.reload(); status.reload() })}>
              release it
            </button>
            <span className="mut" style={{ fontSize: 11.5 }}>
              It is a DAILY loss cap. It clears itself when the day rolls over; releasing it
              sooner waives the cap for the rest of today, so it will not re-arm for this reason
              until midnight. Drawdown and per-order checks still apply.
            </span>
          </div>
        </Banner>
      )}

      {/* Engine stays first and full width: it was deliberately moved to the top
          in an earlier session. Funding sits directly under it. */}
      <Card>
        {/* ONE LINE. Everything that was stacked underneath — the paused banner,
            the 24/7 state, the new-code notice — is folded into this row as a
            short item with the full explanation on hover. A status strip that
            grows a paragraph every time something needs saying stops being a
            status strip. */}
        <div className="row" style={{ flexWrap: 'wrap', rowGap: 4 }}>
          <span className="label mut">engine</span>
          {/* the word is in the LIVE panel above; this row is the detail */}
          <span className="mut mono" style={{ fontSize: 11 }}>{eng?.mode}</span>
          <span className="mut" style={{ fontSize: 11 }}>{eng?.ticks ?? 0} ticks</span>
          <span className="mut mono" style={{ fontSize: 11 }}
                title="What the RUNNING backend is using. If this does not match what you expect, it is running older code.">
            {(eng?.strategies || []).join(' + ') || 'no strategies'}
          </span>
          <span className="mut" style={{ fontSize: 11 }}>{fmt.time(eng?.last_tick)}</span>

          {/* Does the desk survive on its own? One word, colour-coded. */}
          {status.data?.uptime && (
            <span style={{ fontSize: 11, color: status.data.uptime.stale_path ? 'var(--error)'
                           : status.data.uptime.paused ? 'var(--error)'
                           : status.data.uptime.autostart_installed ? 'var(--success)' : 'var(--warning)' }}
                  title={status.data.uptime.summary}>
              {status.data.uptime.stale_path ? '24/7 broken'
                : status.data.uptime.paused ? 'held down'
                : status.data.uptime.autostart_installed ? '24/7' : 'not 24/7'}
            </span>
          )}

          {/* The write-once copy, in the row where the other "is this desk
              actually safe" words live. Green is the only reassuring answer:
              it means every order and trade is also sitting outside this
              project, outside the database, in a file nothing here reads. */}
          {vault.data && (
            <span style={{ fontSize: 11,
                           color: !vault.data.exists ? 'var(--error)'
                                  : vault.data.inside_project ? 'var(--error)'
                                  : 'var(--success)' }}
                  title={!vault.data.exists
                    ? 'The vault folder does not exist yet. It is created on the next order, trade or reconcile pass.'
                    : `${vault.data.records} record(s) across ${vault.data.days} day(s) in ${vault.data.dir} — `
                      + 'append-only, hash-chained, outside this project and never read by the app. '
                      + 'Check it yourself any time: python3 scripts/vault_verify.py'}>
              {!vault.data.exists ? 'vault missing'
                : vault.data.inside_project ? 'vault NOT independent'
                : `vault ${vault.data.records}`}
            </span>
          )}

          {/* Has each safety mechanism actually RUN? One word here, the detail
              in the card below. Red means something exists, is claimed, and has
              never once executed -- which is how a trailing stop was reported as
              working for weeks while its code block was never entered. */}
          {live.data && (
            <span style={{ fontSize: 11,
                           color: (live.data.never_fired || []).length ? 'var(--error)'
                                  : (live.data.known_not_live || []).length ? 'var(--warning)'
                                  : 'var(--success)' }}
                  title={live.data.headline}>
              {(live.data.never_fired || []).length
                ? `${live.data.never_fired.length} never ran`
                : (live.data.known_not_live || []).length
                  ? `${live.data.known_not_live.length} not live`
                  : 'all running'}
            </span>
          )}

          {/* New code applies itself; say so rather than asking for a restart. */}
          {status.data?.auto_apply?.stale && (
            <span style={{ fontSize: 11, color: 'var(--accent)' }}
                  title={status.data.auto_apply.why}>
              {status.data.auto_apply.enabled ? 'new code — applying itself' : 'new code — restart needed'}
            </span>
          )}

          <div className="spacer" />
          <button style={{ fontSize: 11 }}
                  title="Run one pass of the loop now. The engine does this every 15 seconds on its own — this is for watching it happen."
                  onClick={() => api.engineTick().then(() => { status.reload(); setup.reload() })}>one tick</button>
          {/* Start/stop lives in the LIVE panel at the top of this page now. Two
          identical buttons a few inches apart is not redundancy, it is a
          question about which one is the real one. */}
        </div>
        {eng?.last_error && <div className="err" style={{ marginTop: 6, fontSize: 11 }}>{eng.last_error}</div>}
      </Card>

      {/* Coverage. A strategy that cannot see a shape produces silence, not an
          error, so the silence is put on screen. On 2026-09-13 ten tradable coins
          moved enough to cover their own spread and this desk fired zero signals;
          nothing in the app said so. `uncovered` is the specification for the
          next strategy. */}
      {cov.data && cov.data.coins > 0 && (
        <Card>
          <div className="row">
            <span className="label mut">today's coverage</span>
            <span className="pill ok">{cov.data.worth_taking} worth taking</span>
            <span className={`pill ${cov.data.uncovered_count ? 'bad' : 'ok'}`}>
              {cov.data.uncovered_count} seen by nothing
            </span>
            {cov.data.missed_count > 0 && (
              <span className="pill warn">{cov.data.missed_count} seen, not taken</span>
            )}
            <span className="pill">{cov.data.traded_count} traded</span>
            <Info text="A coin counts as 'worth taking' when its best move from today's open was bigger than its own round-trip spread — the move paid for itself. 'Seen by nothing' means no active strategy produced a single signal for it: not a risk block, not a full book, simply invisible. That list is what a new strategy should be built to catch." />
          </div>
          {cov.data.uncovered_count > 0 && (
            <div className="step-detail" style={{ marginTop: 6 }}>
              invisible to every strategy today:{' '}
              {cov.data.uncovered.map((c) => (
                <span key={c.symbol} className="mono" style={{ marginRight: 10 }}>
                  {c.symbol} <b className="pos">+{c.best_from_open_pct.toFixed(1)}%</b>
                  <span className="mut"> (cost {c.round_trip_pct.toFixed(2)}%)</span>
                </span>
              ))}
            </div>
          )}
        </Card>
      )}

      <div className="grid" style={{
        gridTemplateColumns: wide ? 'minmax(0, 1.3fr) minmax(0, 1fr)' : 'minmax(0, 1fr)',
        gap: 12, alignItems: 'start',
      }}>
        {/* ── left: the money ── */}
        <div>
          <Card>
            <div className="row">
              <span className="label mut">funding</span>
              <span className="mono" style={{ fontSize: 17 }}>{fmt.usd(acct.data?.equity_usd)}</span>
              <span className="mut" style={{ fontSize: 11 }}>
                sizing basis{acct.data?.source ? ` · ${acct.data.source}` : ''}
                <Info text="This app cannot read your Robinhood balance. This number is what position sizing and the daily loss cap are computed from, so it should match what you actually funded." />
              </span>
              <div className="spacer" />
              <input value={equity} onChange={(e) => setEquity(e.target.value)}
                     placeholder="what you funded" style={{ width: 104, fontSize: 11 }} inputMode="decimal" />
              <button className="primary" style={{ fontSize: 11 }} disabled={!equity}
                      onClick={() => api.setAccount(Number(equity)).then(() => { setEquity(''); acct.reload(); risk.reload() })}>
                set
              </button>
            </div>
          </Card>

          <Card title="Book">
            {bal.data ? (
              <>
                <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))' }}>
                  <Stat small label="equity now" value={`$${bal.data.equity.toFixed(2)}`}
                        meta={`stake $${bal.data.declared_stake.toFixed(0)}`
                              + (bal.data.stake_set_ts ? ` · set ${fmt.time(bal.data.stake_set_ts)}` : '')} />
                  <Stat small label="cash free" value={`$${bal.data.cash.toFixed(2)}`}
                        meta="what a new position can use" />
                  <div onClick={() => setOpenDetail(openDetail === 'pos' ? null : 'pos')}
                       style={{ cursor: 'pointer' }}>
                    <Stat small label="in positions" value={`$${bal.data.deployed.toFixed(2)}`}
                          meta={`${bal.data.open_positions} open`} />
                  </div>
                  <div onClick={() => setOpenDetail(openDetail === 'pnl' ? null : 'pnl')}
                       style={{ cursor: 'pointer' }}>
                    <Stat small label="profit / loss"
                          value={`$${(bal.data.realised_pnl + bal.data.unrealised_pnl).toFixed(2)}`}
                          meta={`$${bal.data.realised_pnl.toFixed(2)} banked, $${bal.data.unrealised_pnl.toFixed(2)} on paper`}
                          tone={(bal.data.realised_pnl + bal.data.unrealised_pnl) >= 0 ? 'ok' : 'bad'} />
                  </div>
                </div>

                {openDetail === 'pos' && (
                  <div style={{ marginTop: 10 }}>
                    <Table
                      cols={[
                        { key: 'symbol', label: 'coin',
                          render: (r) => (
                            <span className="link"
                                  style={{ cursor: 'pointer', textDecoration: 'underline dotted' }}
                                  onClick={() => setPick({
                                    symbol: r.symbol,
                                    day: new Date().toLocaleDateString('en-CA', { timeZone: 'America/Chicago' }),
                                    entry: r.entry_px ?? null,
                                    exit: null,
                                  })}>
                              {r.symbol}
                            </span>) },
                        { key: 'strategy', label: 'strategy' },
                        { key: 'opened_ts', label: 'got in',
                          render: (r) => (r.opened_ts ? fmt.time(r.opened_ts) : '—') },
                        { key: 'hours_held', label: 'held', num: true,
                          render: (r) => (r.hours_held != null ? `${r.hours_held.toFixed(1)}h` : '—') },
                        { key: 'exit_rule', label: 'how it exits',
                          info: 'Some strategies aim at a price; pump_ride does not — it holds until the price falls a set distance from its own peak, so the exit moves up as the coin does.' },
                        { key: 'last_px', label: 'price now', num: true,
                          render: (r) => (
                            <span title={r.price_source ? `from the ${r.price_source}` : ''}>
                              {fmtPx(r.last_px)}
                              {r.price_stale && r.price_age_s != null && (
                                <span className="mut"> {Math.round(r.price_age_s / 60)}m old</span>
                              )}
                            </span>),
                          info: 'The market price, straight from the feed. This is the number to compare against Robinhood — the columns beside it are money, after costs.' },
                        { key: 'cost_usd', label: 'put in', num: true,
                          render: (r) => fmt.usd(r.cost_usd) },
                        { key: 'value_usd', label: 'get back', num: true,
                          render: (r) => fmt.usd(r.value_usd),
                          info: 'What a sale right now would put back in the account, after the spread on the way out.' },
                        { key: 'pnl_usd', label: 'profit / loss', num: true,
                          render: (r) => (
                            <span className={(r.pnl_usd ?? 0) >= 0 ? 'pos' : 'neg'}>
                              {fmt.usd(r.pnl_usd)}
                              {r.pnl_pct != null ? ` (${r.pnl_pct >= 0 ? '+' : ''}${r.pnl_pct.toFixed(2)}%)` : ''}
                            </span>),
                          info: 'Net of both sides. This is the number that gets booked when the position closes.' },
                      ]}
                      rows={bal.data.positions || []}
                      empty="nothing open"
                    />
                  </div>
                )}

                {openDetail === 'pnl' && bal.data.pnl_explained && (
                  <div className="mut" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.7 }}>
                    <div>
                      <b>banked {fmt.usd(bal.data.pnl_explained.realised_usd)}</b>
                      {' — '}{bal.data.pnl_explained.realised_means}
                    </div>
                    <div>
                      <b>on paper {fmt.usd(bal.data.pnl_explained.unrealised_usd)}</b>
                      {' — '}{bal.data.pnl_explained.unrealised_means}
                    </div>
                    <div>
                      {fmt.usd(bal.data.declared_stake)} stake
                      {' '}{bal.data.pnl_explained.realised_usd >= 0 ? '+' : '−'} {fmt.usd(Math.abs(bal.data.pnl_explained.realised_usd))}
                      {' '}{bal.data.pnl_explained.unrealised_usd >= 0 ? '+' : '−'} {fmt.usd(Math.abs(bal.data.pnl_explained.unrealised_usd))}
                      {' = '}{fmt.usd(bal.data.equity)} equity
                    </div>
                  </div>
                )}

                <div className="row" style={{ marginTop: 8 }}>
                  <span className="mut" style={{ fontSize: 11.5 }}>
                    {bal.data.open_positions} open · ${bal.data.deployed.toFixed(0)} committed ·
                    budget ${bal.data.declared_stake.toFixed(0)} chunk
                    <Info label="budget" text="Raising it means more room for a second position when something real shows up, not more trades. Nothing forces the money out — an unspent chunk is a good outcome. Change it in Account settings and it is stamped on the equity-now tile so you can see what it was." />
                    <Info label="sizing" text="Position size is decided per trade, not configured: free cash × how far the signal is past its own entry bar. A marginal signal takes about half and leaves room; a loud one takes nearly all of it and there is no room for a second until it closes. Conviction is the signal's score divided by its own entry threshold — a coin at 5x normal volume when the bar is 2x scores 2.5. That maps onto a share of free cash: 1x gives about 45%, 2x about 70%, 3x or more about 95%. Nothing sets a number of trades per day either; positions this size commit the money, so nothing new opens until something closes. Cash is the limiter, not a counter." />
                  </span>
                  <div className="spacer" />
                  <button disabled={!bal.data.open_positions} onClick={() => {
                    if (!window.confirm(`Close all ${bal.data.open_positions} open position(s) now, at the current price?`)) return
                    api.closeAll('manual').then(() => { clearAsyncCache(); bal.reload(); status.reload() })
                  }}>close all positions</button>
                  <button className="danger" onClick={() => {
                    if (!window.confirm(
                      'Clear the paper book?\n\nDeletes trades, orders, positions, signals, the equity '
                      + 'curve, the posteriors built from them, and releases the kill switch.\n\n'
                      + 'KEEPS price history, the universe, measured spreads and model cards.')) return
                    api.paperReset().then(() => { clearAsyncCache(); bal.reload(); ks.reload(); status.reload() })
                  }}>clear paper book</button>
                </div>
              </>
            ) : <span className="mut">loading…</span>}
          </Card>

          <div className="label mut" style={{ margin: '10px 0 5px' }}>numbers</div>
          <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(138px, 1fr))' }}>
            <Stat small label="Round trip cost" value={c ? fmt.pct(c.round_trip_bps / 100) : '—'}
                  meta={c?.source} model="execution_cost" onModel={onModel} />
            <Stat small label="Edge needed / trade" value={c ? fmt.pct(c.hurdle_bps / 100) : '—'}
                  meta="cost × margin" />
            <Stat small label="Closed trades" value={perf.data?.n ?? 0} />
            <Stat small label="Mean net / trade"
                  value={perf.data?.mean_return_bps != null ? fmt.bps(perf.data.mean_return_bps, 1) : '—'}
                  n={perf.data?.n} minN={30} model="edge_posterior" onModel={onModel}
                  tone={perf.data?.mean_return_bps > 0 ? 'pos' : 'neg'} />
            <Stat label="P&L today" value={fmt.usd(risk.data?.realised_pnl_today_usd)} small
                  tone={(risk.data?.realised_pnl_today_usd ?? 0) >= 0 ? 'pos' : 'neg'} />
            <Stat label="Loss headroom" value={fmt.usd(risk.data?.daily_loss_headroom_usd)} small
                  model="risk_guards" onModel={onModel} />
            <Stat label="Orders today"
                  value={`${risk.data?.orders_today ?? 0}/${risk.data?.max_trades_per_day ?? '—'}`} small />
            <Stat label="Kill switch" small value={risk.data?.kill_switch_engaged ? 'ENGAGED' : 'clear'}
                  tone={risk.data?.kill_switch_engaged ? 'neg' : 'pos'} />
          </div>
        </div>

        {/* ── right: is it healthy, is it set up, what is stored ── */}
        <div>
          {/* Collapsed by default: one line that still says done / how many left. */}
          <Card>
            <div className="row">
              <span className="label mut">setup</span>
              <span className={`pill ${allDone ? 'ok' : 'warn'}`}>
                {steps.length === 0 ? '…' : allDone ? 'complete' : `${nLeft} left`}
              </span>
              <span className="mut" style={{ fontSize: 12 }}>
                {steps.length === 0 ? 'loading…' : `${nDone} of ${steps.length} steps done`}
              </span>
              <div className="spacer" />
              <button className="linkish" onClick={toggleSetup}
                      title="The full checklist, step by step. This page remembers whether you left it open.">
                {setupOpen ? 'hide' : 'show'}
              </button>
            </div>
            {setupOpen && (
              <div style={{ marginTop: 4 }}>
                {steps.map((s) => <Step key={s.key} s={s} />)}
              </div>
            )}
          </Card>

          <Card title="Health"
                right={<button className="why" onClick={() => api.housekeepingRun().then(hp.reload)}>run housekeeping</button>}>
            <div className="row" style={{ gap: 16, flexWrap: 'wrap' }}>
              <div>
                <div className="label mut">memory
                  <Info text="The overnight crash was almost certainly memory. The process now watches its own RSS and exits cleanly at the ceiling so the supervisor restarts it — a clean exit is restartable, an OS kill is not. A growth rate that does not flatten means something is still accumulating." />
                </div>
                <div className="mono" style={{ fontSize: 16 }}>
                  <span className={(hp.data?.memory?.pct_of_ceiling ?? 0) > 80 ? 'neg' : 'pos'}>
                    {fmt.num(hp.data?.memory?.rss_mb, 0)} MB
                  </span>
                  <span className="mut"> / {fmt.num(hp.data?.memory?.ceiling_mb, 0)}</span>
                </div>
                <div className="step-detail">
                  peak {fmt.num(hp.data?.memory?.peak_mb, 0)} MB
                  {hp.data?.memory?.growth_mb_per_hour != null &&
                    ` · ${fmt.num(hp.data.memory.growth_mb_per_hour, 1)} MB/hr`}
                </div>
              </div>
              <div>
                <div className="label mut">uptime</div>
                <div className="mono" style={{ fontSize: 16 }}>
                  {fmt.num(hp.data?.memory?.uptime_hours, 1)} h
                </div>
              </div>
              <div>
                <div className="label mut">project on disk
                  <Info text="Hourly and daily bars are kept forever — that is the history everything is validated on. 1-minute bars are pruned after 45 days and quotes after 14; they grow fastest and matter least." />
                </div>
                <div className="mono" style={{ fontSize: 16 }}>{fmt.num(hp.data?.disk?.project_mb, 0)} MB</div>
                <div className="step-detail">db {fmt.num(hp.data?.disk?.database_mb, 0)} + wal {fmt.num(hp.data?.disk?.wal_mb, 0)}</div>
              </div>
              <div>
                <div className="label mut">free on this Mac</div>
                <div className="mono" style={{ fontSize: 16 }}>
                  <span className={hp.data?.disk?.low_space ? 'neg' : 'pos'}>
                    {fmt.num(hp.data?.disk?.free_gb, 1)} GB
                  </span>
                </div>
                <div className="step-detail">{fmt.num(hp.data?.disk?.pct_used, 0)}% used</div>
              </div>
            </div>
          </Card>

          {/* Where a tick's time goes -- measured on this desk, not argued. */}
          {status.data?.engine?.tick_profile?.n > 0 && (() => {
            const tp = status.data.engine.tick_profile
            const ph = Object.entries(tp.phases || {}).sort((a, b) => b[1].share_pct - a[1].share_pct)
            return (
              <Card title="Where a tick's time goes">
                <div className="mut" style={{ fontSize: 12, marginBottom: 6 }}>
                  {tp.window}: mean <b>{(tp.total_mean_s * 1000).toFixed(0)} ms</b>, p95{' '}
                  <b>{(tp.total_p95_s * 1000).toFixed(0)} ms</b>, worst {(tp.total_max_s * 1000).toFixed(0)} ms
                  {' · '}{tp.counts?.positions ?? '—'} positions, {tp.counts?.strategies ?? '—'} strategies
                  <Info text="Every tick is timed phase by phase and kept in a bounded ring. The share column says which part of the program is actually expensive right now; hover a phase for what it does and how it scales." />
                </div>
                {ph.map(([name, v]) => (
                  <div key={name} className="row" style={{ gap: 8, fontSize: 12, alignItems: 'center' }}
                       title={tp.what_each_phase_is?.[name] || ''}>
                    <span style={{ width: 78 }}>{name}</span>
                    <div style={{ flex: 1, height: 6, background: 'var(--surface-2)', borderRadius: 3 }}>
                      <div style={{ width: `${Math.max(1, v.share_pct)}%`, height: 6, background: 'var(--accent)', borderRadius: 3 }} />
                    </div>
                    <span className="num" style={{ width: 44, textAlign: 'right' }}>{v.share_pct.toFixed(0)}%</span>
                    <span className="mut num" style={{ width: 70, textAlign: 'right' }}>{(v.mean_s * 1000).toFixed(0)} ms</span>
                  </div>
                ))}
              </Card>
            )
          })()}

          <Card title="Stored">
            <div className="row" style={{ gap: 14, flexWrap: 'wrap' }}>
              {Object.entries(status.data?.data_counts || {}).map(([k, v]) => (
                <div key={k}>
                  <div className="label mut">
                    {k.replace(/_/g, ' ')}
                    <Info text={{
                      bars: 'Price history. Grows while the engine runs, and jumps when you backfill.',
                      quotes: 'Tick snapshots used to mark positions and measure spreads.',
                      signals: 'Every decision, including the ones rejected for being below the cost hurdle.',
                      orders: 'Every order attempt, and why it was blocked if it was.',
                      trades: 'Closed round trips — the only rows that prove anything.',
                      cost_observations: 'Real fills. These replace the cost prior with measurement.',
                    }[k] || k} />
                  </div>
                  <div className="mono" style={{ fontSize: 14 }}>{(v ?? 0).toLocaleString()}</div>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
      {pick && <CoinChart {...pick} onClose={() => setPick(null)} />}
    </>
  )
}
