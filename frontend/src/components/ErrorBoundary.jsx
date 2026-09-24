import React from 'react'

/**
 * Keeps one broken page from taking down the whole app.
 *
 * Why this exists: Journal rendered a CoinChart element without importing it. The first
 * click on a symbol threw a ReferenceError inside render, and because there was
 * no boundary anywhere in the tree, React 18 unmounted EVERYTHING -- including
 * the sidebar nav. The operator got a blank white page with no way back and had
 * to close the app. The import bug was one line; the blank page was the missing
 * boundary, and that is the part worth fixing permanently.
 *
 * Scoped around the page area only, so the nav always survives and you can click
 * your way out. `resetKey` clears the error when the page changes, so navigating
 * away from a broken page is enough to recover.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { err: null, info: null }
  }

  static getDerivedStateFromError(err) {
    return { err }
  }

  componentDidCatch(err, info) {
    this.setState({ info })
    // Log it where it can actually be read later.
    try {
      console.error('[tradecrypto] page crashed:', err, info?.componentStack)
      fetch('/api/v1/ui-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page: this.props.page || null,
          message: String(err && err.message ? err.message : err),
          stack: String(info?.componentStack || '').slice(0, 4000),
        }),
      }).catch(() => {})
    } catch { /* never let the reporter become the crash */ }
  }

  componentDidUpdate(prev) {
    if (this.state.err && prev.resetKey !== this.props.resetKey) {
      this.setState({ err: null, info: null })
    }
  }

  render() {
    if (!this.state.err) return this.props.children
    const msg = String(this.state.err?.message || this.state.err)
    return (
      <div className="banner bad" style={{ display: 'block' }}>
        <b>This page hit an error.</b>
        <p style={{ margin: '8px 0' }}>
          The rest of the app still works — pick another tab on the left, or come
          back to this one to retry. Nothing was lost and the engine kept running.
        </p>
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, opacity: 0.85, margin: 0 }}>{msg}</pre>
        <button className="why" style={{ marginTop: 10 }}
                onClick={() => this.setState({ err: null, info: null })}>
          try this page again
        </button>
      </div>
    )
  }
}
