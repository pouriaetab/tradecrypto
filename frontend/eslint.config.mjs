/* One rule, and it is the one that would have saved two evenings.
 *
 * 2026-09-22: `summarise(rows, key)` was changed to `summarise(rows, col)` so a
 * computed column could declare its own value accessor. The signature changed;
 * one line in the body still said `key`, and it became a reference to nothing.
 * The Risk tab died with "key is not defined" the moment the operator asked for
 * a total.
 *
 * Neither render check saw it. render-check renders with no data, so it only
 * enters the loading branch. render-with-data renders with real payloads, but
 * the stats are computed on a CLICK and both checks render the page AT REST.
 * That is three bugs now in the same family -- a branch no check enters.
 *
 * `no-undef` does not care about branches. It is static: it reads the scopes and
 * fails on any identifier that is not a parameter, a local, an import or a known
 * global. It would have caught this one at write time, and the Journal's
 * `r.gap` class of mistake stays caught by check_jsx_imports.
 *
 * Deliberately narrow. This is not a style gate -- the project has no linter and
 * adding one that argues about semicolons would get switched off within a week
 * (checklist 5.1: a permanently noisy check gets ignored). Two rules, both about
 * code that cannot possibly work.
 */
export default [
  {
    files: ['src/**/*.js', 'src/**/*.jsx'],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: {
        window: 'readonly', document: 'readonly', navigator: 'readonly',
        localStorage: 'readonly', sessionStorage: 'readonly',
        fetch: 'readonly', AbortController: 'readonly', URL: 'readonly',
        setTimeout: 'readonly', clearTimeout: 'readonly',
        setInterval: 'readonly', clearInterval: 'readonly',
        requestAnimationFrame: 'readonly', cancelAnimationFrame: 'readonly',
        ResizeObserver: 'readonly', IntersectionObserver: 'readonly',
        MutationObserver: 'readonly', WebSocket: 'readonly',
        console: 'readonly', performance: 'readonly', matchMedia: 'readonly',
        Image: 'readonly', Blob: 'readonly', FileReader: 'readonly',
        getComputedStyle: 'readonly', devicePixelRatio: 'readonly',
        requestIdleCallback: 'readonly', structuredClone: 'readonly',
        process: 'readonly', globalThis: 'readonly',
        URLSearchParams: 'readonly', URLSearchParams_: 'readonly',
        TextEncoder: 'readonly', TextDecoder: 'readonly', crypto: 'readonly',
        Intl: 'readonly', DOMParser: 'readonly', Node: 'readonly',
        HTMLElement: 'readonly', Event: 'readonly', CustomEvent: 'readonly',
      },
    },
    // The repo carries eslint-disable comments written for a different
    // linter; flagging them as unused is noise, not signal.
    linterOptions: { reportUnusedDisableDirectives: false },
    rules: {
      // The one that matters: an identifier that refers to nothing.
      'no-undef': 'error',
      // `no-unused-vars` was tried and REMOVED the same minute: without the
      // React plugin's jsx-uses-vars, every component referenced only in JSX
      // reads as unused, which was 182 false errors across the app. A check
      // that is 97% noise gets switched off within a week (checklist 5.1), and
      // then the rule that mattered goes with it. One rule, no noise.
    },
  },
]
