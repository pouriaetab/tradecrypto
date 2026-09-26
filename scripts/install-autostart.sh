#!/usr/bin/env bash
# Install Glassbox as a macOS LaunchAgent so it starts at login and is
# restarted automatically if it ever dies.
#
#   ./scripts/install-autostart.sh          install and start
#   ./scripts/install-autostart.sh remove   uninstall
#
# A LaunchAgent (not a LaunchDaemon) runs as you, with your PATH and your
# Python — which is what this app needs. KeepAlive means launchd restarts it
# whenever it exits non-zero.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.tradecrypto.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# --awake wraps the desk in `caffeinate -s`, which stops the Mac idle-sleeping
# while it runs. Without it the desk is only "24/7" for as long as the machine
# is awake: a closed lid stops everything, and the loop resumes on wake as if
# nothing happened. It is opt-in because keeping a machine awake is a decision
# about someone's battery, not a detail.
# KEPT ALIVE EXACTLY WHILE data/STOP_SUPERVISOR IS ABSENT.
#
# A plain KeepAlive=true means the desk can never be stopped: Control Deck's Stop
# button is undone within seconds and there is no way to hold it down while you
# work on it. A KeepAlive DICTIONARY is OR'd across its conditions, so PathState
# has to be the ONLY condition or it cannot be authoritative; adding
# SuccessfulExit alongside it would revive the desk after a crash even when paused.
#
# STOP_SUPERVISOR is the file supervise.sh ALREADY refuses to start against. One
# file, two enforcers: launchd will not revive the desk while it exists, and
# Control Deck's Start cannot either. A pause only one of them honours is no pause.
#
# Result: crash -> restarted. Control Deck Stop -> restarted (Stop becomes
# "restart"). STOP_SUPERVISOR present -> stays down until you say otherwise.
#
# This text lives HERE and not in the plist because an XML comment may not
# contain a double hyphen. One appeared in the middle of a sentence, the whole
# plist stopped parsing, and launchctl reported only "Load failed: 5: Input/output
# error" — which reads exactly like a permissions problem and cost a trip to
# System Settings that was not needed.
AWAKE=0
for a in "$@"; do [ "$a" = "--awake" ] && AWAKE=1; done

if [ "$AWAKE" = "1" ]; then
  PROGRAM_ARGS="    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>/bin/bash</string>
    <string>$PROJECT/scripts/supervise.sh</string>"
else
  PROGRAM_ARGS="    <string>/bin/bash</string>
    <string>$PROJECT/scripts/supervise.sh</string>"
fi

if [ "${1:-install}" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$PROGRAM_ARGS
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/Library/pnpm:/Library/Frameworks/Python.framework/Versions/3.11/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <!-- alive only while data/STOP_SUPERVISOR is absent; see notes above -->
  <key>KeepAlive</key>
  <dict>
    <key>PathState</key>
    <dict><key>$PROJECT/data/STOP_SUPERVISOR</key><false/></dict>
  </dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$PROJECT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/logs/launchd.err.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
PLISTEOF

# VALIDATE BEFORE LOADING. launchctl's failure message for a malformed plist is
# "Load failed: 5: Input/output error", which is indistinguishable from a
# permissions problem. plutil says what is actually wrong, on the right line.
if command -v plutil >/dev/null 2>&1; then
  if ! plutil -lint "$PLIST" >/dev/null 2>&1; then
    echo "REFUSING TO INSTALL: the generated plist is not valid:"
    plutil -lint "$PLIST" || true
    echo "  file: $PLIST"
    exit 1
  fi
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/tmp/tc-bootstrap.err; then
  if ! launchctl load "$PLIST" 2>>/tmp/tc-bootstrap.err; then
    echo "LOAD FAILED — the agent is written but not running:"
    sed 's/^/    /' /tmp/tc-bootstrap.err
    echo "    file: $PLIST"
    exit 1
  fi
fi
# DID IT ACTUALLY RUN? "installed" is not the same as "working".
#
# 2026-09-18: the agent loaded cleanly and then failed every 10 seconds with
#   /bin/bash: .../scripts/supervise.sh: Operation not permitted
# because the project lives under ~/Desktop, which macOS protects from
# background jobs. The installer printed a cheerful success message and a
# generic hint, and the only way to find out was to read launchd's own log.
# So: clear the log, give it a few seconds, and look.
: > "$PROJECT/logs/launchd.err.log" 2>/dev/null || true
sleep 4
if grep -q "Operation not permitted" "$PROJECT/logs/launchd.err.log" 2>/dev/null; then
  echo
  echo "  !! THE AGENT IS INSTALLED BUT CANNOT RUN."
  echo
  echo "  macOS is blocking it from reading the project:"
  sed 's/^/      /' "$PROJECT/logs/launchd.err.log" | head -2
  echo
  echo "  This is because the project sits under a protected folder:"
  echo "      $PROJECT"
  echo
  echo "  TWO WAYS OUT — the first is the safer one:"
  echo
  echo "   1. MOVE the project somewhere unprotected, e.g. ~/apps/tradecrypto,"
  echo "      then re-run this installer. Nothing in the app depends on its path."
  echo
  echo "   2. Grant /bin/bash Full Disk Access:"
  echo "      System Settings > Privacy & Security > Full Disk Access > '+'"
  echo "      then Cmd-Shift-G, type /bin/bash, add it, switch it on."
  echo "      Note this gives EVERY bash script on this Mac full disk access."
  echo
  echo "  Until one of those is done the agent retries every 10s and does nothing."
  echo "  To stop the retrying meanwhile:  ./scripts/install-autostart.sh remove"
  exit 1
fi
echo "  verified: the agent started without a permissions error"
echo "installed $LABEL"
echo
echo
echo "  The desk now runs at login and restarts itself if it dies or is stopped."
echo
echo "  hard stop  :  ./scripts/desk.sh off      (stays down until you say on)"
echo "  start      :  ./scripts/desk.sh on"
echo "  state      :  ./scripts/desk.sh status"
echo "  restart    :  Control Deck Stop — launchd brings it back within ~10s"
echo "  logs       :  tail -f $PROJECT/logs/tradecrypto.log"
echo "  uninstall  :  ./scripts/install-autostart.sh remove"
if [ "$AWAKE" = "1" ]; then
  echo
  echo "  The Mac will not idle-sleep while the desk runs (caffeinate -s)."
  echo "  Closing a laptop lid still sleeps it; that is macOS, not this."
else
  echo
  echo "  NOTE: the desk stops while the Mac sleeps. For literal 24/7 run:"
  echo "        ./scripts/install-autostart.sh --awake"
fi
echo
echo "It now starts at login and restarts itself if it dies."
echo "macOS still sleeps on battery — 'caffeinate -dimsu' keeps a session awake."

# macOS privacy protection (TCC) covers ~/Desktop, ~/Documents and ~/Downloads.
# A LaunchAgent is not started by Terminal, so it does not inherit Terminal's
# access to them: /bin/bash gets EPERM on files you can read perfectly well
# yourself. The installer cannot fix this — only you can — so say so loudly.
case "$PROJECT" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
    echo
    echo "  !! ONE MORE STEP — this will NOT run at login until you do it."
    echo
    echo "  This project lives under a folder macOS protects:"
    echo "      $PROJECT"
    echo "  A background job cannot read it unless /bin/bash has Full Disk Access."
    echo
    echo "  System Settings > Privacy & Security > Full Disk Access > '+'"
    echo "  then Cmd-Shift-G, type  /bin/bash , add it, switch it on."
    echo
    echo "  Or move the project somewhere unprotected (e.g. ~/apps/tradecrypto)"
    echo "  and re-run this installer — nothing in the app depends on the path."
    echo
    ;;
esac
