import React from 'react'
import { api, fmt } from '../lib/api.js'
import DayStop from '../components/DayStop.jsx'
import { Banner, Card, Hint, Info, Stat, Table, useAsync } from '../components/ui.jsx'

/* An elapsed time a person reads at a glance: "3h 12m", "48m", "2d 4h".
 * Seconds are noise at these scales and a raw seconds count is unreadable. */
function hhmm(sec) {
  if (sec == null || !Number.isFinite(sec)) return '—'
  const s = Math.max(0, Math.round(sec))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  return `${m}m`
}

export default function Risk({ onModel }) {
  const r = useAsync(() => api.riskStatus('paper'), [], 8000)
  const safety = useAsync(() => api.safety(), [], 15000)
  const liveness = useAsync(() => api.liveness(), [], 60000)
  const d = r.data
  // Every click answers. "release" used to do its work silently, and a button
  // that says nothing when pressed is indistinguishable from one that is broken.
  const [note, setNote] = React.useState(null)
  // Two views of the same open book. "now" is what it is worth; "high & low" is
  // how far up and down it has been since entry, which is the number the exit
  // argument turns on and which a now-only table structurally cannot show.
  const [posView, setPosView] = React.useState('now')
  const path = useAsync(() => api.positionsPath(), [], 30000)
  const plans = useAsync(() => api.tradePlans(), [], 30000)
  const acc = useAsync(() => api.tradePlansAccuracy(), [], 60000)
  const say = (text, kind = 'info') => {
    setNote({ text, kind, at: Date.now() })
    setTimeout(() => setNote((n) => (n && Date.now() - n.at >= 5900 ? null : n)), 6000)
  }
  const onRelease = async () => {
    const wasOn = !!d?.kill_switch_engaged
    try {
      await api.release()
      await r.reload()
      say(wasOn ? 'kill switch released — new positions may open again'
                : 'nothing to release — the kill switch was not engaged', wasOn ? 'good' : 'info')
    } catch (e) { say(`release failed: ${e.message}`, 'bad') }
  }
  const onKill = async () => {
    try {
      await api.kill('engaged from dashboard')
      await r.reload()
      say('kill switch ENGAGED — no new position can open until you release it. Open positions keep their stops.', 'bad')
    } catch (e) { say(`could not engage: ${e.message}`, 'bad') }
  }

  return (
    <>
      <h1>Risk</h1>

      {/* Everything about stopping and restarting, in one card with the current
          numbers. The buttons below still exist for the detail; this is the one
          a person looks for. */}
      <DayStop onChanged={() => r.reload()} />
      <p className="sub">
        Every order passes all of these checks or it does not exist.<Info text="The kill switch is a file on disk, so it keeps working even if this page does not, and it survives a restart." />
      </p>

      {d?.kill_switch_engaged && (
        <Banner kind="bad" title="Kill switch engaged — nothing can trade">
          {d.kill_switch_reason}
          {(d.daily_loss_headroom_usd ?? 1) <= 0 && (
            <div style={{ marginTop: 6 }}>
              Today's loss is past the cap. Pressing release waives the daily loss cap for the rest
              of today — it will not re-arm for this reason until midnight. The drawdown ceiling and
              every per-order check still apply.
            </div>
          )}
        </Banner>
      )}
      {!d?.kill_switch_engaged && d?.daily_loss_cap_waived_today && (
        <Banner kind="info" title="Daily loss cap waived for today">
          You released the kill switch with today's loss past the {fmt.usd(d?.daily_loss_limit_usd)} cap,
          so it stays off for the rest of today. It resets at midnight. Raise the cap itself with
          TC_MAX_DAILY_LOSS_PCT in .env (needs a restart) if this keeps happening.
        </Banner>
      )}
      {safety.data && (
        <Banner kind={safety.data.live_enabled ? 'bad' : 'info'}
                title={safety.data.live_enabled ? 'Live trading is ENABLED' : 'Live trading is disabled'}>
          {safety.data.explanation}
        </Banner>
      )}

      <div className="row" style={{ marginBottom: 14 }}>
        {/* The clearer pair lives in "The day's stop" at the top of this page.
            Two sets of the same two buttons on one screen is a question about
            which one is real. */}
        {note && <span className={note.kind === 'bad' ? 'neg' : note.kind === 'good' ? 'pos' : 'mut'}
                       style={{ fontSize: 12, alignSelf: 'center' }}>{note.text}</span>}
        <div className="spacer" />
        <button className="why" onClick={() => onModel('risk_guards')}>how these checks work</button>
      </div>

      <div className="grid c4">
        <Stat label="Daily loss headroom" value={fmt.usd(d?.daily_loss_headroom_usd)} small
              meta={`cap ${fmt.usd(d?.daily_loss_limit_usd)}${d?.daily_loss_cap_waived_today ? ' · waived for today' : ''}`}
              tone={(d?.daily_loss_headroom_usd ?? 1) > 0 ? 'pos' : d?.daily_loss_cap_waived_today ? 'warn' : 'neg'} />
        <Stat label="Book at risk" value={fmt.usd(d?.open_risk_usd)} small
              meta={`if every stop fires · ceiling ${fmt.usd(d?.book_risk_headroom_usd)} (drawdown budget)${d?.book_risk_exceeds_daily_cap ? ' · more than one day\'s cap' : ''}`}
              tone={(d?.open_risk_usd ?? 0) > (d?.book_risk_headroom_usd ?? 1) ? 'neg' : d?.book_risk_exceeds_daily_cap ? 'warn' : 'pos'} />
        <Stat label="Drawdown" value={fmt.pct(d?.drawdown_pct)} small meta={`ceiling ${fmt.pct(d?.max_drawdown_pct)}`} />
        <Stat label="Open positions" value={`${d?.open_positions?.length ?? 0}`} small
              meta={d?.max_concurrent_positions > 0 ? `cap ${d.max_concurrent_positions}` : 'no cap — cash is the limit'} />
        <Stat label="Orders today" value={`${d?.orders_today ?? 0} / ${d?.max_trades_per_day ?? '—'}`} small
              meta="each round trip pays the spread" />
      </div>

      {/* ── is every safety mechanism actually running? ───────────────────
          The Risk tab is where you come to ask "am I protected". A guard that
          exists in the code and has never executed is not protection, and until
          this card existed nothing anywhere could tell the difference. */}
      <Card title="mechanisms — has each one actually run?">
        <Hint more={'Every mechanism that matters increments a counter when it does its job. '
          + 'A green row has genuinely happened. A red row exists in the code and has NEVER '
          + 'executed, and nobody wrote down why — that is the state that let a trailing stop '
          + 'be reported as working for weeks while its code was never entered. An amber row '
          + 'has never run either, but it is DECLARED, with a reason and a date.'}>
          Built and running are different words. This card is the difference.
        </Hint>
        <Table
          cols={[
            { key: 'name', label: 'mechanism' },
            { key: 'what', label: 'what it does' },
            { key: 'times', label: 'times', num: true,
              render: (m) => (m.times
                ? <span className="pos">{m.times.toLocaleString()}</span>
                : <span className={m.known_not_live ? '' : 'neg'}>never</span>) },
            { key: 'last_age_s', label: 'last', num: true,
              render: (m) => (m.last_age_s == null ? '—'
                : m.last_age_s < 3600 ? `${Math.round(m.last_age_s / 60)}m ago`
                : `${Math.round(m.last_age_s / 3600)}h ago`) },
            { key: 'state', label: 'state',
              render: (m) => (m.alarming
                ? <span className="neg">NEVER RAN — undeclared</span>
                : m.known_not_live
                  ? <span style={{ color: 'var(--warning)' }} title={m.known_not_live}>
                      known not live
                    </span>
                  : m.times ? <span className="pos">running</span>
                            : <span className="mut">not yet</span>) },
          ]}
          rows={liveness.data?.mechanisms || []}
          empty="the mechanism registry has not reported yet" />
        {!!(liveness.data?.known_not_live || []).length && (
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            {(liveness.data.mechanisms || [])
              .filter((m) => m.known_not_live)
              .map((m) => <div key={m.name}>· <b>{m.name}</b> — {m.known_not_live}</div>)}
          </div>
        )}
      </Card>

      <h2>Open positions</h2>
      <Card>
        <div className="row" style={{ gap: 6, marginBottom: 8 }}>
          {[['now', 'what they are worth now'],
            ['range', 'how far up and down they have been'],
            ['plan', 'what we predicted at entry, and how it is going']].map(([k, title]) => (
            <button key={k} className="btn" aria-pressed={posView === k} title={title}
                    style={posView === k ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : null}
                    onClick={() => setPosView(k)}>
              {k === 'now' ? 'now' : k === 'range' ? 'high & low' : 'the plan'}
            </button>
          ))}
          <span className="mut" style={{ fontSize: 11, alignSelf: 'center' }}>
            {posView === 'now'
              ? 'current mark, stop and target'
              : posView === 'range'
              ? 'best and worst this position has been since entry — both sides of the spread charged'
              : 'the prediction written down at entry, and the verdict on it since'}
          </span>
        </div>
        {posView === 'plan' ? (
          plans.err ? <Banner tone="neg">{plans.err}</Banner>
          : !plans.data ? <div className="mut">loading…</div>
          : !(plans.data.plans || []).length
            ? <div className="mut">No plans recorded yet. One is written the next time a
                position opens — existing positions predate this.</div>
          : (
            <>
              {!!(acc.data?.rows || []).length && (
                <div style={{ marginBottom: 12 }}>
                  <div className="mut" style={{ fontSize: 11, marginBottom: 6 }}>
                    <b>Was the prediction any good?</b> “hit” is how often the coin
                    actually reached the predicted price inside its window. “said”
                    is how often the model expected to. A big negative gap means the
                    strategy promises more than it delivers.
                  </div>
                  <Table
                    rows={acc.data.rows}
                    cols={[
                      { key: 'strategy', label: 'strategy' },
                      { key: 'n', label: 'trades', num: true },
                      { key: 'hit_rate', label: 'hit', num: true,
                        render: (r) => `${(r.hit_rate || 0).toFixed(0)}%` },
                      { key: 'predicted_rate', label: 'said it would', num: true,
                        render: (r) => `${(r.predicted_rate || 0).toFixed(0)}%` },
                      { key: 'calibration_gap', label: 'gap', num: true,
                        render: (r) => (
                          <span style={{ color: (r.calibration_gap || 0) < -10
                            ? 'var(--neg)' : 'inherit' }}>
                            {(r.calibration_gap || 0) >= 0 ? '+' : ''}
                            {(r.calibration_gap || 0).toFixed(0)}
                          </span>) },
                      { key: 'avg_ratio', label: 'avg target vs hourly move', num: true,
                        render: (r) => `${(r.avg_ratio || 0).toFixed(1)}x` },
                      { key: 'on_track', label: 'reached it', num: true },
                      { key: 'exit_seeking', label: 'missed but green', num: true },
                      { key: 'failed', label: 'failed', num: true },
                    ]}
                  />
                </div>
              )}
              <div className="mut" style={{ fontSize: 11, marginBottom: 8 }}>
                <b>on track</b> doing what we said · <b>wobbling</b> still inside its
                window but the path has already broken · <b>failed</b> past the window
                without reaching the price · <b>exit seeking</b> failed, but it is green
                right now, which is a way out. Rows marked <b>rebuilt</b> were
                reconstructed from the bars that existed before the fill — the
                strategy's own numbers, but nobody wrote them down in advance.
              </div>
              <Table
                rows={plans.data.plans}
                cols={[
                  { key: 'symbol', label: 'coin' },
                  { key: 'strategy', label: 'strategy' },
                  { key: 'state', label: 'verdict',
                    render: (r) => (
                      <span style={{ color:
                        r.state === 'failed' ? 'var(--neg)'
                        : r.state === 'wobbling' ? '#c90'
                        : r.state === 'exit_seeking' ? 'var(--accent)'
                        : 'var(--pos)' }}>{String(r.state || '').replace('_', ' ')}</span>) },
                  { key: 'entry_px', label: 'bought at', num: true, render: (r) => fmt.px(r.entry_px) },
                  { key: 'target_px', label: 'predicted', num: true, render: (r) => fmt.px(r.target_px) },
                  { key: 'target_frac', label: 'that is', num: true,
                    render: (r) => (r.target_frac == null ? '—' : `${(r.target_frac * 100).toFixed(2)}%`) },
                  { key: 'window_h', label: 'by hour', num: true,
                    render: (r) => (r.window_h == null ? '—' : `${r.window_h}h`) },
                  { key: 'p_reach', label: 'odds it arrives', num: true,
                    render: (r) => (r.p_reach == null ? '—' : `${(r.p_reach * 100).toFixed(0)}%`) },
                  { key: 'ratio', label: 'target vs its hourly move', num: true,
                    render: (r) => (r.ratio == null ? '—' : `${r.ratio.toFixed(1)}x`) },
                  { key: 'expected_dip_pct', label: 'dip we expect', num: true,
                    render: (r) => (r.expected_dip_pct == null ? '—' : `${r.expected_dip_pct.toFixed(1)}%`) },
                  { key: 'worst_pct', label: 'worst so far', num: true,
                    render: (r) => (r.worst_pct == null ? '—' : `${r.worst_pct.toFixed(2)}%`) },
                  { key: 'best_pct', label: 'best so far', num: true,
                    render: (r) => (r.best_pct == null ? '—' : `${r.best_pct.toFixed(2)}%`) },
                  { key: 'rebuilt', label: 'written when',
                    value: (r) => ((r.why || {}).reconstructed ? 'rebuilt' : 'at entry'),
                    render: (r) => ((r.why || {}).reconstructed
                      ? <span className="mut">rebuilt</span>
                      : <span style={{ color: 'var(--pos)' }}>at entry</span>) },
                  { key: 'notes', label: 'what happened',
                    value: (r) => ((r.notes || []).slice(-1)[0] || {}).note || '',
                    render: (r) => <span className="mut">{((r.notes || []).slice(-1)[0] || {}).note || '—'}</span> },
                ]}
              />
            </>
          )
        ) : posView === 'range' ? (
          path.err ? <Banner tone="neg">{path.err}</Banner>
          : !path.data ? <div className="mut">loading…</div>
          : (
            <>
              <Table
                cols={[
                  { key: 'symbol', label: 'symbol' },
                  { key: 'strategy', label: 'strategy' },
                  { key: 'cost_basis_usd', label: 'size', num: true,
                    value: (x) => x.cost_basis_usd,
                    render: (x) => fmt.usd(x.cost_basis_usd) },
                  { key: 'peak_usd', label: 'best it has been', num: true,
                    value: (x) => x.peak_usd,
                    info: "The most this position was ever worth if it had been sold at that moment, with the buy spread already in the entry price and the sell spread charged on the way out.",
                    render: (x) => (x.peak_usd == null ? <span className="mut">—</span>
                      : <span className={x.peak_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(x.peak_usd)}</span>) },
                  { key: 'peak_after_s', label: 'reached after', num: true,
                    value: (x) => x.peak_after_s,
                    render: (x) => (x.peak_after_s == null ? <span className="mut">—</span> : hhmm(x.peak_after_s)) },
                  { key: 'trough_usd', label: 'worst it has been', num: true,
                    value: (x) => x.trough_usd,
                    render: (x) => (x.trough_usd == null ? <span className="mut">—</span>
                      : <span className={x.trough_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(x.trough_usd)}</span>) },
                  { key: 'trough_after_s', label: 'reached after', num: true,
                    value: (x) => x.trough_after_s,
                    render: (x) => (x.trough_after_s == null ? <span className="mut">—</span> : hhmm(x.trough_after_s)) },
                  { key: 'now_usd', label: 'P&L now', num: true,
                    value: (x) => x.now_usd,
                    render: (x) => (x.now_usd == null ? <span className="mut">—</span>
                      : <span className={x.now_usd >= 0 ? 'pos' : 'neg'}><b>{fmt.usd(x.now_usd)}</b></span>) },
                  { key: 'given_back_usd', label: 'given back', num: true,
                    value: (x) => x.given_back_usd,
                    info: "Best it has been, minus what it is worth now. How much of the peak is still on the table.",
                    render: (x) => (x.given_back_usd == null ? <span className="mut">—</span>
                      : <span className={x.given_back_usd > 0 ? 'neg' : 'mut'}>{fmt.usd(x.given_back_usd)}</span>) },
                  { key: 'age_s', label: 'held for', num: true,
                    value: (x) => x.age_s, render: (x) => hhmm(x.age_s) },
                  { key: 'granularity_s', label: 'resolution',
                    info: "The times are located to the bar the extreme fell in, not the minute.",
                    render: (x) => (x.granularity_s
                      ? <span className="mut">{Math.round(x.granularity_s / 60)}m bars</span>
                      : <span className="mut" title={x.why || ''}>no bars</span>) },
                ]}
                rows={path.data.positions || []} empty="flat" />
              <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
                {path.data.totals?.reconstructed ?? 0} of {path.data.totals?.n ?? 0} reconstructed ·
                {' '}best together {fmt.usd(path.data.totals?.peak_usd)} ·
                {' '}worst together {fmt.usd(path.data.totals?.trough_usd)} ·
                {' '}now {fmt.usd(path.data.totals?.now_usd)} ·
                {' '}<b>given back {fmt.usd(path.data.totals?.given_back_usd)}</b>
                <br />{path.data.note}
              </div>
            </>
          )
        ) : (
        <Table
          cols={[
            { key: 'symbol', label: 'symbol' }, { key: 'strategy', label: 'strategy' },
            { key: 'qty', label: 'qty', num: true, render: (x) => fmt.num(x.qty, 4) },
            { key: 'avg_px', label: 'entry', num: true, render: (x) => fmt.px(x.avg_px) },
            { key: 'price', label: 'now', num: true, render: (x) => fmt.px(x.price) },
            { key: 'net_if_closed_usd', label: 'P&L now', num: true,
              info: 'What selling this position at this instant would book, AFTER '
                  + "Robinhood's sell-side spread. The smaller number underneath is the "
                  + 'same thing before costs (mid to mid). Green means a sale right now '
                  + 'is a real profit, not just a coin that is up.',
              render: (x) => (x.net_if_closed_usd == null ? '—'
                : <span className={x.net_if_closed_usd >= 0 ? 'pos' : 'neg'}
                        title={`gross ${fmt.usd(x.unrealised_usd)} (${fmt.pct(x.unrealised_pct)})`}>
                    {fmt.usd(x.net_if_closed_usd)}
                    <span className="mut" style={{ marginLeft: 4, fontSize: 11 }}>{fmt.pct(x.net_if_closed_pct, 1)}</span>
                  </span>) },
            { key: 'stop_px', label: 'stop', num: true, render: (x) => fmt.px(x.stop_px) },
            /* The two questions a stop actually has to answer. */
            { key: 'stop_kind', label: 'stop type',
              info: 'A TRAILING stop follows the price up and never comes back down. '
                  + 'A FIXED stop is set at entry and stays there whatever the coin does. '
                  + 'Only strategies that ask for one get a trailing stop — if this says '
                  + 'fixed, the stop will not move no matter how far the coin runs.',
              render: (x) => (x.stop_kind === 'trailing'
                ? <span className="pos">trailing{x.trail_pct ? ` ${x.trail_pct.toFixed(1)}%` : ''}</span>
                : <span className="mut">fixed</span>) },
            { key: 'stop_moves', label: 'moved', num: true,
              info: 'How many times this stop has actually been raised. 0 means it is '
                  + 'sitting exactly where it was put at entry.',
              render: (x) => (x.stop_moves > 0
                ? <span className="pos">{x.stop_moves}×</span>
                : <span className="mut">never</span>) },
            { key: 'stop_room_pct', label: 'room', num: true,
              info: 'How far the coin can fall from here before the stop fires, as a '
                  + 'percentage of the current price. This is the number to act on — '
                  + 'the stop price on its own does not tell you how exposed you are.',
              render: (x) => (x.stop_room_pct == null ? '—'
                : <span className={x.stop_room_pct < 3 ? 'neg' : ''}>
                    {x.stop_room_pct.toFixed(1)}%
                  </span>) },
            { key: 'stop_above_entry', label: 'safe', num: true,
              info: 'The stop is at or above the entry price, so this position can no '
                  + 'longer lose money on the way out.',
              render: (x) => (x.stop_above_entry
                ? <span className="pos">yes</span>
                : <span className="mut">no</span>) },
            { key: 'target_px', label: 'target', num: true, render: (x) => fmt.px(x.target_px) },
            { key: 'opened_ts', label: 'opened', render: (x) => fmt.time(x.opened_ts) },
          ]}
          rows={d?.open_positions || []} empty="flat" />
        )}
        {posView === 'now' && !!(d?.open_positions || []).length && (() => {
          const ps = d.open_positions
          const net = ps.reduce((a, x) => a + (x.net_if_closed_usd || 0), 0)
          const gross = ps.reduce((a, x) => a + (x.unrealised_usd || 0), 0)
          return (
            <div className="mut" style={{ fontSize: 12, marginTop: 8 }}>
              whole open book if closed right now:{' '}
              <b className={net >= 0 ? 'pos' : 'neg'}>{fmt.usd(net)}</b> after spread
              {' · '}{fmt.usd(gross)} before it{' · '}{ps.length} position{ps.length === 1 ? '' : 's'}
            </div>
          )
        })()}
      </Card>
    </>
  )
}
