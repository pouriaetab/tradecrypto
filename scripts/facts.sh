#!/usr/bin/env bash
# facts.sh — the current, verified state of this app. Evidence, never inference.
#
# WHY THIS EXISTS
#
# On 2026-09-18 the assistant told the operator not to press the Phone button,
# because the log said `phone access is ON (same address as before)`. That line
# was real. It was also from an earlier boot, several thousand lines up a 15 MB
# log that spans sixteen restarts. The current boot had never printed it, phone
# access was off, and the operator spent the morning pasting a valid token into
# a backend that was never going to look at one.
#
# Nothing was wrong with the log. What was wrong was reading it without
# establishing which boot it belonged to.
#
# So every log fact printed here is scoped to the CURRENT boot, and if the boot
# cannot be located this says UNKNOWN and stops. It never silently widens to the
# whole file, because a fact from the wrong boot is worse than no fact at all:
# it reads exactly like the truth.
#
# Usage:  scripts/facts.sh [boot|phone|db|jobs|all]     (default: all)
# Safe to run while trading. Reads only; never opens the database read-write.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
ROOT="$PWD"
LOG="$ROOT/logs/tradecrypto.log"

hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
fact() { printf '   %-26s %s\n' "$1" "$2"; }
warn() { printf '   \033[33m%-26s %s\033[0m\n' "$1" "$2"; }
bad()  { printf '   \033[31m%-26s %s\033[0m\n' "$1" "$2"; }
note() { printf '   \033[2m%s\033[0m\n' "$1"; }

# ── which boot are we looking at ─────────────────────────────────────────────
# run.sh prints this once per start, before uvicorn says anything, so it is the
# first line of a boot and everything after it belongs to that boot.
BOOT_MARK="starting backend on"
BOOT_LINE=""
[ -r "$LOG" ] && BOOT_LINE=$(grep -n "$BOOT_MARK" "$LOG" 2>/dev/null | tail -1 | cut -d: -f1)

cur() {           # every log read in this script goes through here
  if [ -z "$BOOT_LINE" ]; then return 1; fi
  tail -n +"$BOOT_LINE" "$LOG"
}

require_boot() {
  if [ -z "$BOOT_LINE" ]; then
    bad "current boot" "UNKNOWN — no '$BOOT_MARK' line in $(basename "$LOG")"
    note "the log has probably rotated. Look in tradecrypto.log.1, and do NOT"
    note "read the whole file: older boots say things that are no longer true."
    return 1
  fi
  return 0
}

section_boot() {
  hdr "boot"
  require_boot || return 1
  local started boots
  started=$(cur | grep -oE "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}" | head -1)
  boots=$(grep -c "$BOOT_MARK" "$LOG" 2>/dev/null)
  fact "log file" "$(basename "$LOG") ($(du -h "$LOG" 2>/dev/null | cut -f1))"
  fact "boots in this file" "$boots — only the last one is current"
  fact "current boot begins" "line $BOOT_LINE${started:+, $started local}"
  # NOT `cur | grep -q`: grep exits at the first match, tail takes SIGPIPE, and
  # under `set -o pipefail` the pipeline reports failure. This check reported
  # "no completion line yet" about an app that was serving 12,875 requests.
  local done_n fail_n
  done_n=$(cur | grep -c "Application startup complete" || true)
  fail_n=$(cur | grep -cE "died during startup|Application startup failed" || true)
  if [ "${fail_n:-0}" -gt 0 ] && [ "${done_n:-0}" -eq 0 ]; then
    bad  "startup" "FAILED — the app is not serving"
  elif [ "${done_n:-0}" -gt 0 ]; then
    fact "startup" "completed"
  else
    warn "startup" "no completion line yet"
  fi
  # A paused desk must never be a mystery. Without this line, "it will not
  # start" looks identical to "it is broken".
  if [ -f "$ROOT/data/STOP_SUPERVISOR" ]; then
    warn "PAUSED" "data/STOP_SUPERVISOR exists — nothing will start the desk until: ./scripts/desk.sh on"
  fi
  local flags
  flags=$(cur | grep -oE "phone access is ON[^\"]*" | head -1 || true)
  fact "this boot printed" "${flags:-(nothing about phone access)}"
}

section_phone() {
  hdr "phone / outside access"
  require_boot || return 1
  local rows
  rows=$(cur | grep -oE '^INFO: +[0-9.]+:[0-9]+ - "[A-Z]+ [^"]+" [0-9]{3}' \
        | awk '{ip=$2; st=$NF; sub(/:.*/,"",ip);
                print (ip=="127.0.0.1" ? "loopback" : "outside " ip), st}' \
        | sort | uniq -c | sort -rn)
  if [ -z "$rows" ]; then
    warn "requests this boot" "none recorded"
    return 0
  fi
  printf '%s\n' "$rows" | while read -r n who st; do
    fact "$who" "$st  ×$n"
  done
  # The verdict, and it is read off the status code rather than guessed.
  local out403 out401 out200
  out403=$(printf '%s\n' "$rows" | awk '$2=="outside" && $NF=="403"{s+=$1} END{print s+0}')
  out401=$(printf '%s\n' "$rows" | awk '$2=="outside" && $NF=="401"{s+=$1} END{print s+0}')
  out200=$(printf '%s\n' "$rows" | awk '$2=="outside" && $NF=="200"{s+=$1} END{print s+0}')
  echo
  if   [ "$out200" -gt 0 ]; then fact "verdict" "outside access is WORKING"
  elif [ "$out403" -gt 0 ]; then
    bad  "verdict" "403 — phone access is OFF on this Mac"
    note "A token cannot fix a 403. The backend is bound to localhost and never"
    note "checks one. Restart with the Phone button, or: bash run.sh --phone"
  elif [ "$out401" -gt 0 ]; then
    warn "verdict" "401 — phone access is on, the token is missing or wrong"
    note "Token lives in secrets/lan_token.txt. This one a paste does fix."
  else
    fact "verdict" "nothing has reached this backend from outside"
  fi
}

section_db() {
  hdr "database"
  local claim="$ROOT/data/.db-owner.json"
  if [ ! -r "$claim" ]; then
    warn "owner" "no claim file — the app is not holding the database"
    return 0
  fi
  python3 - "$claim" <<'PY'
import json, sys, time
try:
    c = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"   owner                      UNREADABLE ({e})"); raise SystemExit
age = time.time() - float(c.get("heartbeat", 0))
live = age <= 90
print(f"   {'held by':<26} {c.get('role','?')} on {c.get('host','?')}/{c.get('system','?')} (pid {c.get('pid','?')})")
print(f"   {'heartbeat':<26} {age:.0f}s ago — {'ALIVE' if live else 'STALE'}")
if live:
    print("   \033[2mAnything on another machine must open this database READ-ONLY.\033[0m")
    print("   \033[2m  TC_DB_READONLY=1, or sqlite3 'file:...?mode=ro'. Two writers destroys it.\033[0m")
PY
  local db="$ROOT/data/tradecrypto.sqlite"
  [ -f "$db" ] && fact "file" "$(du -h "$db" | cut -f1), modified $(date -r "$db" '+%m-%d %H:%M' 2>/dev/null)"
  local n
  n=$(ls -1 "$ROOT/data/backups" 2>/dev/null | wc -l | tr -d ' ')
  fact "full backups" "${n:-0} in data/backups"
  n=$(ls -1 "$ROOT/data/ledger" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${n:-0}" -gt 0 ]; then
    fact "ledger snapshots" "$n, newest $(ls -1t "$ROOT/data/ledger" | head -1)"
  else
    warn "ledger snapshots" "none yet — the 10-minute job has not run"
  fi
}

section_jobs() {
  hdr "scheduled jobs"
  python3 - "$ROOT" <<'PY'
import re, sqlite3, sys, time
from pathlib import Path
root = Path(sys.argv[1])
src = (root / "backend/app/core/scheduler.py").read_text()
HOUR, DAY = 3600.0, 86400.0
code = {}
for m in re.finditer(r'"(\w+)": \{"fn": _job_\w+, "interval": ([^,]+),', src):
    try: code[m.group(1)] = eval(m.group(2), {"HOUR": HOUR, "DAY": DAY})
    except Exception: pass
# NEVER the live file from another machine. On 2026-09-21 09:08 this very
# block opened data/tradecrypto.sqlite with mode=ro from the Linux VM; twelve
# seconds later the Mac backend died with "Bus error: 10" (SIGBUS) -- a WAL
# reader maps and writes the -shm index, the bridge mount is not coherent
# with the app's mapping, and the app's mapped pages went away under it.
# Fourth incident of the same class (CLAUDE.md rule 5). From anywhere but
# the machine that holds the claim, read the newest ledger snapshot
# immutable: a static copy, nothing mapped, nothing written.
import json, platform, os
claim = {}
try: claim = json.loads((root / "data/.db-owner.json").read_text())
except Exception: pass
me = platform.node().split(".")[0].lower()
holder = str(claim.get("host", "")).split(".")[0].lower()
same_machine = (platform.system() == "Darwin" and holder and holder == me
                and not str(root).startswith(os.path.expanduser("~/mnt")))
if same_machine:
    target, how = root / "data/tradecrypto.sqlite", "live file, same machine"
else:
    snaps = sorted((root / "data/ledger").glob("ledger-*.sqlite"))
    if not snaps:
        print(f"   \033[33m{'cannot read':<26} not on the machine that holds the database and no ledger snapshot\033[0m")
        raise SystemExit
    target, how = snaps[-1], f"snapshot {snaps[-1].name} (the live file is never opened from here)"
try:
    c = sqlite3.connect(f"file:{target}?mode=ro&immutable=1", uri=True, timeout=5)
    rows = list(c.execute("SELECT name, interval_s, last_run FROM scheduler_jobs"))
except sqlite3.Error as e:
    print(f"   \033[33m{'cannot read':<26} {e}\033[0m")
    raise SystemExit
print(f"   {'read from':<26} {how}")
now, drift, late = time.time(), [], []
for n, i, l in rows:
    want = code.get(n)
    if want is not None and abs(want - i) > 1:
        drift.append(f"{n}: db {i/3600:.1f}h vs code {want/3600:.1f}h")
    if l and (now - l) > i * 2:
        late.append(f"{n} ({(now-l)/3600:.1f}h)")
print(f"   {'jobs':<26} {len(rows)} registered")
if drift:
    for d in drift: print(f"   \033[33m{'INTERVAL DRIFT':<26} {d}\033[0m")
else:
    print(f"   {'intervals':<26} match the code")
print(f"   {'overdue':<26} {', '.join(late) if late else 'none'}")
PY
}

case "${1:-all}" in
  boot)  section_boot ;;
  phone) section_phone ;;
  db)    section_db ;;
  jobs)  section_jobs ;;
  all)   section_boot; section_phone; section_db; section_jobs ;;
  *)     echo "usage: $0 [boot|phone|db|jobs|all]"; exit 2 ;;
esac
echo
