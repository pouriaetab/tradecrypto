#!/usr/bin/env bash
# TradeCrypto — unified entry point (control_deck RUN_SH_STANDARD)
#
# NOTE: bash, not zsh. An earlier version used the construct
#     (cd frontend && VAR=x VAR2=y { cmd_a || cmd_b; }) &
# which is a SYNTAX ERROR in both shells: you cannot prefix a brace group with
# environment assignments. A syntax error anywhere means the whole script never
# parses, so nothing started and Control Deck waited forever on the port.
# Keep this file passing `bash -n run.sh`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_NAME="TradeCrypto"
BACKEND_PORT="${BACKEND_PORT:-8006}"
FRONTEND_PORT="${FRONTEND_PORT:-5180}"
export BACKEND_PORT FRONTEND_PORT

# launchd starts LaunchAgents with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin).
# Homebrew/nvm node, pnpm and the Python framework are all invisible there, which
# is exactly how the autostart died with "exec: npm: not found". Put the usual
# install roots back before anything else runs.
for _d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin" "$HOME/Library/pnpm" \
          "$HOME/.volta/bin" /Library/Frameworks/Python.framework/Versions/3.13/bin \
          /Library/Frameworks/Python.framework/Versions/3.12/bin \
          /Library/Frameworks/Python.framework/Versions/3.11/bin; do
  [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
if [ -d "$HOME/.nvm/versions/node" ]; then
  _nvm="$(ls -1d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
  [ -n "${_nvm:-}" ] && PATH="$_nvm:$PATH"
fi
export PATH

if [ -t 1 ]; then
  BLUE=$'\033[34m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; RESET=$'\033[0m'
else
  BLUE=''; GREEN=''; RED=''; YEL=''; RESET=''
fi
log()  { echo "${BLUE}[run.sh]${RESET}: $1" >&2; }
ok()   { echo "${GREEN}[run.sh]${RESET}: $1" >&2; }
warn() { echo "${YEL}[run.sh]${RESET}: $1" >&2; }
err()  { echo "${RED}[run.sh]${RESET}: $1" >&2; }

cd "$SCRIPT_DIR"
[ -f .env ] || { cp .env.example .env; log "created .env from .env.example"; }

# macOS privacy protection (TCC) applies to ~/Desktop, ~/Documents and ~/Downloads.
# A process started by launchd has no access to them unless its BINARY has been
# granted Full Disk Access, and the failure looks like a nonsense error:
#     PermissionError: [Errno 1] Operation not permitted: .../.venv/pyvenv.cfg
# on a file that any Terminal can read. Say so in words rather than letting
# Python die with a traceback nobody can interpret.
case "$SCRIPT_DIR" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
    if ! head -c 1 run.sh >/dev/null 2>&1; then
      err "cannot read this project's own files, though they exist and are yours."
      err "That is macOS privacy protection (TCC), not a file permission."
      err "project: $SCRIPT_DIR"
      err "Fix it one of two ways:"
      err "  1. System Settings > Privacy & Security > Full Disk Access > '+',"
      err "     then Cmd-Shift-G, type /bin/bash, add it, switch it on."
      err "  2. Move the project out of Desktop/Documents/Downloads."
      exit 5
    fi
    ;;
esac

find_python() {
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

# pyvenv.cfg is a real file; bin/python is a symlink to the host interpreter,
# which reads as dangling across the Cowork bridge. Use the file as the marker.
backend_ready()  { [ -f backend/.venv/pyvenv.cfg ]; }
frontend_ready() { [ -x frontend/node_modules/.bin/vite ]; }

# `command -v pnpm` only proves the file is on PATH. A pnpm newer than the
# installed Node refuses to run at all:
#     ERROR: This version of pnpm requires at least Node.js v22.13
#     The current version of Node.js is v18.13.0
# which is how the dashboard stopped starting while the backend was fine. So the
# test is whether pnpm can execute, not whether it exists.
pnpm_works() { command -v pnpm >/dev/null 2>&1 && pnpm --version >/dev/null 2>&1; }
npm_works()  { command -v npm  >/dev/null 2>&1 && npm  --version >/dev/null 2>&1; }

# --check runs BEFORE any install so it can diagnose a broken environment
# rather than failing on the thing you are trying to diagnose.
if [ "${1:-}" = "--check" ]; then
  echo "project:        $SCRIPT_DIR"
  echo "python >=3.11:  $(find_python || echo 'NOT FOUND')"
  echo "backend venv:   $(backend_ready && echo present || echo MISSING)"
  echo "frontend vite:  $(frontend_ready && echo present || echo MISSING)"
  echo "pnpm:           $(pnpm_works && pnpm --version || echo "$(command -v pnpm >/dev/null 2>&1 && echo 'installed but WILL NOT RUN' || echo 'not installed')")"
  echo "node:           $(command -v node >/dev/null 2>&1 && node -v || echo 'not installed')"
  echo "mode:           $(grep -E '^TC_EXECUTION_MODE=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
  echo "ports:          backend $BACKEND_PORT / frontend $FRONTEND_PORT"
  if command -v lsof >/dev/null 2>&1; then
    echo "port $BACKEND_PORT held by:  $(lsof -ti tcp:"$BACKEND_PORT" 2>/dev/null | tr '\n' ' ')"
    echo "port $FRONTEND_PORT held by: $(lsof -ti tcp:"$FRONTEND_PORT" 2>/dev/null | tr '\n' ' ')"
  fi
  if command -v node >/dev/null 2>&1 && [ -f scripts/check-frontend.js ]; then
    node scripts/check-frontend.js || true
  fi
  bash -n "$0" && echo "run.sh syntax: OK"
  exit 0
fi

# --doctor answers the only question that matters when it will not start: WHO
# holds the ports, and which supervisor is behind them.
#
# --check prints bare PIDs, and a bare PID cannot tell a Control Deck instance
# from a launchd one. That distinction is the whole difference between "the app
# is broken" and "two supervisors are fighting over port 8006", and not being
# able to see it cost a day.
if [ "${1:-}" = "--doctor" ]; then
  echo "== TradeCrypto doctor =="
  echo "when:            $(date '+%F %T %Z')"
  echo "project:         $SCRIPT_DIR"
  echo
  echo "-- interpreter and dependencies --"
  echo "venv python:     $([ -x backend/.venv/bin/python ] && backend/.venv/bin/python --version 2>&1 || echo 'venv MISSING')"
  echo "backend deps:    $(backend_ready && echo ok || echo INCOMPLETE)"
  echo "frontend vite:   $(frontend_ready && echo present || echo MISSING)"
  echo "node:            $(command -v node >/dev/null 2>&1 && node -v || echo 'not installed')"
  echo "disk free:       $(df -h "$SCRIPT_DIR" 2>/dev/null | awk 'NR==2{print $4}' || echo '?')"
  echo
  echo "-- who owns the ports --"
  for _port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if ! command -v lsof >/dev/null 2>&1; then
      echo "port $_port: (lsof unavailable)"
      continue
    fi
    _pids="$(lsof -ti tcp:"$_port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
    if [ -z "${_pids// /}" ]; then
      echo "port $_port: free"
      continue
    fi
    for _pid in $_pids; do
      echo "port $_port: pid $_pid  $(ps -o command= -p "$_pid" 2>/dev/null | cut -c1-88 || true)"
      _ppid="$(ps -o ppid= -p "$_pid" 2>/dev/null | tr -d ' ' || true)"
      if [ -n "$_ppid" ]; then
        echo "            parent $_ppid  $(ps -o command= -p "$_ppid" 2>/dev/null | cut -c1-88 || true)"
      fi
    done
  done
  echo
  echo "-- supervisors (there must be exactly one) --"
  if [ -f data/supervisor.pid ]; then
    _sp="$(tr -dc '0-9' < data/supervisor.pid 2>/dev/null || true)"
    if [ -n "$_sp" ] && kill -0 "$_sp" 2>/dev/null; then
      echo "pidfile:         supervisor $_sp is RUNNING"
      echo "                 $(ps -o command= -p "$_sp" 2>/dev/null | cut -c1-88 || true)"
    else
      echo "pidfile:         stale (pid ${_sp:-?} is gone) — safe to ignore"
    fi
  else
    echo "pidfile:         none — no supervisor has started this project"
  fi
  if launchctl list 2>/dev/null | grep -q 'com.tradecrypto.app'; then
    echo "launchd agent:   LOADED (com.tradecrypto.app)"
    echo "                 It competes with Control Deck for ports $BACKEND_PORT and $FRONTEND_PORT."
    echo "                 Control Deck SIGKILLs whatever holds those ports when you press"
    echo "                 Start, and this agent revives it five seconds later. Pick one:"
    echo "                   ./scripts/install-autostart.sh remove"
  else
    echo "launchd agent:   not loaded — good, one owner"
  fi
  echo "STOP_SUPERVISOR: $([ -f data/STOP_SUPERVISOR ] && echo 'PRESENT — the supervisor refuses to start' || echo absent)"
  echo "kill switch:     $([ -f data/KILL_SWITCH ] && echo ENGAGED || echo clear)"
  echo
  echo "-- is it actually serving --"
  if command -v curl >/dev/null 2>&1; then
    curl -sf -m 3 "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1 \
      && echo "backend:         answering /health on $BACKEND_PORT" \
      || echo "backend:         NOT answering on $BACKEND_PORT"
    curl -sf -m 3 "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1 \
      && echo "frontend:        serving on $FRONTEND_PORT" \
      || echo "frontend:        NOT serving on $FRONTEND_PORT"
  fi
  exit 0
fi

if ! backend_ready; then
  PY="$(find_python)" || { err "no Python >= 3.11 on PATH"; exit 1; }
  log "creating backend venv with $PY ($("$PY" --version 2>&1))…"
  ( cd backend \
    && "$PY" -m venv .venv \
    && .venv/bin/pip install --upgrade pip >/dev/null \
    && .venv/bin/pip install -r requirements.txt ) \
    || { err "backend install failed"; exit 1; }
  ok "backend dependencies installed"
fi

# pnpm can exit non-zero on advisory conditions (ignored build scripts, update
# notices) while having installed everything correctly, so success is judged by
# whether vite actually landed — never by the exit code.
if ! frontend_ready; then
  log "installing frontend dependencies…"
  if pnpm_works; then
    ( cd frontend && pnpm install ) || warn "pnpm exited non-zero; checking whether it installed anyway"
  elif command -v pnpm >/dev/null 2>&1; then
    warn "pnpm is installed but will not run (probably needs a newer Node); using npm"
  fi
  if ! frontend_ready && npm_works; then
    warn "falling back to npm"
    ( cd frontend && npm install --no-audit --no-fund ) || true
  fi
  if ! frontend_ready; then
    err "frontend install failed — frontend/node_modules/.bin/vite is missing"
    err "try:  cd frontend && pnpm install && pnpm approve-builds"
    exit 1
  fi
  ok "frontend dependencies installed"
fi

MODE="$(grep -E '^TC_EXECUTION_MODE=' .env | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
CONFIRM="$(grep -E '^TC_LIVE_CONFIRM=' .env | head -1 | cut -d= -f2 | tr -d '[:space:]' || true)"
if [ "$MODE" = "mcp" ] && [ "$CONFIRM" = "I_ACCEPT_REAL_MONEY_RISK" ]; then
  warn "=============================================="
  warn " LIVE MODE — real orders can be placed."
  warn " Kill switch: touch data/KILL_SWITCH"
  warn "=============================================="
else
  ok "mode=${MODE:-paper} — real orders are DISABLED"
fi

if [ "${1:-}" = "--selftest" ]; then
  exec backend/.venv/bin/python backend/selftest.py "${@:2}"
fi

BACKEND_PID=""
FRONTEND_PID=""
# `./run.sh --phone` turns the tunnel on for THIS launch only, leaving .env alone.
# That is what lets Control Deck have two buttons against one project: the normal
# Start behaves exactly as it always did, and a second button passes --phone.
for _arg in "$@"; do
  case "$_arg" in
    --phone) TC_TUNNEL=1; export TC_TUNNEL ;;
  esac
done

if [ "${1:-}" = "--tunnel-restart" ]; then
  # Deliberately cycle the tunnel and take a new address. Only needed if the
  # tunnel process has died or wedged; ordinary app restarts keep the old one.
  P="$SCRIPT_DIR/logs/tunnel.pid"
  [ -f "$P" ] && kill "$(cat "$P")" 2>/dev/null || true
  rm -f "$P"
  echo "[run.sh]: tunnel stopped. Start the app again to get a new address."
  exit 0
fi

cleanup() {
  local pid
  for pid in "$FRONTEND_PID" "$BACKEND_PID"; do
    [ -n "$pid" ] || continue
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# If something is ALREADY listening on our port, the health check below would
# get a 200 from that stranger and this instance would report itself healthy
# while its own backend was dying. That is how run.sh exited 0 after a failure
# and told the supervisor "deliberate shutdown — stop restarting".
if command -v lsof >/dev/null 2>&1; then
  held="$(lsof -ti tcp:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "${held// /}" ]; then
    err "port $BACKEND_PORT is already in use by PID(s): $held"
    err "Another TradeCrypto is running. Stop it before starting a second one:"
    err "  kill $held"
    err "To see WHICH one — Control Deck's, a launchd agent's, or an orphan —"
    err "and who its parent is:"
    err "  bash run.sh --doctor"
    exit 4
  fi
fi

# TC_LAN=1 makes the app reachable from other devices on this wifi -- a phone,
# mostly. It is OFF by default and that is deliberate: this process holds
# Robinhood credentials and can place orders, so being reachable is a decision
# rather than a default. With it on, the backend demands a token for anything
# that is not loopback (backend/app/core/access.py) and prints the phone URL.
# Both switches can come from the shell OR from .env, and .env is the one that
# matters in practice: Control Deck launches this script without a custom
# environment, so a setting that only exists as `TC_TUNNEL=1 bash run.sh` means
# the app can only be started from a terminal. Put it in .env once and every
# launcher picks it up.
env_flag() {
  grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]' | cut -d'#' -f1
}
: "${TC_LAN:=$(env_flag TC_LAN)}"
: "${TC_TUNNEL:=$(env_flag TC_TUNNEL)}"

case "$(printf '%s' "${TC_LAN:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) TC_LAN=1 ;;
  *)             TC_LAN="" ;;
esac
export TC_LAN

# TC_TUNNEL=1 is for a network that will not let two of your own devices talk to
# each other -- an apartment or ISP wifi with client isolation, which is what
# 100.x addresses on a /26 mean. Instead of the phone dialling the Mac, the Mac
# dials OUT to Cloudflare and Cloudflare hands back an https address. Outbound
# traffic, so a full-tunnel VPN on this machine does not interfere, and the phone
# needs no VPN of its own. Works on cell data too.
case "$(printf '%s' "${TC_TUNNEL:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) TC_TUNNEL=1 ;;
  *)             TC_TUNNEL="" ;;
esac
export TC_TUNNEL
BIND_HOST=127.0.0.1
if [ -n "$TC_LAN" ]; then
  BIND_HOST=0.0.0.0
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$LAN_IP" ] || LAN_IP="<this-mac-ip>"
fi

log "starting backend on http://${BIND_HOST}:${BACKEND_PORT}"
( cd backend && exec .venv/bin/python -m uvicorn app.main:app \
    --host "$BIND_HOST" --port "$BACKEND_PORT" --log-level info ) &
BACKEND_PID=$!

backend_up=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then backend_up=1; break; fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    err "backend process died during startup — scroll up for the traceback"
    exit 1
  fi
  sleep 0.5
done
[ "$backend_up" = 1 ] && ok "backend healthy" || warn "backend did not answer /health in 30s; starting UI anyway"

log "starting frontend on http://${BIND_HOST}:${FRONTEND_PORT}"
if [ -n "$TC_TUNNEL" ]; then
  if ! command -v cloudflared >/dev/null 2>&1; then
    err "TC_TUNNEL=1 needs cloudflared, which is not installed."
    err "Install it with:  brew install cloudflared"
  else
    TUNNEL_LOG="$SCRIPT_DIR/logs/tunnel.log"
    TUNNEL_PIDFILE="$SCRIPT_DIR/logs/tunnel.pid"
    TUNNEL_PY="$SCRIPT_DIR/backend/.venv/bin/python3"
    [ -x "$TUNNEL_PY" ] || TUNNEL_PY="$(command -v python3)"
    TOKEN="$(cat "$SCRIPT_DIR/secrets/lan_token.txt" 2>/dev/null || true)"

    # THE TUNNEL OUTLIVES THE APP, ON PURPOSE.
    #
    # A free Cloudflare tunnel gets a random address, and a new one every time
    # cloudflared starts. If it were a child of this script, every code change
    # that needs a backend restart would also hand the operator a new link, a
    # dead home-screen icon, and a re-install. That is not a workflow anyone
    # keeps up with.
    #
    # So it runs detached, survives Ctrl+C here, and is reused on the next start.
    # The address then only changes when the tunnel itself is restarted, which
    # is: `bash run.sh --tunnel-restart`.
    REUSED=""
    if [ -f "$TUNNEL_PIDFILE" ] && kill -0 "$(cat "$TUNNEL_PIDFILE" 2>/dev/null)" 2>/dev/null; then
      REUSED=1
    else
      : > "$TUNNEL_LOG"
      # `nohup … & disown` was NOT enough, and the failure was invisible.
      #
      # disown removes a job from the shell's table; it does not change the
      # process GROUP. Control Deck stops a project with os.killpg on the whole
      # group, so the tunnel died with the app every single time — taking the
      # address with it, which is the one thing this design exists to prevent.
      # The log showed it plainly: "Initiating graceful shutdown due to signal
      # terminated" at the same second the supervisor was signalled.
      #
      # start_new_session=True gives cloudflared its own session and group, out
      # of reach of that killpg. macOS has no setsid, so python does it.
      "$TUNNEL_PY" - "$FRONTEND_PORT" "$TUNNEL_LOG" "$TUNNEL_PIDFILE" <<'PYTUNNEL'
import subprocess, sys
port, log_path, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
with open(log_path, "ab", buffering=0) as fh:
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--no-autoupdate",
         "--url", f"http://127.0.0.1:{port}"],
        stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
with open(pidfile, "w") as fh:
    fh.write(str(proc.pid))
PYTUNNEL
      log "opening a Cloudflare tunnel…"
    fi

    TUNNEL_URL=""
    for _ in $(seq 1 40); do
      TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1 || true)"
      [ -n "$TUNNEL_URL" ] && break
      sleep 0.5
    done

    if [ -n "$TUNNEL_URL" ]; then
      echo
      ok  "phone access is ON${REUSED:+ (same address as before)}"
      log "  On your phone, open:"
      log "      ${TUNNEL_URL}/?token=${TOKEN}"
      log "  Then Share -> Add to Home Screen."
      log "  This address survives app restarts. It only changes if you run:"
      log "      bash run.sh --tunnel-restart"
      echo
    else
      err "the tunnel did not come up; see logs/tunnel.log"
    fi
  fi
fi

if [ -n "$TC_LAN" ]; then
  TOKEN="$(cat "$SCRIPT_DIR/secrets/lan_token.txt" 2>/dev/null || true)"
  echo
  ok  "phone access is ON"
  log "  On your phone, on the same wifi, open:"
  log "      http://${LAN_IP}:${FRONTEND_PORT}/?token=${TOKEN:-<printed-in-the-backend-log-above>}"
  log "  Then use Share -> Add to Home Screen to keep it as an app icon."
  log "  The token is stored by the page, so that link is only typed once."
  echo
fi
start_frontend() {
  cd "$SCRIPT_DIR/frontend"
  # Three routes, in order of preference. The last one matters: node_modules
  # already contains vite, so a broken package manager should never be the
  # reason the dashboard is unavailable.
  if pnpm_works; then
    exec pnpm dev
  elif npm_works; then
    exec npm run dev
  elif [ -x ./node_modules/.bin/vite ]; then
    exec ./node_modules/.bin/vite --port "$FRONTEND_PORT" --strictPort \
         ${TC_LAN:+--host 0.0.0.0}
  else
    echo "[run.sh]: no working pnpm, npm or vite binary" >&2
    exit 127
  fi
}
start_frontend &
FRONTEND_PID=$!

frontend_up=0
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1; then frontend_up=1; break; fi
  kill -0 "$FRONTEND_PID" 2>/dev/null || break
  sleep 0.5
done
if [ "$frontend_up" = 1 ]; then
  ok "$PROJECT_NAME up — dashboard at http://127.0.0.1:${FRONTEND_PORT}"
else
  FRONTEND_PID=""
  warn "frontend did not start — the dashboard is unavailable, but the backend"
  warn "keeps collecting data and trading. npm: $(command -v npm || echo 'NOT FOUND')"
fi

# Wait on the BACKEND specifically. The backend is the process that matters; a
# dead UI is an inconvenience, a dead backend is an outage. Exiting 0 after a
# failure is what made the supervisor give up permanently, so the exit code
# here is always the truth: 0 only when the backend was asked to stop.
backend_code=0
wait "$BACKEND_PID" || backend_code=$?
if [ "$backend_code" = 0 ]; then
  err "backend exited unexpectedly with status 0 — treating as a failure"
  backend_code=1
else
  err "backend exited with $backend_code"
fi
exit "$backend_code"
