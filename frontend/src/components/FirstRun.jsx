/**
 * The first screen, and the demo bar that follows it.
 *
 * An empty dashboard is indistinguishable from a broken one. That is what a new
 * person actually meets: every page says "insufficient evidence", every chart is
 * blank, and there is no way to tell whether the program works, because it has
 * not done anything yet and will not for days.
 *
 * So this asks one question with two answers, and gets out of the way forever
 * once it is answered.
 */
import React, { useEffect, useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { clearAsyncCache } from './ui.jsx'

function Choice({ title, lead, points, button, tone, busy, disabled, note, onPick }) {
  return (
    <div className="card fr-choice">
      <div className="fr-choice-title">{title}</div>
      <div className="mut fr-choice-lead">{lead}</div>
      <ul className="fr-points">
        {points.map((p, i) => <li key={i}>{p}</li>)}
      </ul>
      <button
        className={tone === 'primary' ? 'primary' : ''}
        disabled={busy || disabled}
        onClick={onPick}
        style={{ width: '100%', padding: '10px 14px', fontSize: '15px' }}
      >
        {busy ? 'working…' : button}
      </button>
      {note && <div className="mut fr-note">{note}</div>}
    </div>
  )
}

export default function FirstRunGate({ children }) {
  const [st, setSt] = useState(null)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')

  const load = React.useCallback(() => {
    api.firstRun().then(setSt).catch(e => setErr(String(e.message || e)))
  }, [])

  useEffect(() => { load() }, [load])

  // While the demo cannot run yet the background backfill is still filling
  // `bars`. Rather than failing on the click, the button enables itself.
  useEffect(() => {
    if (!st || !st.needs_choice || st.demo_ready) return
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [st, load])

  async function pick(kind) {
    setErr(''); setBusy(kind)
    try {
      const res = kind === 'demo' ? await api.firstRunDemo() : await api.firstRunLive()
      if (res && res.ok === false) { setErr(res.error || 'could not do that'); setBusy(''); return }
      clearAsyncCache()
      const next = await api.firstRun()
      setSt(next)
    } catch (e) {
      setErr(String(e.message || e))
    }
    setBusy('')
  }

  // PT's ask: from inside the demo, start the real thing WITHOUT throwing the
  // demo book away, so you can watch a live trade land next to the replayed
  // ones and see how it works.
  async function goLive() {
    setBusy('golive')
    try {
      await api.engineStart()
      clearAsyncCache()
      setSt(await api.firstRun())
    } catch (e) { setErr(String(e.message || e)) }
    setBusy('')
  }

  async function clearDemo() {
    setBusy('clear')
    try {
      await api.firstRunClearDemo()
      clearAsyncCache()
      setSt(await api.firstRun())
    } catch (e) { setErr(String(e.message || e)) }
    setBusy('')
  }

  // Never flash the chooser at someone who has already answered.
  if (!st) return children

  if (st.needs_choice) {
    return (
      <div className="fr-wrap">
        <h1 className="fr-h1">What would you like to see first?</h1>
        <div className="mut fr-sub">
          Nothing here touches real money, either way. You can change your mind
          at any time.
        </div>
        {err && <div className="banner bad" style={{ marginBottom: 14 }}>{err}</div>}

        <div className="fr-grid">
          <Choice
            title="Show me a demo"
            tone="primary"
            lead="Fills every page with real trades so you can see what this is."
            points={[
              'Runs the same strategies over real prices from the last month',
              'Pays the same 1.9% cost a real trade pays',
              'Ready in a couple of seconds',
              'You can switch to live later, or wipe it, with one click',
            ]}
            button="Load the demo"
            busy={busy === 'demo'}
            disabled={!st.demo_ready}
            note={st.demo_ready
              ? 'Real prices, real rules. Not invented numbers.'
              : st.demo_blocked_reason}
            onPick={() => pick('demo')}
          />
          <Choice
            title="Start collecting my own"
            lead="Begin from nothing. Your numbers, from today."
            points={[
              'Watches live prices and decides as if trading',
              'Still pretend money — nothing can be lost',
              'Pages stay mostly empty for the first day or two',
              'This is the real thing, just slower to look at',
            ]}
            button="Start live paper trading"
            busy={busy === 'live'}
            onPick={() => pick('live')}
          />
        </div>

        <div className="mut fr-foot">
          Either way it needs no account, no password and no card — it reads
          public prices from Coinbase.
        </div>
      </div>
    )
  }

  return (
    <>
      {err && <div className="banner bad">{err}</div>}
      {st.in_demo && (
        <div className="banner warn fr-bar">
          <div>
            <b>You are looking at the demo.</b>{' '}
            <span className="mut">
              {fmt.int ? fmt.int(st.demo_trades) : st.demo_trades} trades, replayed
              from real prices from the last month using the same rules and the
              same 1.9% cost. They are not yours and not live.
            </span>
          </div>
          <div className="fr-bar-actions">
            <button className="primary" onClick={goLive} disabled={busy === 'golive'}>
              {busy === 'golive' ? 'starting…' : 'Go live — keep this history'}
            </button>
            <button onClick={clearDemo} disabled={busy === 'clear'}>
              {busy === 'clear' ? 'clearing…' : 'Clear the demo and start fresh'}
            </button>
          </div>
        </div>
      )}
      {children}
    </>
  )
}
