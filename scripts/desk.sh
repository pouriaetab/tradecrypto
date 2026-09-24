#!/usr/bin/env bash
# desk.sh — the on/off switch for the trading desk.
#
# With the LaunchAgent installed the desk runs 24/7 and restarts itself, which
# is what you want almost all of the time and exactly what you do NOT want while
# you are working on it. A plain always-restart policy makes Control Deck's Stop
# button useless: it kills the process and launchd brings it straight back.
#
# So the authority is a file, not a button: data/STOP_SUPERVISOR, which
# supervise.sh already refuses to start against. launchd is told the same thing,
# so while that file exists NOTHING can bring the desk up -- not launchd, not
# Control Deck's Start. Remove it and the desk comes back. Nothing races,
# nothing needs remembering, and `status` always says which state you are in.
#
#   ./scripts/desk.sh off      hard stop — stays down
#   ./scripts/desk.sh on       run, and keep running
#   ./scripts/desk.sh status   what is true right now
#   ./scripts/desk.sh restart  bounce it (picks up code changes)
set -uo pipefail
# DESK_ROOT exists so this switch can be exercised against a scratch tree. It was
# added after `off` was run against the live desk to "test" it, which wrote the
# real stop file. An on/off switch for production is not something to try out on
# production.
if [ -n "${DESK_ROOT:-}" ]; then
  ROOT="$DESK_ROOT"; mkdir -p "$ROOT/data"
else
  cd "$(dirname "$0")/.." || exit 2
  ROOT="$PWD"
fi
PAUSE="$ROOT/data/STOP_SUPERVISOR"
LABEL="com.tradecrypto.app"
CLAIM="$ROOT/data/.db-owner.json"

installed() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

running() {
  python3 - "$CLAIM" <<'PY' 2>/dev/null
import json, sys, time
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if time.time() - float(c["heartbeat"]) < 90 else 1)
PY
}

state() {
  printf '  %-12s %s\n' "autostart" "$(installed && echo 'installed' || echo 'NOT installed — the desk only runs while Control Deck runs it')"
  if [ -f "$PAUSE" ]; then
    printf '  %-12s %s\n' "paused" "YES — since $(date -r "$PAUSE" '+%Y-%m-%d %H:%M'). Nothing can start it (not launchd, not Control Deck) until: ./scripts/desk.sh on"
  else
    printf '  %-12s %s\n' "paused" "no"
  fi
  if running; then
    printf '  %-12s %s\n' "desk" "RUNNING (pid $(python3 -c "import json;print(json.load(open('$CLAIM'))['pid'])" 2>/dev/null || echo '?'))"
  else
    printf '  %-12s %s\n' "desk" "not running"
  fi
}

case "${1:-status}" in
  off)
    : > "$PAUSE"
    installed && launchctl kill SIGTERM "gui/$(id -u)/$LABEL" 2>/dev/null
    pkill -f "scripts/supervise.sh" 2>/dev/null
    echo "desk paused — it will stay down until: ./scripts/desk.sh on"
    ;;
  on)
    # Some environments refuse deletes inside the project folder. Moving the
    # file aside clears the pause just as well; failing silently would not.
    if ! rm -f "$PAUSE" 2>/dev/null && [ -f "$PAUSE" ]; then
      mkdir -p "$ROOT/data/_to_delete"
      mv "$PAUSE" "$ROOT/data/_to_delete/STOP_SUPERVISOR-$(date +%H%M%S)" 2>/dev/null \
        || { echo "could not clear $PAUSE — remove it by hand"; exit 1; }
    fi
    if installed; then
      launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null
      echo "desk resumed — launchd is bringing it up"
    else
      echo "autostart is not installed; start it from Control Deck, or run:"
      echo "  ./scripts/install-autostart.sh"
    fi
    ;;
  restart)
    if installed; then
      rm -f "$PAUSE" 2>/dev/null || true
      launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null
      echo "restarting…"
    else
      echo "autostart is not installed; use Control Deck's Restart."
    fi
    ;;
  status) state; exit 0 ;;
  *) echo "usage: $0 [on|off|restart|status]"; exit 2 ;;
esac
echo
state
