// Render every page with REAL API payloads, and fail if any of them throws.
//
// WHY THIS EXISTS, CONCRETELY
// render-check.mjs says at the top: "NOT covered: effects. useEffect/useAsync
// never fire in a server render." That is a bigger hole than it sounds. Every
// card in this app is written as `!data ? <loading/> : <the real thing/>`, so a
// render with no data exercises the loading branch of all eighteen pages and
// nothing else. On 2026-09-21 the Strategies tab died in the browser with
// "Cannot read properties of undefined (reading 'toFixed')" while render-check
// reported 18/18 rendered, because the crash lived in a branch it never entered.
//
// This check calls the real API, hands each page its real payloads with
// useAsync resolved synchronously, and renders. That is the branch the operator
// actually sees.
//
//   node scripts/render-with-data.mjs                 # fetch live, then render
//   node scripts/render-with-data.mjs payloads.json   # render a saved capture
//
// The payload file is {"<api method name>": <the `data` the endpoint returned>}.
// A method with no entry returns null, which is the honest stand-in for an
// endpoint that has not run yet -- pages must survive that too.
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync, cpSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const src = path.join(root, 'src')

function findEsbuild() {
  if (process.env.ESBUILD_BIN && existsSync(process.env.ESBUILD_BIN)) return process.env.ESBUILD_BIN
  const pnpm = path.join(root, 'node_modules', '.pnpm')
  const roots = [path.join(root, 'node_modules', '@esbuild')]
  if (existsSync(pnpm)) {
    for (const d of readdirSync(pnpm)) {
      if (d.startsWith('@esbuild+')) roots.push(path.join(pnpm, d, 'node_modules', '@esbuild'))
    }
  }
  for (const r of roots) {
    if (!existsSync(r)) continue
    for (const plat of readdirSync(r)) {
      const bin = path.join(r, plat, 'bin', 'esbuild')
      if (existsSync(bin)) { try { execFileSync(bin, ['--version'], { stdio: 'ignore' }); return bin } catch {} }
    }
  }
  const shim = path.join(root, 'node_modules', '.bin', 'esbuild')
  if (existsSync(shim)) return shim
  return null
}
const esbuild = findEsbuild()
if (!esbuild) { console.error('render-with-data: no esbuild binary for this platform'); process.exit(2) }

const payloadFile = process.argv[2]
if (!payloadFile || !existsSync(payloadFile)) {
  console.error('render-with-data: pass a payload JSON file (see the header)')
  process.exit(2)
}
const payloads = JSON.parse(readFileSync(payloadFile, 'utf8'))

// ---- a copy of src with useAsync made synchronous and api made a lookup ----
const out = mkdtempSync(path.join(os.tmpdir(), 'tc-render-data-'))
const cleanup = () => { try { rmSync(out, { recursive: true, force: true }) } catch {} }
process.on('exit', cleanup)
process.on('SIGINT', () => { cleanup(); process.exit(130) })
process.on('SIGTERM', () => { cleanup(); process.exit(143) })
try {
  for (const d of readdirSync(os.tmpdir())) {
    if (d.startsWith('tc-render-data-') && path.join(os.tmpdir(), d) !== out) {
      rmSync(path.join(os.tmpdir(), d), { recursive: true, force: true })
    }
  }
} catch {}

const work = path.join(out, 'src')
cpSync(src, work, { recursive: true })

// api.js becomes a Proxy: any method name returns that method's captured
// payload. A name with no capture returns null -- which is exactly what a page
// sees the first time a daily job has not run yet, and must survive.
const apiPath = path.join(work, 'lib', 'api.js')
const apiOrig = readFileSync(apiPath, 'utf8')
const keepFmt = apiOrig.includes('export const fmt')
writeFileSync(apiPath, [
  `const PAYLOADS = ${JSON.stringify(payloads)}`,
  `export const api = new Proxy({}, { get: (_t, k) => () => (k in PAYLOADS ? PAYLOADS[k] : null) })`,
  `export const setToken = () => {}`,
  `export const verifyToken = async () => true`,
  `export const offlineReason = () => null`,
  `export const base = ''`,
  // fmt is pure formatting with no network; keep the REAL one, because a stub
  // would hide exactly the kind of bug this check exists to find.
  keepFmt ? apiOrig.slice(apiOrig.indexOf('export const fmt')).split('\nexport ')[0] : 'export const fmt = {}',
].join('\n'))

// useAsync resolves synchronously against that api.
const uiPath = path.join(work, 'components', 'ui.jsx')
let ui = readFileSync(uiPath, 'utf8')
const uiStart = ui.indexOf('export function useAsync')
if (uiStart < 0) { console.error('render-with-data: could not find useAsync in ui.jsx'); process.exit(2) }
// Replace the whole function by finding its matching close brace.
let depth = 0, i = ui.indexOf('{', uiStart), end = -1
for (; i < ui.length; i++) {
  if (ui[i] === '{') depth++
  else if (ui[i] === '}') { depth--; if (depth === 0) { end = i + 1; break } }
}
ui = ui.slice(0, uiStart) + `export function useAsync(fn) {
  try { return { data: fn(), err: null, loading: false, reload: () => {} } }
  catch (e) { return { data: null, err: String(e && e.message || e), loading: false, reload: () => {} } }
}` + ui.slice(end)
writeFileSync(uiPath, ui)

// ---- pages, read out of App.jsx, never a second list -----------------------
const appSrc = readFileSync(path.join(work, 'App.jsx'), 'utf8')
const imports = new Map()
for (const m of appSrc.matchAll(/import\s+([A-Z][\w$]*)\s+from\s+'(\.\/pages\/[^']+)'/g)) imports.set(m[1], m[2])
const pages = []
const block = appSrc.match(/const PAGES = \[([\s\S]*?)\n\]/)
if (block) {
  for (const m of block[1].matchAll(/\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*([A-Z][\w$]*)\s*\]/g)) {
    if (imports.has(m[3])) pages.push({ id: m[1], label: m[2], comp: m[3], from: imports.get(m[3]) })
  }
}
if (!pages.length) { console.error('render-with-data: could not parse PAGES out of App.jsx'); process.exit(2) }

const req = path.join(root, 'node_modules')
const reactPath = path.join(req, 'react')
const reactServerPath = path.join(req, 'react-dom', 'server')
const entry = path.join(out, 'entry.jsx')
writeFileSync(entry, [
  `import React from ${JSON.stringify(reactPath)}`,
  `import { renderToString } from ${JSON.stringify(reactServerPath)}`,
  ...pages.map((p, i) => `import P${i} from ${JSON.stringify(path.join(work, p.from.replace('./', '')))}`),
  `const LIST = [${pages.map((p, i) => `{ id: ${JSON.stringify(p.id)}, label: ${JSON.stringify(p.label)}, C: P${i} }`).join(', ')}]`,
  `module.exports.run = function run() {`,
  `  const results = []`,
  `  for (const { id, label, C } of LIST) {`,
  `    try {`,
  `      const html = renderToString(React.createElement(C, { onModel: () => {} }))`,
  `      results.push({ id, label, ok: true, chars: html.length })`,
  `    } catch (err) {`,
  `      results.push({ id, label, ok: false,`,
  `        why: (err && err.constructor ? err.constructor.name : 'Error') + ': ' + (err && err.message),`,
  `        stack: String((err && err.stack) || '').split('\\n').slice(1, 5).join('\\n') })`,
  `    }`,
  `  }`,
  `  return results`,
  `}`,
].join('\n'))

const bundle = path.join(out, 'bundle.cjs')
try {
  execFileSync(esbuild, [entry, '--bundle', '--format=cjs', '--platform=node',
    '--loader:.js=jsx', '--loader:.jsx=jsx', '--jsx=transform',
    `--outfile=${bundle}`, '--log-level=warning'],
    // The working copy lives in /tmp, so a bare `react` import cannot resolve by
    // walking up from it. NODE_PATH points esbuild at the project's modules --
    // copying the tree inside the repo instead would need a delete under a
    // mounted folder, which fails from this side (checklist 5.16).
    { stdio: 'inherit', cwd: root, env: { ...process.env, NODE_PATH: req } })
} catch { console.error('render-with-data: bundling failed'); process.exit(1) }

const store = new Map()
const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k), clear: () => store.clear(), key: (i) => [...store.keys()][i] ?? null,
  get length() { return store.size } }
globalThis.localStorage = storage; globalThis.sessionStorage = storage
globalThis.window = globalThis; globalThis.self = globalThis
globalThis.innerWidth = 1440; globalThis.innerHeight = 900; globalThis.devicePixelRatio = 1
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0)
globalThis.cancelAnimationFrame = (id) => clearTimeout(id)
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node', language: 'en-US', clipboard: {} },
  configurable: true, writable: true })
globalThis.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true, data: null }), text: () => Promise.resolve('') })
globalThis.AbortController = globalThis.AbortController || class { constructor() { this.signal = {} } abort() {} }
const styleSheet = { insertRule() {}, cssRules: [] }
globalThis.document = {
  documentElement: { style: {}, setAttribute() {}, classList: { add() {}, remove() {} } },
  head: { appendChild() {}, querySelector: () => null },
  body: { style: {}, appendChild() {}, removeChild() {} },
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, remove() {}, sheet: styleSheet }),
  addEventListener() {}, removeEventListener() {}, querySelector: () => null, getElementById: () => null,
  createTextNode: () => ({}),
}

const { run } = await import('file://' + bundle)
const results = run()
let bad = 0
for (const r of results) {
  if (r.ok) console.log(`   ok    ${r.id.padEnd(16)} ${String(r.chars).padStart(7)} chars`)
  else { bad++; console.log(`   FAIL  ${r.id.padEnd(16)} ${r.why}`); if (r.stack) console.log(r.stack.replace(/^/gm, '           ')) }
}
console.log(bad ? `\nrender-with-data: ${bad} page(s) threw on real data`
                : `\nrender-with-data: ${results.length}/${results.length} rendered with real payloads`)
process.exit(bad ? 1 : 0)
