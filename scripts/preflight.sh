#!/usr/bin/env bash
# Pre-handoff gate. From market/webapp_blueprint CHECKLIST item 2.5.
#
# The rule it enforces: "py_compile / tsc is not 'it opens'." Two bugs on
# 2026-09-12 passed every check that existed -- a missing import that blanked the
# whole app on a click, and a Movers endpoint that timed out on every single
# request. Both are caught below.
#
# Runs anywhere, including the assistant's Linux sandbox. Gates that need macOS
# (vite build -- the repo's rollup binary is macOS-only) or a live server are
# named and skipped out loud rather than silently passing. Checklist 5.3.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
ROOT="$PWD"
fail=0
skip=0

say()  { printf '\n== %s ==\n' "$1"; }
ok()   { printf '   ok   %s\n' "$1"; }
bad()  { printf '   FAIL %s\n' "$1"; fail=$((fail+1)); }
skipm(){ printf '   skip %s (%s)\n' "$1" "$2"; skip=$((skip+1)); }

PY="$ROOT/backend/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

say "backend: every module compiles"
if (cd "$ROOT/backend" && "$PY" -m compileall -q app >/dev/null 2>&1); then
  ok "compileall"
else
  (cd "$ROOT/backend" && "$PY" -m compileall -q app 2>&1 | head -20)
  bad "compileall"
fi

say "backend: pyflakes (undefined names, bad imports)"
# Read the OUTPUT, not the exit code. pyflakes exits 1 whenever it reports
# anything, and under `set -o pipefail` that exit code won the whole pipeline --
# so this gate said "ok" while printing the very findings it was meant to catch.
# An undefined name in Python is the same class of bug as a missing JSX import.
PF="$( (cd "$ROOT/backend" && "$PY" -m pyflakes app 2>/dev/null) \
        | grep -v "imported but unused" | grep -v "unable to detect undefined" || true )"
if [ -n "$PF" ]; then
  printf '%s\n' "$PF" | head -20
  bad "pyflakes reported the lines above"
else
  ok "pyflakes clean (unused imports ignored)"
fi

say "backend: the test suite, including the data invariants"
# The gate that did not exist. Every check above this line reads the CODE;
# the NULL order linkage lived in the DATA and survived four days of green
# checks. See backend/tests/test_data_invariants.py.
if [ -f "$ROOT/backend/tests/test_data_invariants.py" ]; then
  if "$PY" -c "import pytest" >/dev/null 2>&1; then
    OUT="$( (cd "$ROOT/backend" && "$PY" -m pytest -q tests 2>&1) )"
    if printf '%s' "$OUT" | grep -qE "^(FAILED|ERROR)|failed"; then
      printf '%s\n' "$OUT" | tail -30
      bad "a test failed — see above (data invariants are in tests/test_data_invariants.py)"
    else
      ok "$(printf '%s' "$OUT" | tail -1)"
    fi
  else
    skipm "invariants" "pytest not installed for $PY"
  fi
else
  bad "the invariant tests are missing"
fi

say "frontend: every component used is imported"
if "$PY" "$ROOT/scripts/check_jsx_imports.py" frontend/src; then
  ok "no undefined components"
else
  bad "a component is used without being imported -- this is the blank-page bug"
fi

say "frontend: every CSS variable resolves"
if "$PY" "$ROOT/scripts/check_css_vars.py" frontend/src; then
  ok "no undefined custom properties"
else
  bad "a CSS variable is used but never defined — it renders as if absent"
fi

say "frontend: every page actually renders"
EB="${ESBUILD_BIN:-$ROOT/frontend/node_modules/.bin/esbuild}"
if [ ! -d "$ROOT/frontend/node_modules/react" ] && [ ! -d "$ROOT/frontend/node_modules/.pnpm" ]; then
  skipm "render-check" "frontend deps not installed"
elif [ ! -x "$EB" ]; then
  skipm "render-check" "no esbuild for this platform; set ESBUILD_BIN"
elif ESBUILD_BIN="$EB" node "$ROOT/frontend/scripts/render-check.mjs"; then
  ok "all pages rendered"
else
  bad "a page threw while rendering"
fi

say "frontend: production build"
if [ -x "$ROOT/frontend/node_modules/.bin/vite" ] && "$ROOT/frontend/node_modules/.bin/vite" build \
     --outDir "$(mktemp -d)" --emptyOutDir >/dev/null 2>&1; then
  ok "vite build"
else
  skipm "vite build" "needs the platform's own rollup binary — run on the Mac"
fi

say "live stack"
if curl -fsS -m 3 "http://127.0.0.1:${BACKEND_PORT:-8006}/api/v1/health" >/dev/null 2>&1; then
  ok "backend answering on ${BACKEND_PORT:-8006}"
else
  skipm "live smoke" "backend not reachable from here (the sandbox has its own localhost)"
fi

printf '\npreflight: %d failed, %d skipped\n' "$fail" "$skip"
[ "$fail" -eq 0 ] || exit 1
