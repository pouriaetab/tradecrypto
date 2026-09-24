// Render check -- ported from market/webapp_blueprint CHECKLIST item 2.4, adapted
// from the template's TypeScript/tsc version to this app's plain JSX by using
// esbuild to transpile instead.
//
// WHY THIS EXISTS, CONCRETELY
// On 2026-09-12 clicking a symbol in the Journal turned the whole app blank with
// no way back. The cause: Journal.jsx rendered a CoinChart element and never
// imported it. `pick` starts null, so the bad reference was never evaluated --
// the page loaded fine, the bundle built fine, and nothing caught it until a
// human clicked. Then React 18 unmounted the entire tree, nav included.
//
// A bundler cannot catch this: an undefined identifier is legal JavaScript until
// it runs. A type-checker would, but this app has no types. So: actually RENDER
// every page in Node and fail if any of them throws.
//
//   node scripts/render-check.mjs
//   ESBUILD_BIN=/path/to/esbuild node scripts/render-check.mjs
//
// It runs where vite cannot -- the assistant's Linux sandbox, where the repo's
// rollup/esbuild binaries are the macOS ones (checklist item 5.1).
//
// NOT covered: effects. useEffect/useAsync never fire in a server render, so
// fetches, charts and WebSockets are untested here. That is what a live smoke
// test is for (checklist 2.1/2.3).
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '..')
const src = path.join(root, 'src')
const require_ = createRequire(path.join(root, 'package.json'))

// ---- locate an esbuild that runs on THIS platform --------------------------
//
// This used to look in node_modules/.bin and nowhere else. Under pnpm — which is
// what this project actually uses — .bin holds only DIRECT dependencies, so the
// real binary sits in node_modules/.pnpm/@esbuild+<platform>@<ver>/... and the
// check reported "no esbuild binary found" on the machine that owns the repo,
// while passing anywhere ESBUILD_BIN happened to be exported. A checker that
// only runs in its author's environment is not a checker.
function findEsbuild() {
  if (process.env.ESBUILD_BIN && existsSync(process.env.ESBUILD_BIN)) return process.env.ESBUILD_BIN


  // The native @esbuild/<platform> binary first; the .bin shims all delegate to
  // it, so if it is missing or for another platform they fail too.
  for (const c of [path.join(root, 'node_modules', '.bin', 'esbuild'),
                   path.join(root, 'node_modules', 'esbuild', 'bin', 'esbuild')]) {
    if (existsSync(c) && process.platform !== 'linux') return c
  }
  const store = path.join(root, 'node_modules', '.pnpm')
  if (existsSync(store)) {
    let entries = []
    try { entries = readdirSync(store) } catch { entries = [] }
    // Only a binary built for THIS platform. The store can hold another
    // machine's build — a macOS arm64 esbuild sitting in a repo inspected from
    // Linux runs far enough to fail with "bundling failed", which reads like a
    // broken component rather than the wrong executable.
    const want = `@esbuild+${process.platform}-${process.arch}@`
    const native = entries.filter((e) => e.startsWith(want))
    const plain = entries.filter((e) => e.startsWith('esbuild@'))
    for (const e of native) {
      const scoped = path.join(store, e, 'node_modules', '@esbuild')
      let inner = []
      try { inner = readdirSync(scoped) } catch { continue }
      for (const i of inner) {
        const c = path.join(scoped, i, 'bin', 'esbuild')
        if (existsSync(c)) return c
      }
    }
    // The JS shim only works if the matching native package is installed, so it
    // is a last resort and only when we found a platform match above.
    if (native.length) {
      for (const e of plain) {
        const c = path.join(store, e, 'node_modules', 'esbuild', 'bin', 'esbuild')
        if (existsSync(c)) return c
      }
    }
  }
  return null
}
const esbuild = findEsbuild()
if (!esbuild) {
  console.error('render-check: no esbuild found. Run `npm install` (or `pnpm install`) in frontend/, or set ESBUILD_BIN=/path/to/esbuild')
  process.exit(2)
}

let reactPath, reactServerPath
try {
  reactPath = require_.resolve('react')
  reactServerPath = require_.resolve('react-dom/server')
} catch {
  console.error('render-check: react / react-dom not installed under frontend/')
  process.exit(2)
}

// ---- which pages to render: read them out of App.jsx, never a second list ---
const appSrc = readFileSync(path.join(src, 'App.jsx'), 'utf8')
const imports = new Map()
for (const m of appSrc.matchAll(/import\s+([A-Z][\w$]*)\s+from\s+'(\.\/pages\/[^']+)'/g)) {
  imports.set(m[1], m[2])
}
const pages = []
const pagesBlock = appSrc.match(/const PAGES = \[([\s\S]*?)\n\]/)
if (pagesBlock) {
  for (const m of pagesBlock[1].matchAll(/\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*([A-Z][\w$]*)\s*\]/g)) {
    const [, id, label, comp] = m
    if (imports.has(comp)) pages.push({ id, label, comp, from: imports.get(comp) })
  }
}
if (!pages.length) {
  console.error('render-check: could not parse PAGES out of App.jsx')
  process.exit(2)
}

// ---- bundle an entry that exposes App plus every page ----------------------
const out = mkdtempSync(path.join(os.tmpdir(), 'tc-render-'))
// Clean up after ourselves, including on a crash. 96 of these had accumulated
// at ~2 MB each and taken the temp volume below the floor the gate checks for,
// so running the gate was what made the gate fail.
const cleanup = () => { try { rmSync(out, { recursive: true, force: true }) } catch {} }
process.on('exit', cleanup)
process.on('SIGINT', () => { cleanup(); process.exit(130) })
process.on('SIGTERM', () => { cleanup(); process.exit(143) })
// Sweep any left by earlier runs, before this fix or after a hard kill.
try {
  for (const d of readdirSync(os.tmpdir())) {
    if (d.startsWith('tc-render-') && path.join(os.tmpdir(), d) !== out) {
      rmSync(path.join(os.tmpdir(), d), { recursive: true, force: true })
    }
  }
} catch {}
// React and react-dom/server are bundled IN, not externalised. Externalising
// them put the bundle in a temp dir where 'react' does not resolve, and even with
// a resolvable path the check would hold a different React instance than the one
// the components use, which breaks hooks. One bundle, one React.
const entry = path.join(out, 'entry.jsx')
writeFileSync(entry, [
  // Absolute paths: the entry lives in a temp dir, so esbuild cannot resolve
  // bare 'react' from there. Resolve it from the project and inject the path.
  `import React from ${JSON.stringify(reactPath)}`,
  `import { renderToString } from ${JSON.stringify(reactServerPath)}`,
  `import App from ${JSON.stringify(path.join(src, 'App.jsx'))}`,
  ...pages.map((p, i) => `import P${i} from ${JSON.stringify(path.join(src, p.from.replace('./', '')))}`),
  `const PAGE_LIST = [${pages.map((p, i) => `{ id: ${JSON.stringify(p.id)}, label: ${JSON.stringify(p.label)}, C: P${i} }`).join(', ')}]`,
  `module.exports.run = function run(onError) {`,
  `  const results = []`,
  `  for (const { id, label, C } of PAGE_LIST) {`,
  `    const mark = onError()`,
  `    try {`,
  `      const html = renderToString(React.createElement(C, { onModel: () => {} }))`,
  `      const errs = onError(mark)`,
  `      if (errs.length) { results.push({ id, label, ok: false, why: errs[0] }) }`,
  `      else if (!html || html.length < 20) {`,
  `        results.push({ id, label, ok: false, why: 'rendered nothing (' + html.length + ' chars)' })`,
  `      } else { results.push({ id, label, ok: true }) }`,
  `    } catch (err) {`,
  `      results.push({ id, label, ok: false, why: (err && err.constructor ? err.constructor.name : 'Error') + ': ' + (err && err.message) })`,
  `    }`,
  `  }`,
  `  try {`,
  `    const shell = renderToString(React.createElement(App, {}))`,
  `    // AN EMPTY RENDER IS A FAILURE, not a pass. On 2026-09-17 an edit`,
  `    // attached \`export default\` to a component that returns null unless a`,
  `    // token is missing, so the app's default export became that component.`,
  `    // The real app never mounted -- every page in the browser was blank --`,
  `    // and this check reported 18/18 rendered, because renderToString on a`,
  `    // component that returns null succeeds and gives you ''.`,
  `    //`,
  `    // The shell always emits the sidebar and the nav, so anything under a`,
  `    // few hundred characters is not the shell.`,
  `    if (!shell || shell.length < 200) {`,
  `      results.push({ id: 'App', label: 'app shell', ok: false,`,
  `                     why: 'rendered ' + shell.length + ' chars -- the default export is not the app' })`,
  `    } else {`,
  `      results.push({ id: 'App', label: 'app shell', ok: true })`,
  `    }`,
  `  } catch (err) {`,
  `    results.push({ id: 'App', label: 'app shell', ok: false, why: (err && err.constructor ? err.constructor.name : 'Error') + ': ' + (err && err.message) })`,
  `  }`,
  `  return results`,
  `}`,
].join('\n'))

// CJS, not ESM: react-dom/server is CommonJS and requires node builtins like
// 'stream'. Bundled into ESM, esbuild turns those into a "Dynamic require is not
// supported" stub and the check dies before rendering anything.
const bundle = path.join(out, 'bundle.cjs')
try {
  execFileSync(esbuild, [
    entry, '--bundle', '--format=cjs', '--platform=node',
    '--loader:.js=jsx', '--loader:.jsx=jsx', '--jsx=transform',
    `--outfile=${bundle}`, '--log-level=warning',
  ], { stdio: 'inherit', cwd: root })
} catch {
  console.error('render-check: bundling failed (syntax error or unresolved import)')
  process.exit(1)
}

// ---- browser-ish globals a first render may touch --------------------------
const store = new Map()
const storage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
  clear: () => store.clear(),
  key: (i) => [...store.keys()][i] ?? null,
  get length() { return store.size },
}
globalThis.localStorage = storage
globalThis.sessionStorage = storage
globalThis.window = globalThis
globalThis.self = globalThis
globalThis.innerWidth = 1440
globalThis.innerHeight = 900
globalThis.devicePixelRatio = 1
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0)
globalThis.cancelAnimationFrame = (id) => clearTimeout(id)
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
Object.defineProperty(globalThis, 'navigator', {
  value: { userAgent: 'node-render-check', language: 'en-US', clipboard: {} },
  configurable: true, writable: true,
})
// No network in a render check; anything that slips through resolves empty.
globalThis.fetch = () => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve({ ok: true, data: null }),
  text: () => Promise.resolve(''),
})
globalThis.AbortController = globalThis.AbortController || class { constructor() { this.signal = {} } abort() {} }
globalThis.document = {
  documentElement: { style: {}, setAttribute() {}, classList: { add() {}, remove() {} } },
  body: { style: {}, appendChild() {}, removeChild() {} },
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {}, remove() {} }),
  addEventListener() {}, removeEventListener() {}, querySelector: () => null,
  getElementById: () => null,
}

// Console errors are failures too: React logs "Each child needs a key" and
// similar here, and a swallowed exception often shows up only as a console.error.
const consoleErrors = []
const realError = console.error
console.error = (...a) => { consoleErrors.push(a.map(String).join(' ')) }
const NOISE = /useLayoutEffect|not supported in the server|validateDOMNesting/i
// onError() with no argument returns a mark; onError(mark) returns what was
// logged since that mark. Passed into the bundle so it sees the same array.
const onError = (mark) => (mark === undefined
  ? consoleErrors.length
  : consoleErrors.slice(mark).filter((e) => !NOISE.test(e)).map((e) => e.slice(0, 200)))

const nodeRequire = createRequire(import.meta.url)
const mod = nodeRequire(bundle)
const results = mod.run(onError)
console.error = realError

const failed = results.filter((r) => !r.ok).length
const pad = Math.max(...results.map((r) => r.label.length))
for (const r of results) {
  console.log(`  ${r.ok ? 'ok  ' : 'FAIL'}  ${r.label.padEnd(pad)}${r.ok ? '' : '  ' + r.why}`)
}
console.log(`\nrender-check: ${results.length - failed}/${results.length} rendered`)
process.exit(failed ? 1 : 0)
