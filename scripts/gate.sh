#!/usr/bin/env bash
# gate.sh — run before changing anything, and again before handing it back.
#
# The rules in CLAUDE.md § "Rules of investigation" exist because each one was
# broken once and cost real time. A rule that lives only in a document gets
# re-learned the expensive way, so the ones that can be machine-checked are
# checked here, and the ones that cannot are printed so they have to be looked at.
#
# HOW TO ADD A RULE
#   Every repeat of a mistake earns an entry. Add a `rule_<id>()` function that
#   prints ok/bad and returns non-zero on failure, then list it in RULES below.
#   If it genuinely cannot be automated, add one line to MANUAL instead. Keep the
#   incident in the comment — a rule without its story gets deleted by someone who
#   thinks it is pedantry.
#
# Usage:  scripts/gate.sh [--quick]     --quick skips the test suite
# Exit:   0 all clear · 1 something failed · 2 could not run

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
# TC_GATE_ROOT lets the gate be exercised against a throwaway tree. Checking a
# check by planting an offender in the real repo is how you ship the offender
# (2026-09-18: a re-break restore failed and left a broken file behind).
ROOT="${TC_GATE_ROOT:-$PWD}"
QUICK=0; [ "${1:-}" = "--quick" ] && QUICK=1
fails=0

hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok()   { printf '   \033[32mok  \033[0m %s\n' "$1"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails+1)); }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$1"; }
note() { printf '        \033[2m%s\033[0m\n' "$1"; }

# ── R1 · a log line proves nothing until you know which boot it belongs to ────
# 2026-09-18: `phone access is ON` was quoted from a boot that had already ended,
# out of a 22 MB log spanning sixteen restarts, and the advice that followed was
# backwards. Any tool that reads the log must scope itself to the current boot.
rule_log_scoping() {
  # Precision matters more than reach here. The first version flagged a script
  # that only PRINTS the log path in a hint, and another that merely defines it
  # to write to — a gate that cries wolf gets switched off, which is worse than
  # not having one. So only a line that actually extracts facts from the log
  # counts, and only outside an echo/printf or a comment.
  local out
  out=$(python3 - "$ROOT" <<'PY'
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
READ = re.compile(r'\b(grep|awk|sed|cat|head|tail)\b')
REF  = re.compile(r'\$\{?LOG\b|tradecrypto\.log')
SKIP = re.compile(r'^\s*#|\becho\b|\bprintf\b')
MARK = "starting backend on"
bad = []
for f in sorted(list(root.glob("scripts/*.sh")) + list(root.glob("scripts/*.py"))):
    try: text = f.read_text()
    except OSError: continue
    reads = [ln.strip() for ln in text.splitlines()
             if REF.search(ln) and READ.search(ln) and not SKIP.search(ln)]
    if reads and MARK not in text:
        bad.append((f.name, reads[0][:90]))
for name, ln in bad:
    print(f"{name}\t{ln}")
PY
)
  if [ -z "$out" ]; then
    ok "every script that reads the log scopes itself to the current boot"
  else
    printf '%s\n' "$out" | while IFS=$'\t' read -r name ln; do
      bad "$name extracts facts from the log without scoping to the current boot"
      note "$ln"
      note "scope it: last '''starting backend on''' line, then read forward"
    done
    fails=$((fails+1))
  fi
  return 0
}

# ── R5 · only the app opens the live database read-write ─────────────────────
# 2026-09-18: writes from the Linux VM while the macOS app held the same file
# destroyed it. SQLite's locking does not cross the bridge mount.
rule_db_readonly() {
  # Grep cannot tell a sqlite3.connect() CALL from the same text quoted inside
  # an error message, and it cannot tell WHICH FILE is being opened. The grep
  # version of this rule flagged two explanatory strings in dbguard.py and two
  # opens of tradecrypto.recovered.sqlite -- a file nothing else holds -- while
  # a real offender hiding behind a variable would have slipped past. A gate
  # that cries wolf gets switched off, so this walks the AST instead.
  local out rc pybin
  # $PY is local to the pytest rule; resolve our own (set -u would abort here).
  pybin="$ROOT/backend/.venv/bin/python3"
  [ -x "$pybin" ] || pybin="$(command -v python3)"
  out=$("$pybin" "$ROOT/scripts/check_db_opens.py" "$ROOT/backend/app" "$ROOT/scripts" 2>&1); rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "a database open outside the app's own layer is not inert"
    printf '%s\n' "$out" | head -6 | while read -r l; do note "$l"; done
  else
    ok "$(printf '%s\n' "$out" | tail -1)"
  fi

  if grep -l "TC_DB_READONLY" "$ROOT/backend/tests/conftest.py" >/dev/null 2>&1; then
    ok "the test suite defaults to a read-only live database"
  else
    bad "backend/tests/conftest.py no longer forces TC_DB_READONLY"
    note "without it, running the tests writes production — that is how it died"
  fi
  return 0
}

# ── R5b · the vault has everything, and nothing has been removed from it ─────
# The vault (core/vault.py) is the copy that is not in this folder, not a
# database, and not read by anything here. It is only worth having if it is
# checked, and "we write to it every time" is a promise, not a check.
rule_vault() {
  local vdir pybin
  pybin="$ROOT/backend/.venv/bin/python3"
  [ -n "${TC_PY:-}" ] && [ -x "${TC_PY}" ] && pybin="$TC_PY"
  [ -x "$pybin" ] || pybin="$(command -v python3)"
  vdir="${TC_VAULT_DIR:-$HOME/tradecrypto-vault}"

  if [ -d "$vdir/records" ]; then
    local out rc
    out=$("$pybin" "$ROOT/scripts/vault_verify.py" --dir "$vdir" 2>&1); rc=$?
    if [ "$rc" -eq 0 ]; then
      ok "vault — $(printf '%s\n' "$out" | grep -E 'record\(s\),' | tail -1 | sed 's/^ *//')"
    else
      bad "the vault does not verify"
      printf '%s\n' "$out" | grep -E 'FAIL|removed|edited' | head -4 \
        | while read -r l; do note "$l"; done
    fi
  else
    # Not reachable from here (the assistant's VM only sees the project folder).
    # Fall back to the status file the app writes on every reconcile -- and SAY
    # HOW OLD IT IS, because a stale snapshot reported as a fact is how a broken
    # backup went unnoticed for a day.
    local st
    st="$ROOT/data/vault-status.json"
    if [ -f "$st" ]; then
      local line verdict
      line=$("$pybin" - "$st" <<'PYEOF'
import json, sys, time
d = json.load(open(sys.argv[1]))
age = (time.time() - (d.get("checked_at") or 0)) / 60
r = d.get("reconcile") or {}
line = (f"vault (reported {age:.0f} min ago): {d.get('records', 0)} record(s) at "
        f"{d.get('dir')}, {r.get('added', 0)} appended on the last pass")
why = ""
if r.get("failed"):
    why += f"; {r['failed']} record(s) could not be written"
if r.get("error"):
    why += f"; {r['error']}"
if age > 60:
    why += "; the app has not reported on the vault in over an hour"
print(("BAD " if why else "OK ") + line + why)
PYEOF
)
      verdict="${line%% *}"; line="${line#* }"
      # The vault itself is not reachable from here, so this is a SNAPSHOT and
      # the age is part of the claim (blueprint 5.1). Deep verification runs
      # wherever the vault actually is -- on the Mac, where the gate also runs.
      if [ "$verdict" = "OK" ]; then ok "$line"; else bad "$line"; fi
    else
      warn "no vault yet — the app creates it on its next order, trade or reconcile"
      note "verify it yourself any time: python3 scripts/vault_verify.py"
    fi
  fi
  return 0
}

# ── R5c · built is not the same word as running ──────────────────────────────
# "you confirmed the trailing stop had moved several times" -- the code was real
# and had never once been entered. Tests passed (they call the function), the
# gate passed (the code is there), the UI showed the setting as on. Nothing
# asked whether it had ever FIRED. This does.
rule_liveness() {
  local pybin out
  pybin="$ROOT/backend/.venv/bin/python3"
  [ -n "${TC_PY:-}" ] && [ -x "${TC_PY}" ] && pybin="$TC_PY"
  [ -x "$pybin" ] || pybin="$(command -v python3)"
  # Off the machine that holds the database, this reads the newest ledger
  # snapshot, never the live file. Rule 5 (CLAUDE.md), fourth incident,
  # 2026-09-21 09:08: facts.sh opened the live file mode=ro from the VM and
  # the Mac backend died with "Bus error: 10" twelve seconds later. The third
  # incident (2026-09-20 16:14) was an immutable=1 read. Neither mode is a
  # promise the bridge mount keeps, so from here the live file is not opened
  # at all. mechanism_fires, orders and trades are all in the snapshot.
  local dbpath=""
  if [ "$(uname -s)" != "Darwin" ] || [ "${ROOT#"$HOME"/mnt/}" != "$ROOT" ]; then
    dbpath=$(ls -1t "$ROOT"/data/ledger/ledger-*.sqlite 2>/dev/null | head -1)
    if [ -z "$dbpath" ]; then
      warn "not on the machine that holds the database and no ledger snapshot to read"
      return 0
    fi
  fi
  out=$(cd "$ROOT/backend" && env TC_DB_READONLY=1 ${dbpath:+TC_DB_PATH="$dbpath"} "$pybin" - <<'PYEOF' 2>&1
import sys
sys.path.insert(0, ".")
try:
    from app.core import liveness
    r = liveness.report()
except Exception as exc:
    print(f"SKIP could not read the mechanism registry: {type(exc).__name__}: {exc}")
    raise SystemExit
if r.get("error"):
    print("SKIP " + r["error"]); raise SystemExit
never = [m for m in r["mechanisms"] if m["alarming"]]
quiet = r.get("went_quiet") or []
if never:
    print("BAD " + f"{len(never)} mechanism(s) exist but have NEVER run")
    for m in never[:5]:
        print("  " + f"{m['name']}: {m['what']}")
else:
    n = len(r["mechanisms"])
    fired = sum(1 for m in r["mechanisms"] if m["times"])
    dark = r.get("known_not_live") or []
    bits = []
    if dark:
        bits.append("known not live: " + ", ".join(dark))
    if quiet:
        bits.append("quiet: " + ", ".join(quiet))
    tail = ("; " + "; ".join(bits)) if bits else ""
    print("OK " + f"{fired}/{n} mechanism(s) have fired at least once{tail}")
PYEOF
)
  case "$out" in
    OK*)   ok "${out#OK }" ;;
    SKIP*) warn "${out#SKIP }" ;;
    BAD*)  bad "$(printf '%s' "$out" | head -1 | sed 's/^BAD //')"
           printf '%s\n' "$out" | tail -n +2 | while read -r l; do note "$l"; done ;;
    *)     warn "mechanism registry said: $(printf '%s' "$out" | head -2 | tr '\n' ' ')" ;;
  esac
  return 0
}

# ── R6 · redirect the bytecode cache on Linux ────────────────────────────────
# 2026-09-18: a restored access.py and its stale .pyc carried mtimes in the same
# second, so Python kept the cache and the tests went on executing the broken
# build. .pyc files in the mounted folder cannot be deleted from the VM.
rule_pycache() {
  if [ "$(uname -s)" != "Linux" ]; then
    ok "not Linux — bytecode cache rule does not apply"
    return 0
  fi
  if [ -n "${PYTHONPYCACHEPREFIX:-}" ]; then
    ok "PYTHONPYCACHEPREFIX is set ($PYTHONPYCACHEPREFIX)"
  else
    bad "PYTHONPYCACHEPREFIX is unset — a stale .pyc can hide a fix, or a break"
    note 'export PYTHONPYCACHEPREFIX=$HOME/scratch/pycache'
  fi
  return 0
}

# ── R8 · a trade id is never handed out twice ────────────────────────────────
# 2026-09-18: restoring the database rolled `sqlite_sequence` back to 31, so the
# next four trades took ids 32-35 — ids four real trades from the 17th already
# had. Nothing errored. Trade #33 was a UNI trade one day and an ARB trade the
# next. A missing trade is a hole you can see; a reused id is a hole that looks
# like data. Checked against the newest ledger snapshot, never the live file.
rule_trade_ids() {
  local snap
  snap=$(ls -1t "$ROOT"/data/ledger/ledger-*.sqlite 2>/dev/null | head -1)
  if [ -z "$snap" ]; then warn "no ledger snapshot to check trade ids against"; return 0; fi
  # The snapshot can be up to ten minutes behind the live database, so a failure
  # here might already be fixed. Say how old the evidence is, every time — a check
  # that cannot tell "broken" from "stale" teaches you to ignore it.
  local age_min
  age_min=$(( ( $(date +%s) - $(date -r "$snap" +%s) ) / 60 ))
  local out
  out=$(python3 - "$snap" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
try:
    mx = c.execute("SELECT COALESCE(MAX(id),0) FROM trades").fetchone()[0]
    hw = c.execute("SELECT value FROM app_state WHERE key='trades_max_id_ever'").fetchone()
except sqlite3.Error as e:
    print(f"SKIP {e}"); raise SystemExit
if hw is None:
    print("FAIL the trade-id high-water mark has never been recorded")
elif int(float(hw[0])) < mx:
    print(f"FAIL high-water mark {hw[0]} is below the highest id {mx}")
else:
    print(f"OK high-water {hw[0]}, highest id {mx}")
dups = c.execute("SELECT symbol, strategy, COUNT(*) n FROM trades "
                 "GROUP BY symbol, strategy, CAST(ts_open AS INT) HAVING n > 1").fetchall()
if dups:
    print(f"FAIL {len(dups)} trade(s) recorded twice: "
          + ", ".join(f"{d[0]}/{d[1]}" for d in dups[:4]))
PY
)
  printf '%s\n' "$out" | while read -r verdict rest; do
    case "$verdict" in
      OK)   ok "trade ids never reused ($rest)" ;;
      FAIL) bad "$rest (from a ledger snapshot ${age_min} min old)" ;;
      SKIP) warn "trade-id check skipped ($rest)" ;;
    esac
  done
  if printf '%s' "$out" | grep -c "^FAIL" >/dev/null && [ "$(printf '%s' "$out" | grep -c '^FAIL')" -gt 0 ]; then
    fails=$((fails+1))
  fi
  return 0
}

# ── R10 · the second copy exists and has not been altered ───────────────────
# 2026-09-18: the database died and there was nowhere else to look — trades were
# written to database tables and nothing else. Every trade and order now also
# goes to an append-only, hash-chained journal. If the chain breaks, something
# edited or truncated the ledger's only independent copy.
rule_journal() {
  local out
  out=$(python3 - "$ROOT" <<'PY'
import sys, hashlib, json
from pathlib import Path
d = Path(sys.argv[1]) / "data" / "journal"
files = sorted(d.glob("*.jsonl")) if d.is_dir() else []
if not files:
    print("WARN no journal files yet — nothing has been journalled since the feature landed")
    raise SystemExit
def tail(f):
    last = b""
    for line in f.open("rb"):
        if line.strip():
            last = line.rstrip(b"\n")
    return hashlib.sha256(last).hexdigest() if last else None

bad, total, carried = [], 0, 0
for k, f in enumerate(files):
    prev = "0" * 64
    n = 0
    for i, line in enumerate(f.open("rb"), 1):
        line = line.rstrip(b"\n")
        if not line.strip():
            continue
        n += 1
        try:
            rec = json.loads(line)
        except ValueError:
            bad.append(f"{f.name}:{i} unreadable"); break
        if rec.get("prev") != prev:
            # A file may legitimately chain on from the previous day's tail:
            # before 2026-09-19 the writer carried its cached hash across
            # midnight, so those days are one long chain rather than one per
            # file. That is still a verifiable chain, and this is an append-only
            # log -- rewriting the files so the old bug never appears to have
            # happened is the one thing we must not do.
            if i == 1 and k > 0 and rec.get("prev") == tail(files[k - 1]):
                prev, _c = rec["prev"], carried
                carried += 1
            else:
                bad.append(f"{f.name}:{i} chain broken"); break
        prev = hashlib.sha256(line).hexdigest()
    total += n
for b in bad:
    print("FAIL " + b)
if not bad:
    note = f", {carried} carried across midnight" if carried else ""
    print(f"OK {len(files)} file(s), {total} event(s), chain intact{note}")
PY
)
  printf '%s\n' "$out" | while read -r verdict rest; do
    case "$verdict" in
      OK)   ok "journal $rest" ;;
      FAIL) bad "journal: $rest" ;;
      WARN) warn "journal: $rest" ;;
    esac
  done
  if [ "$(printf '%s' "$out" | grep -c '^FAIL')" -gt 0 ]; then fails=$((fails+1)); fi
  return 0
}

# ── R3 · use the checkers that already exist ─────────────────────────────────
# 2026-09-18: a token screen was written against var(--good)/var(--bad), neither
# of which exists here, found by hand while check_css_vars.py sat unused.
rule_checkers() {
  local out
  out=$(cd "$ROOT" && python3 scripts/check_css_vars.py 2>&1)
  if printf '%s' "$out" | grep -c "OK" >/dev/null 2>&1 && [ "$(printf '%s' "$out" | grep -c 'OK')" -gt 0 ]
    then ok "check_css_vars"; else bad "check_css_vars"; printf '%s\n' "$out" | tail -4; fi

  out=$(cd "$ROOT" && python3 scripts/check_jsx_imports.py 2>&1)
  if [ "$(printf '%s' "$out" | grep -c 'OK')" -gt 0 ]
    then ok "check_jsx_imports"; else bad "check_jsx_imports"; printf '%s\n' "$out" | tail -4; fi

  # SQL must not decide a trading day with 'localtime' -- that is the timezone of
  # whatever machine runs it, and UTC midnight is 19:00 Chicago.
  out=$(cd "$ROOT" && python3 scripts/check_day_boundary.py 2>&1)
  if [ "$(printf '%s' "$out" | grep -c 'OK')" -gt 0 ]
    then ok "check_day_boundary"; else bad "check_day_boundary"; printf '%s\n' "$out" | tail -8; fi

  # THE SAME RULE FOR PYTHON. 2026-09-23: a block added to engine.tick() read
  # `_rank_of`, which does not exist in that scope. It sat inside a try/except
  # whose whole job is to keep a bad plan from blocking a fill -- so the
  # NameError would have been swallowed on every entry, forever, and the feature
  # would simply never have produced a row. Nothing would have failed. pyflakes
  # is used for undefined names ONLY, for the reason given below about style.
  if python3 -c "import pyflakes" 2>/dev/null || [ -d "$HOME/scratch/pyflakes" ]
  then
    out=$(cd "$ROOT/backend" && PYTHONPATH="$HOME/scratch/pyflakes" python3 -m pyflakes app/ 2>&1 | grep "undefined name")
    if [ -z "$out" ]
      then ok "py-undef — every name resolves"
      else bad "py-undef"; printf '%s\n' "$out" | head -8; fi
  else
    warn "py-undef skipped — no pyflakes (pip install --target=$HOME/scratch/pyflakes pyflakes)"
  fi

  # no-undef. An identifier that refers to nothing cannot be caught by rendering,
  # because the line only runs in the branch nobody renders: on 2026-09-22 the
  # Risk tab died with "key is not defined" the moment the operator asked a table
  # for a total, and BOTH render checks were green -- they render at rest and the
  # stats are computed on a click. This is static and does not care about
  # branches. Deliberately one rule; a linter that argues about style gets
  # switched off and takes the useful rule with it.
  #
  # Same binary-lookup shape as esbuild: the project's own first, a scratch
  # install second, never only a path from someone's sandbox.
  if [ -z "${ESLINT_BIN:-}" ]; then
    for cand in "$ROOT/frontend/node_modules/.bin/eslint" \
                "$ROOT/node_modules/.bin/eslint" \
                "$HOME/scratch/eslint/node_modules/.bin/eslint"; do
      [ -x "$cand" ] && { ESLINT_BIN="$cand"; break; }
    done
  fi
  if [ -z "${ESLINT_BIN:-}" ]; then
    warn "no-undef skipped — no eslint binary (npm i eslint@9 in ~/scratch/eslint)"
  else
    out=$(cd "$ROOT/frontend" && "$ESLINT_BIN" --no-config-lookup -c eslint.config.mjs "src/**/*.{js,jsx}" 2>&1)
    if [ -z "$out" ]
      then ok "no-undef — every identifier resolves"
      else bad "no-undef"; printf '%s\n' "$out" | grep -E "no-undef|problems" | head -8; fi
  fi

  # The project's own esbuild first — a scratch path from someone's sandbox is
  # not a dependency of this repo and must never be the only place it looks.
  if [ -z "${ESBUILD_BIN:-}" ]; then
    for cand in "$ROOT/frontend/node_modules/.bin/esbuild" \
                "$ROOT/node_modules/.bin/esbuild" \
                "$HOME/scratch/eb/node_modules/.bin/esbuild" \
                "$HOME/scratch/esb/node_modules/.bin/esbuild"; do
      [ -x "$cand" ] && { export ESBUILD_BIN="$cand"; break; }
    done
  fi
  out=$(cd "$ROOT/frontend" && node scripts/render-check.mjs 2>&1)
  if [ "$(printf '%s' "$out" | grep -cE '[0-9]+/[0-9]+ rendered')" -gt 0 ] \
     && [ "$(printf '%s' "$out" | grep -c 'FAIL')" -eq 0 ]
    then ok "render-check — $(printf '%s' "$out" | grep -oE '[0-9]+/[0-9]+ rendered' | tail -1)"
    else bad "render-check"; printf '%s\n' "$out" | tail -6; fi

  # render-check renders every page with NO data, so it only ever enters the
  # `!data ? loading : real thing` branch that shows a spinner. On 2026-09-21
  # the Strategies tab died in the browser with "Cannot read properties of
  # undefined (reading 'toFixed')" while render-check reported 18/18 rendered:
  # the crash was in the branch it never enters. This one captures every
  # endpoint's real answer and renders the pages against it.
  # Same snapshot rule as the suite: off the machine that holds the database,
  # read the newest ledger snapshot, never the live file (rule 5).
  local PAYLOADS="${TMPDIR:-/tmp}/tc_gate_payloads.json" gdb=""
  if [ "$(uname -s)" != "Darwin" ] || [ "${ROOT#"$HOME"/mnt/}" != "$ROOT" ]; then
    gdb=$(ls -1t "$ROOT"/data/ledger/ledger-*.sqlite 2>/dev/null | head -1)
  fi
  out=$(cd "$ROOT" && env ${gdb:+TC_DB_PATH="$gdb"} TC_DB_READONLY=1 \
        python3 scripts/capture_payloads.py "$PAYLOADS" 2>&1)
  if [ ! -s "$PAYLOADS" ]; then
    bad "render-with-data — could not capture payloads"; printf '%s\n' "$out" | tail -4
  else
    out=$(cd "$ROOT/frontend" && node scripts/render-with-data.mjs "$PAYLOADS" 2>&1)
    if [ "$(printf '%s' "$out" | grep -cE '[0-9]+/[0-9]+ rendered with real payloads')" -gt 0 ]
      then ok "render-with-data — $(printf '%s' "$out" | grep -oE '[0-9]+/[0-9]+ rendered with real payloads' | tail -1)"
      else bad "render-with-data"; printf '%s\n' "$out" | grep -A4 FAIL | head -12; fi
    rm -f "$PAYLOADS" 2>/dev/null || true
  fi
  return 0
}

rule_tests() {
  if [ "$QUICK" -eq 1 ]; then warn "test suite skipped (--quick)"; return 0; fi

  # Use the project's own interpreter. A bare `python3` on macOS is not the venv
  # and usually has no pytest at all.
  local PY="$ROOT/backend/.venv/bin/python3"
  # From the Linux side the venv's python is a symlink to a Mac binary and is
  # not executable; TC_PY names an interpreter that has the requirements.
  [ -n "${TC_PY:-}" ] && [ -x "${TC_PY}" ] && PY="$TC_PY"
  [ -x "$PY" ] || PY="$(command -v python3)"

  # Off the machine that holds the database the suite reads the newest
  # ledger snapshot, never the live file (rule 5; the fourth incident on
  # 2026-09-21 was a mode=ro open from the VM, the third an immutable one).
  # Tests that need bars/quotes skip there with the reason and run in full
  # on the Mac -- see skip_without() in tests/conftest.py.
  local dbpath=""
  if [ "$(uname -s)" != "Darwin" ] || [ "${ROOT#"$HOME"/mnt/}" != "$ROOT" ]; then
    dbpath=$(ls -1t "$ROOT"/data/ledger/ledger-*.sqlite 2>/dev/null | head -1)
    if [ -z "$dbpath" ]; then
      bad "not on the machine that holds the database and no ledger snapshot to run the suite against"
      return 0
    fi
  fi
  local out passed
  out=$(cd "$ROOT/backend" && env ${dbpath:+TC_DB_PATH="$dbpath"} "$PY" -m pytest tests/ -q 2>&1)
  passed=$(printf '%s\n' "$out" | grep -oE '[0-9]+ passed' | tail -1)

  # POSITIVE EVIDENCE, NOT THE ABSENCE OF BAD NEWS.
  #
  # The old version failed only if the output mentioned "failed" or "error".
  # On 2026-09-18 `python3 -m pytest` on the operator's Mac printed
  # "No module named pytest" — no such word — so the gate reported `ok pytest —`
  # with a blank count while a real test was failing, and handed back a build
  # containing a deliberate re-break. A gate that treats silence as success is
  # worse than no gate: it is a gate that lies.
  if [ -z "$passed" ]; then
    bad "pytest did not report a passing count — the suite did not run"
    printf '%s\n' "$out" | tail -4 | while read -r l; do note "$l"; done
    note "interpreter: $PY"
  elif [ "$(printf '%s\n' "$out" | grep -cE '[0-9]+ (failed|error)')" -gt 0 ]; then
    bad "pytest — $(printf '%s\n' "$out" | tail -2 | head -1)"
    printf '%s\n' "$out" | grep FAILED | head -8 | while read -r l; do note "$l"; done
  else
    ok "pytest — $passed"
  fi
  return 0
}

# ── R11 · no deliberate break left behind ────────────────────────────────────
# Proving a fix by re-breaking it (5.2) means the codebase spends a few seconds
# deliberately broken. On 2026-09-18 a shell timeout killed the restore step and
# a re-break marker shipped: the engine journalled its breakeven lock under the
# wrong event name for an hour, and the gate's pytest check was too weak to say so.
# ── R-envelope · every API route answers inside {success, data, ...} ─────────
# 2026-09-19: /liveness and /vault returned their dict bare. The frontend's
# req() unwraps `.data` from every response, so both cards rendered blank --
# "the mechanism registry has not reported yet" on a registry that had reported
# 84 orders -- with no error anywhere, for a day. A route that skips ok() is a
# blank card that looks like a quiet system.
rule_envelope() {
  local hits
  hits=$(awk '
    /^@router\.(get|post|put|delete)/ { route=$0; armed=1; next }
    /^def / { if (!armed) route=""; armed=0 }
    /^    return / {
      if ($0 !~ /return ok\(/ && $0 !~ /return _ok\(/ && $0 !~ /return \{"success"/ \
          && $0 !~ /StreamingResponse|FileResponse|Response\(|RedirectResponse|JSONResponse/) {
        print route " -> " $0
      }
    }' "$ROOT/backend/app/api/v1/routes.py" | grep -v '^ ->' | grep -v '^->' | head -8 || true)
  if [ -n "$hits" ]; then
    bad "route(s) answer without the {success,data} envelope — the UI will render them blank"
    printf '%s\n' "$hits" | while read -r l; do note "${l:0:140}"; done
  else
    ok "every route answers inside the envelope"
  fi
  return 0
}

rule_no_leftover_breaks() {
  local hits
  # The pattern is assembled so this file does not match itself.
  local pat="RE-?BR""OKEN"
  hits=$(grep -rnE "$pat" "$ROOT/backend/app" "$ROOT/frontend/src" \
           --include=*.py --include=*.js --include=*.jsx 2>/dev/null | head -5 || true)
  if [ -n "$hits" ]; then
    bad "a deliberate re-break was left in the code"
    printf '%s\n' "$hits" | while read -r l; do note "${l:0:120}"; done
  else
    ok "no re-break markers left behind"
  fi
  return 0
}

# ── housekeeping · the conditions under which the safety nets actually work ──
# A backup refuses to run when the disk is nearly full, and a backup that cannot
# run is the difference between losing ten minutes and losing a day.
rule_housekeeping() {
  local free_mb
  free_mb=$(df -Pm "$ROOT" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "$free_mb" ]; then
    if [ "$free_mb" -lt 3000 ]; then
      bad "only ${free_mb} MB free — backups refuse to run below 3000 MB"
    else ok "disk: ${free_mb} MB free"; fi
  fi

  # The tests run on whatever volume holds TMPDIR, not on the project's. On
  # 2026-09-18 that volume hit 100% and pytest failed with "could not create
  # numbered dir" — which the gate reported as a test failure. An environment
  # problem must never be handed back as a code problem.
  local tmpdir tmp_free
  tmpdir="${TMPDIR:-/tmp}"
  tmp_free=$(df -Pm "$tmpdir" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "$tmp_free" ]; then
    if [ "$tmp_free" -lt 500 ]; then
      bad "only ${tmp_free} MB free on $tmpdir — the test suite cannot create temp dirs"
    else ok "temp space: ${tmp_free} MB free on $tmpdir"; fi
  fi

  local newest age_h
  newest=$(ls -1t "$ROOT/data/backups"/*.sqlite 2>/dev/null | head -1)
  if [ -n "$newest" ]; then
    age_h=$(( ( $(date +%s) - $(date -r "$newest" +%s) ) / 3600 ))
    # The daily job runs every 24h, so 36h was already a missed one -- and it
    # only warned. On 2026-09-18 the gate printed "ok full backup 30h old" while
    # every backup since the 17th had failed with "cannot VACUUM from within a
    # transaction". Past one missed day this is a FAILURE, not a note: the
    # backup is what made the corruption survivable.
    if [ "$age_h" -gt 30 ]; then
      bad "newest full backup is ${age_h}h old — the daily backup is not running"
      note "check: scheduler_jobs.last_summary for 'housekeeping'"
    elif [ "$age_h" -gt 26 ]; then warn "newest full backup is ${age_h}h old"
    else ok "full backup ${age_h}h old — $(basename "$newest")"; fi
  else bad "no full backup in data/backups"; fi

  newest=$(ls -1t "$ROOT/data/ledger"/*.sqlite 2>/dev/null | head -1)
  if [ -n "$newest" ]; then
    age_h=$(( ( $(date +%s) - $(date -r "$newest" +%s) ) / 60 ))
    if [ "$age_h" -gt 30 ]; then warn "newest ledger snapshot is ${age_h} min old (job runs every 10)"
    else ok "ledger snapshot ${age_h} min old"; fi
  else warn "no ledger snapshot yet"; fi

  local logmb
  logmb=$(du -m "$ROOT/logs/tradecrypto.log" 2>/dev/null | cut -f1)
  if [ -n "$logmb" ] && [ "$logmb" -gt 50 ]; then
    warn "tradecrypto.log is ${logmb} MB — many boots in one file, scope your reads"
  elif [ -n "$logmb" ]; then ok "log ${logmb} MB"; fi
  return 0
}

# ── rules no script can check ────────────────────────────────────────────────
MANUAL=(
  "R6b No git write (commit/add/gc) was run inside a mounted folder from the Linux side — it leaves locks the Mac cannot get past."
"R2  Every claim you made in the answer has its evidence quoted in the answer."
"R2  A named value was checked twice — is it 401? check. no, 403? check again."
"R4  Every fix has a test, and the test was proven by re-breaking the mechanism."
"R7  Anything you got wrong earlier is corrected in the same message, not buried."
"R9  Any number you quote about lost or changed data was counted, not remembered."
)

hdr "rules that can be checked"
rule_log_scoping
rule_db_readonly
rule_vault
rule_liveness
rule_pycache
rule_trade_ids
rule_journal
rule_no_leftover_breaks
rule_envelope

hdr "checkers"
rule_checkers
rule_tests

hdr "housekeeping"
rule_housekeeping

hdr "rules that cannot be checked — confirm each before handing anything back"
for m in "${MANUAL[@]}"; do printf '   \033[2m[ ]\033[0m %s\n' "$m"; done

echo
if [ "$fails" -eq 0 ]; then
  printf '\033[32mgate: clear\033[0m — the manual list above is still yours to confirm.\n\n'
  exit 0
fi
printf '\033[31mgate: %d failure(s)\033[0m — fix these before handing anything back.\n\n' "$fails"
exit 1
