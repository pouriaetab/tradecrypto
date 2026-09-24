/**
 * How you are running, and how to change it — permanently reachable.
 *
 * The first-run screen asks the question once and then never appears again,
 * which left no way back: someone who picked the demo could not clear it, and
 * someone who started empty could not look at a demo. The choice needs a home
 * that outlives the first five minutes.
 *
 * Also the honest answer to "how do I go fully live": you cannot, in this
 * build, and the reason is written on the panel rather than buried.
 */
import React, { useState } from 'react'
import { api } from '../lib/api.js'
import { Banner, Card, useAsync, clearAsyncCache } from './ui.jsx'

function Row({ title, body, action, tone }) {
  return (
    <div className={`rm-row ${tone || ''}`}>
      <div className="rm-text">
        <div className="rm-title">{title}</div>
        <div className="mut rm-body">{body}</div>
      </div>
      {action && <div className="rm-act">{action}</div>}
    </div>
  )
}

export default function RunMode() {
  const st = useAsync(() => api.firstRun(), [], 15000)
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const d = st.data

  async function go(kind, fn) {
    setErr(''); setBusy(kind)
    try {
      const r = await fn()
      if (r && r.ok === false) setErr(r.error || 'could not do that')
      clearAsyncCache()
      st.reload()
    } catch (e) { setErr(String(e.message || e)) }
    setBusy('')
  }

  if (!d) return null
  const rh = d.robinhood || {}
  const rm = d.real_money || {}

  const where = d.in_demo
    ? (d.engine_running
        ? 'Demo history, and it is collecting live on top of it'
        : 'Demo history only — it is not collecting anything new')
    : (d.engine_running
        ? 'Your own data, collected live since you started'
        : 'Your own data, but nothing is being collected right now')

  return (
    <Card title="How you are running">
      {err && <Banner kind="bad" title="that did not work">{err}</Banner>}

      <div className="rm-now">
        <span className="rm-now-k">right now</span>
        <span className="rm-now-v">{where}</span>
        {d.in_demo && <span className="pill warn">{d.demo_trades} demo trades</span>}
      </div>

      {!d.in_demo && (
        <Row
          title="Load the demo"
          body={d.demo_ready
            ? 'Fills every page by replaying the real strategies over real prices from the last month, paying the real 1.9% cost. It will sit alongside anything you have collected, and every row is stamped so it can be removed again.'
            : (d.demo_blocked_reason || 'not ready yet')}
          action={
            <button disabled={!d.demo_ready || busy === 'demo'}
                    onClick={() => go('demo', api.firstRunDemo)}>
              {busy === 'demo' ? 'building…' : 'load demo'}
            </button>}
        />
      )}

      {d.in_demo && (
        <Row
          title="Clear the demo"
          body="Removes every demo trade and leaves anything you collected yourself untouched. They are matched on their stamp, not on a date, so your own trades survive even if they are mixed in among them."
          action={
            <button className="danger" disabled={busy === 'clear'}
                    onClick={() => go('clear', api.firstRunClearDemo)}>
              {busy === 'clear' ? 'clearing…' : 'clear demo'}
            </button>}
        />
      )}

      <Row
        title={d.engine_running ? 'Collecting live (paper)' : 'Not collecting'}
        tone={d.engine_running ? 'on' : ''}
        body={d.engine_running
          ? 'Watching real prices and deciding as if trading, every few seconds. No real money is involved. New trades are added to whatever is already here.'
          : 'Nothing is being watched or decided. Start this to build up your own record — it is the real thing, just with pretend money.'}
        action={d.engine_running
          ? <button className="danger" disabled={busy === 'stop'}
                    onClick={() => go('stop', api.engineStop)}>stop</button>
          : <button className="primary" disabled={busy === 'start'}
                    onClick={() => go('start', api.engineStart)}>start collecting</button>}
      />

      <Row
        title="Start over with nothing"
        body="Empties the book completely — demo and your own — and begins again from today. There is no undo."
        action={
          <button className="danger" disabled={busy === 'live'}
                  onClick={() => {
                    if (window.confirm('Delete every trade, demo and your own, and start from empty?')) {
                      go('live', api.firstRunLive)
                    }
                  }}>
            empty it
          </button>}
      />

      <div className="rm-sep" />

      <Row
        title={rh.connected ? 'Robinhood connected' : 'Connect Robinhood (optional)'}
        tone={rh.connected ? 'on' : ''}
        body={
          <>
            Not needed to run. Prices come from Coinbase, free and with no
            account. What connecting adds is {rh.what_it_adds || 'which coins Robinhood will actually trade, and each coin’s own measured spread'}.
            {' '}It is still paper trading either way.
            <div style={{ marginTop: 4 }}>
              It is set up by putting a key file on this computer — see{' '}
              <span className="mono">docs/ROBINHOOD_API.md</span>. This page will
              never ask you for a password, and nothing you type here leaves this
              computer.
            </div>
          </>}
      />

      <Banner kind={rm.possible ? 'warn' : 'info'}
              title={rm.possible
                ? 'Real-money trading is UNLOCKED on this install'
                : 'Real-money trading is off, and cannot be turned on from here'}>
        {rm.how_it_stays_off ||
          'Placing real orders needs a confirmation phrase typed into the .env file by hand. No button in this interface can set it.'}
      </Banner>
    </Card>
  )
}
