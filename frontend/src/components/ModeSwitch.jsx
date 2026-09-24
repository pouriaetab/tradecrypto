import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Info } from './ui.jsx'

export default function ModeSwitch() {
  const [st, setSt] = useState(null)
  const [msg, setMsg] = useState(null)

  const load = () => api.mode().then(setSt).catch(() => {})
  useEffect(() => { load(); const id = setInterval(load, 20000); return () => clearInterval(id) }, [])

  const pick = async (v) => {
    setMsg(null)
    try {
      const d = await api.setMode(v)
      setSt(d.state)
      if (!d.changed) setMsg(d.reason)
    } catch (e) { setMsg(e.message) }
  }

  if (!st) return null
  return (
    <div className="mode-switch">
      <div className="label mut" style={{ marginBottom: 5 }}>
        mode
        <Info text="Paper simulates fills against live quotes. Advisory posts tickets for you to place by hand. Live places real orders and cannot be enabled from this toggle — it needs TC_LIVE_CONFIRM in .env plus a restart, so a mis-click can never risk money." />
      </div>
      <div className="seg">
        {st.options.map((o) => (
          <button key={o.value}
                  className={`${st.mode === o.value ? 'on' : ''} ${o.value === 'mcp' ? 'live' : ''}`}
                  disabled={!o.enabled}
                  title={o.enabled ? o.description : o.locked_reason}
                  onClick={() => pick(o.value)}>
            {o.label}
          </button>
        ))}
      </div>
      <div className="mode-note">
        {st.live_active ? '⚠ real orders enabled' : 'no real orders possible'}
      </div>
      {msg && <div className="mode-note" style={{ color: 'var(--warning)' }}>{msg}</div>}
    </div>
  )
}
