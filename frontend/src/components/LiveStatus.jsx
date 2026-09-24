/**
 * Is this thing actually running right now?
 *
 * The engine row already existed, but it was one line of small grey text among
 * a dozen other small grey things, and the operator -- who built it -- still
 * could not tell at a glance whether the app was trading. Somebody who did not
 * build it has no chance.
 *
 * So this says it once, large, at the top: running or not, when it last
 * decided anything, and when it will decide again. The countdown matters more
 * than it looks: a static "last tick 14:22:01" cannot be told apart from a
 * frozen program, and a number moving every second can.
 */
import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { useAsync } from './ui.jsx'

function ago(ts) {
  if (!ts) return null
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

export default function LiveStatus() {
  const status = useAsync(() => api.status(), [], 10000)
  const [, tick] = useState(0)

  // Re-render every second so the clock moves. Nothing is fetched here -- the
  // data still refreshes on its own 10s poll.
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [])

  const d = status.data
  const eng = d?.engine
  if (!d) return null

  const running = !!eng?.running
  const last = eng?.last_tick ? Number(eng.last_tick) : null
  const every = Number(eng?.poll_interval_s || 0)
  const since = last ? Math.max(0, Math.round(Date.now() / 1000 - last)) : null
  const nextIn = (running && every && since != null)
    ? Math.max(0, every - (since % every)) : null

  // A tick that has not happened in several intervals is not "running", whatever
  // the flag says. Saying LIVE over a stalled loop is the one thing this panel
  // must never do.
  const stalled = running && since != null && every > 0 && since > every * 4

  return (
    <div className={`ls ${running ? (stalled ? 'ls-stall' : 'ls-on') : 'ls-off'}`}>
      <div className="ls-main">
        <span className="ls-dot" aria-hidden />
        <span className="ls-word">
          {running ? (stalled ? 'NOT RESPONDING' : 'LIVE') : 'PAUSED'}
        </span>
        <span className="ls-say">
          {running
            ? (stalled
                ? `no decision for ${ago(last)} — it should be every ${every}s`
                : 'watching prices and deciding, on its own')
            : 'you stopped it — it will stay stopped across restarts'}
        </span>
      </div>

      <div className="ls-facts">
        <div className="ls-fact">
          <div className="ls-k">last decision</div>
          <div className="ls-v">{last ? ago(last) : 'none yet'}</div>
        </div>
        {running && !stalled && (
          <div className="ls-fact">
            <div className="ls-k">next in</div>
            <div className="ls-v">{nextIn != null ? `${nextIn}s` : '—'}</div>
          </div>
        )}
        <div className="ls-fact">
          <div className="ls-k">coins watched</div>
          <div className="ls-v">{d.universe_size ?? '—'}</div>
        </div>
        <div className="ls-fact">
          <div className="ls-k">strategies</div>
          <div className="ls-v">{(eng?.strategies || []).length || '—'}</div>
        </div>
        <div className="ls-fact">
          <div className="ls-k">decisions made</div>
          <div className="ls-v">{d.data_counts?.signals ?? '—'}</div>
        </div>
        <div className="ls-fact">
          <div className="ls-k">trades closed</div>
          <div className="ls-v">{d.data_counts?.trades ?? '—'}</div>
        </div>
        <div className="ls-fact">
          <div className="ls-k">money</div>
          <div className="ls-v ls-paper">{eng?.mode === 'mcp' ? 'REAL' : 'pretend'}</div>
        </div>
      </div>

      <div className="ls-act">
        {running
          ? <button className="danger" onClick={() => api.engineStop().then(status.reload)}>
              stop trading
            </button>
          : <button className="primary" onClick={() => api.engineStart().then(status.reload)}>
              start trading
            </button>}
      </div>
    </div>
  )
}
