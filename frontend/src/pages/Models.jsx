import React from 'react'
import { Card, Info, useAsync } from '../components/ui.jsx'
import { api } from '../lib/api.js'

export default function Models({ onModel }) {
  const cards = useAsync(() => api.models(), [])
  return (
    <>
      <h1>Models</h1>
      <p className="sub">
        Every model, with its formula, assumptions, failure modes and citation.<Info text="If a number appears anywhere in this app and is not traceable to one of these cards, that is a bug worth reporting." />
      </p>
      <div className="grid c2">
        {(cards.data || []).map((c) => (
          <Card key={c.name} title={c.name}
                right={<span className={`pill ${c.status === 'validated' ? 'ok' : 'warn'}`}>{c.status}</span>}>
            <div style={{ marginBottom: 8 }}>{c.question}</div>
            <div className="mut" style={{ fontSize: 12, marginBottom: 10 }}>{c.plain_english}</div>
            <div className="row">
              <span className="mut mono" style={{ fontSize: 11 }}>{c.code_ref}</span>
              <div className="spacer" />
              <button className="why" onClick={() => onModel(c.name)}>open full card</button>
            </div>
          </Card>
        ))}
      </div>
    </>
  )
}
