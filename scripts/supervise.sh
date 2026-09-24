#!/usr/bin/env bash
# Keep TradeCrypto running, restart it if it dies, and preserve the evidence.
#
# Two failures shaped this file:
#
#   1. A Python process died overnight with nobody to notice. Hence the restart
#      loop, with backoff so a broken build cannot spin the CPU until morning.
#
#   2. run.sh exited 0 after a failure (it had wrongly reported itself healthy
#      by reading another instance's /health), this script read 0 as "deliberate
#      shutdown", and TradeCrypto stayed dead all day. So exit 0 is no longer
#      trusted on its own: a stop is deliberate only if the operator asked for
#      one, or the app ran long enough to have been genuinely up.
#
# Deliberate stop  : touch data/STOP_SUPERVISOR   (or send SIGTERM/SIGINT)
# Exit code 3      : the app hit its own memory ceiling and wants a restart.
# Exit code 4      : port already in use — another instance is running.
# Exit code 5      : macOS TCC is blocking file access; restarting cannot help.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$PROJECT/logs"
LOG="$LOG_DIR/tradecrypto.log"
STOP_FILE="$PROJECT/data/STOP_SUPERVISOR"
MAX_LOG_BYTES=$((20 * 1024 * 1024))    # rotate at 20 MB; three generations kept
MIN_BACKOFF=5
MAX_BACKOFF=300
GRACE_SECONDS=90                       # ran less than this = it never really started

# launchd gives agents a minimal PATH. Restore the usual roots here too, so the
# diagnostics in this file (and anything run.sh shells out to) can be found.
for _d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/Library/pnpm" \
          "$HOME/.volta/bin" /Library/Frameworks/Python.framework/Versions/3.13/bin \
          /Library/Frameworks/Python.framework/Versions/3.12/bin \
          /Library/Frameworks/Python.framework/Versions/3.11/bin; do
  [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
export PATH

mkdir -p "$LOG_DIR" "$PROJECT/data"
cd "$PROJECT"

# Control Deck shows each project's terminal pane, and a pane that prints
# nothing reads as a project that never started. Everything goes to both.
say() { echo "[supervise] $(date '+%F %T') $*" | tee -a "$LOG"; }

rotate() {
  [ -f "$LOG" ] || return 0
  [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt "$MAX_LOG_BYTES" ] || return 0
  rm -f "$LOG.3" 2>/dev/null || true
  [ -f "$LOG.2" ] && mv -f "$LOG.2" "$LOG.3"
  [ -f "$LOG.1" ] && mv -f "$LOG.1" "$LOG.2"
  mv -f "$LOG" "$LOG.1"
}

stop_requested() { [ -f "$STOP_FILE" ]; }

release_lock() {
  [ -n "${PIDFILE:-}" ] || return 0
  [ -f "$PIDFILE" ] || return 0
  [ "$(cat "$PIDFILE" 2>/dev/null || true)" = "$$" ] && rm -f "$PIDFILE"
  return 0
}

trap 'say "supervisor received SIGTERM"; release_lock; exit 143' TERM
trap 'say "supervisor received SIGINT";  release_lock; exit 130' INT
trap 'release_lock' EXIT

if stop_requested; then
  say "STOP_SUPERVISOR present — refusing to start. Remove it to run again."
  exit 0
fi

# ── one supervisor, always ───────────────────────────────────────────────────
#
# Control Deck SIGKILLs whatever is listening on this project's ports before it
# starts it. It cannot see a SUPERVISOR that way, because a supervisor holds no
# port -- so a second Start left two restart loops alive, each reviving the
# processes the other had just lost. That is what 16 backend kills, 13 frontend
# kills and 574 "port already in use" starts in the log actually were.
#
# A new supervisor therefore takes the project over from an old one, and waits
# for the port to genuinely clear before handing it to run.sh.
PIDFILE="$PROJECT/data/supervisor.pid"

# Signal a process and everything descended from it, children first.
#
# Signalling the supervisor alone is not enough: while it is sitting in
# `bash ./run.sh`, bash defers the trap until that foreground job finishes, and
# run.sh does not finish on its own. SIGKILLing the supervisor instead would
# leave run.sh, uvicorn and vite orphaned and STILL holding port 8006 -- the
# exact state this whole change exists to end. So the children go down first;
# the parent's queued trap then runs the moment its foreground job returns.
tree_signal() {
  local pid="$1" sig="$2" kid
  for kid in $(pgrep -P "$pid" 2>/dev/null || true); do
    tree_signal "$kid" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null || true
}

stop_supervisor() {
  local old="$1" _
  kill -TERM "$old" 2>/dev/null || true      # queue its trap
  tree_signal "$old" TERM                    # let its foreground job finish
  for _ in $(seq 1 60); do
    kill -0 "$old" 2>/dev/null || return 0
    sleep 0.25
  done
  say "supervisor $old did not stop in 15s — forcing its whole tree down"
  tree_signal "$old" KILL
  for _ in $(seq 1 20); do
    kill -0 "$old" 2>/dev/null || return 0
    sleep 0.25
  done
  return 1
}

if [ -f "$PIDFILE" ]; then
  OLD="$(tr -dc '0-9' < "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD" ] && [ "$OLD" != "$$" ] && kill -0 "$OLD" 2>/dev/null; then
    say "supervisor $OLD is already running — taking the project over from it"
    stop_supervisor "$OLD" || say "WARNING: supervisor $OLD is still alive; expect a port conflict below"
  fi
fi
echo "$$" > "$PIDFILE"

# The handover is not finished until the port is actually free. Without this,
# run.sh races the outgoing instance and exits 4, and a 300-second wait for a
# port that cleared half a second later looks exactly like a hang.
_wait_port="${BACKEND_PORT:-8006}"
if command -v lsof >/dev/null 2>&1; then
  for _ in $(seq 1 40); do
    _held="$(lsof -ti tcp:"$_wait_port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
    [ -z "${_held// /}" ] && break
    sleep 0.25
  done
  [ -n "${_held// /}" ] && say "port $_wait_port still held by ${_held% } after 10s — run.sh will report it"
fi

backoff=$MIN_BACKOFF
consecutive_fast_failures=0

while true; do
  rotate
  say "starting"
  started_at=$(date +%s)
  # `bash ./run.sh`, not `./run.sh`. Executing the script directly goes through
  # execve, which macOS refuses with EPERM ("Operation not permitted") when the
  # file carries a com.apple.quarantine attribute — which files written by a
  # sandboxed helper pick up. Passing it to bash as an argument is a plain read
  # and sidesteps the whole question. Clear the attribute too: xattr -cr .
  # tee, not >>, so Control Deck's pane shows the app starting. PIPESTATUS[0]
  # because through a pipe $? is tee's status, and every decision below turns
  # on run.sh's real exit code.
  # Arguments given to the supervisor are handed to every relaunch, so
  # `bash scripts/supervise.sh --phone` keeps the tunnel on across restarts
  # rather than only on the first one.
  bash ./run.sh "$@" 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
  ran_for=$(( $(date +%s) - started_at ))
  say "exited with $code after ${ran_for}s"

  if stop_requested; then
    say "STOP_SUPERVISOR present — stopping as asked."
    exit 0
  fi

  case $code in
    130|143)
      say "interrupted — not restarting"; exit 0 ;;
    5)
      # A permissions problem is not something a restart can cure. Keep the
      # process alive but idle, checking rarely, so the fix takes effect
      # without a crash loop hammering the machine in the meantime.
      say "FATAL: macOS is blocking file access (see the message above)."
      say "Restarting cannot fix this. Waiting ${MAX_BACKOFF}s and retrying."
      sleep "$MAX_BACKOFF"; continue ;;
    4)
      say "another instance holds the port — waiting ${MAX_BACKOFF}s"
      sleep "$MAX_BACKOFF"; continue ;;
    3)
      say "memory-ceiling restart (planned)"
      backoff=$MIN_BACKOFF; consecutive_fast_failures=0 ;;
    6)
      # core/autoapply.py: the source on disk was newer than the running build
      # and nothing was in flight, so the engine restarted itself. Identical
      # handling to 3 (planned, no backoff) but it must not be REPORTED as a
      # memory-ceiling restart -- on 2026-09-18 it was, and the log said the
      # desk had hit its memory ceiling when it had simply picked up new code.
      say "restarted to pick up new code (planned)"
      # NO PAUSE. A backoff exists to stop a broken build spinning all night;
      # exit 6 is the process ASKING to be restarted, having already checked
      # that nothing is in flight. There is nothing to back off from, and the
      # pause was 5 of the 8 seconds every code update cost -- measured, not
      # assumed: three restarts on 2026-09-19 reported 8s of downtime each.
      #
      # Every second here is a second of quotes that cannot be recovered. Bars
      # are refetched from the exchange by minute_topup; the live mid is not
      # stored anywhere else, so a gap in it is permanent.
      backoff=0; consecutive_fast_failures=0 ;;
    0)
      if [ "$ran_for" -ge "$GRACE_SECONDS" ]; then
        say "ran ${ran_for}s then exited cleanly — treating as a deliberate stop"
        exit 0
      fi
      say "exited 0 after only ${ran_for}s — that is a failed start, not a shutdown"
      consecutive_fast_failures=$(( consecutive_fast_failures + 1 )) ;;
    *)
      if [ "$ran_for" -ge "$GRACE_SECONDS" ]; then
        backoff=$MIN_BACKOFF; consecutive_fast_failures=0
      else
        consecutive_fast_failures=$(( consecutive_fast_failures + 1 ))
      fi ;;
  esac

  if [ "$consecutive_fast_failures" -gt 0 ]; then
    backoff=$(( backoff * 2 ))
    [ $backoff -gt $MAX_BACKOFF ] && backoff=$MAX_BACKOFF
  fi
  if [ "$consecutive_fast_failures" = 5 ]; then
    say "5 fast failures in a row — the log above holds the reason; still retrying"
  fi

  if [ "$backoff" -gt 0 ]; then
    say "restarting in ${backoff}s"
    sleep "$backoff"
  else
    say "restarting now"
  fi
done
