/**
 * The day's stop, and the three ways to change your mind about it.
 *
 * All of this already existed and none of it was findable. "engage kill switch"
 * is in fact "stop for today, start again tomorrow" — in paper the switch
 * clears itself on the next trading day — but nothing said so, so the operator
 * asked where the button was while looking straight at it. And the limit itself
 * could only be changed by editing .env and restarting, which is the one thing
 * he has said repeatedly he does not want to do.
 *
 * So: one card, the current numbers, and three buttons whose labels say what
 * they do rather than what they are called internally.
 */
import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Banner, Card, useAsync, clearAsyncCache } from './ui.jsx'

function money(v) {
  if (v === null || v === undefined || !isFinite(v)) return '—'
  return (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(2)
}

export default function DayStop({ onChanged }) {
  const r = useAsync(() => api.riskStatus('paper'), [], 8000)
  const st = useAsync(() => api.status(), [], 10000)
  const [pct, setPct] = useState('')
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState(null)
  const d = r.data

  useEffect(() => {
    if (d && pct === '') setPct(String(d.daily_loss_pct ?? ''))
  }, [d]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!d) return null

  const engineOn = !!st.data?.engine?.running
  const loss = Number(d.realised_pnl_today_usd || 0)
  const limit = Number(d.daily_loss_limit_usd || 0)
  const used = limit > 0 ? Math.min(1, Math.max(0, -loss / limit)) : 0
  const stopped = !!d.kill_switch_engaged
  const waived = !!d.daily_loss_cap_waived_today

  const say = (text, kind = 'info') => setMsg({ text, kind })

  const act = async (key, fn, text, kind) => {
    setBusy(key); setMsg(null)
    try {
      await fn()
      clearAsyncCache()
      await r.reload(); await st.reload(); onChanged?.()
      say(text, kind)
    }
    catch (e) { say(String(e.message || e), 'bad') }
    setBusy('')
  }

  const saveLimit = async () => {
    setBusy('limit'); setMsg(null)
    try {
      const res = await api.riskDailyLimit(pct)
      clearAsyncCache()
      await r.reload()
      onChanged?.()
      say(`the day's stop is now ${res.pct}% of your stake — ${money(res.now_usd ?? limit)}`, 'good')
    } catch (e) { say(String(e.message || e), 'bad') }
    setBusy('')
  }

  return (
    <Card title="The day's stop">
      {msg && <Banner kind={msg.kind === 'good' ? 'info' : msg.kind}>{msg.text}</Banner>}

      <div className="ds-state">
        {stopped
          ? <><b className="neg">Stopped for today.</b> No new position can open.
              {' '}It starts again by itself tomorrow — or press <i>start again now</i>.</>
          : waived
            ? <><b>Running, with today's stop waived.</b> You released it after the
                loss passed the cap, so it will not stop you again until tomorrow.</>
            : engineOn
              ? <><b className="pos">Running.</b> It stops itself if today's loss reaches the cap below.</>
              : <><b>Not trading.</b> You stopped it entirely — this survives restarts
                  until you start it again.</>}
      </div>

      <div className="ds-bar-wrap">
        <div className="ds-bar">
          <div className={`ds-fill ${used >= 1 ? 'full' : used > 0.6 ? 'warn' : ''}`}
               style={{ width: `${used * 100}%` }} />
        </div>
        <div className="ds-bar-k">
          <span>today: <b className={loss < 0 ? 'neg' : 'pos'}>{money(loss)}</b></span>
          <span className="mut">stops at {money(-limit)}</span>
        </div>
      </div>

      <div className="ds-limit">
        <label className="ds-k">stop the day after losing</label>
        <input className="ds-in" type="number" min="0.5" max="50" step="0.5"
               value={pct} onChange={(e) => setPct(e.target.value)} />
        <span className="ds-suffix">% of your {money(d.stake_usd)} stake</span>
        <span className="mut ds-eq">= {money((Number(pct) || 0) * Number(d.stake_usd || 0) / 100)}</span>
        <button className="primary" disabled={busy === 'limit' || String(d.daily_loss_pct) === pct}
                onClick={saveLimit}>
          {busy === 'limit' ? 'saving…' : 'save'}
        </button>
        {d.daily_loss_pct_source === 'operator' && (
          <button className="why" disabled={busy === 'reset'}
                  onClick={() => act('reset', () => api.riskDailyLimit('default'),
                                     'back to the value in your .env file')}>
            reset to default
          </button>
        )}
      </div>
      <div className="mut ds-note">
        Takes effect immediately — nothing to restart. Allowed range is
        {' '}{(d.daily_loss_pct_bounds || [0.5, 50])[0]}% to {(d.daily_loss_pct_bounds || [0.5, 50])[1]}%:
        a stop of zero halts on the first cent, and one too large is not a stop.
      </div>

      <div className="ds-acts">
        {!stopped && (
          <button className="danger" disabled={busy === 'kill'}
                  onClick={() => act('kill', () => api.kill('stopped for today from the dashboard'),
                    'stopped for today — it starts again by itself tomorrow', 'bad')}>
            Stop for today
          </button>
        )}
        {(stopped || waived) && (
          <button className="primary" disabled={busy === 'release'}
                  onClick={() => act('release', api.release,
                    'running again — and today’s cap will not stop you a second time', 'good')}>
            Start again now
          </button>
        )}
        {engineOn
          ? <button disabled={busy === 'engine'}
                    onClick={() => act('engine', api.engineStop,
                      'trading stopped until you start it again', 'bad')}>
              Stop until I say
            </button>
          : <button disabled={busy === 'engine'}
                    onClick={() => act('engine', api.engineStart, 'trading again', 'good')}>
              Start trading
            </button>}
      </div>

      <div className="mut ds-note">
        <b>Stop for today</b> is the day's cap — it clears itself on the next
        trading day, so you do not have to remember to turn it back on.{' '}
        <b>Stop until I say</b> stops everything and stays stopped across
        restarts. Either way, positions you already hold keep their own stops
        and are still managed.
      </div>
    </Card>
  )
}
