#!/usr/bin/env bash
# Build a clean copy of TradeCrypto to hand to someone else.
#
# What it leaves behind, deliberately:
#   data/      every trade, position, signal and price bar this desk has taken
#   logs/      the same story in prose
#   secrets/   the Robinhood token and the LAN token
#   .env       the real one: account equity, ports, token paths
#   __pycache__  compiled files that embed absolute paths like /Users/you<name>/...
#
# The result starts with an empty database, so the person who receives it begins
# on the day they start, with their own numbers and no trace of ours.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/tradecrypto-share}"
# Some destinations cannot be deleted into (a folder shared with an assistant,
# a synced drive). Clear what we can, then let rsync --delete do the rest.
rm -rf "$OUT" 2>/dev/null || true
mkdir -p "$OUT"

# The leading slash matters. `--exclude 'data/'` matches at ANY depth, so it
# silently deleted backend/app/data/ as well and shipped a bundle that could not
# import. Anchor every directory exclude to the bundle root.
rsync -a --delete \
  --exclude '/data/' --exclude '/logs/' --exclude '/secrets/' \
  --exclude '.env' --exclude '.env.local' \
  --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'node_modules/' --exclude '.venv/' --exclude 'venv/' \
  --exclude '.git/' --exclude '.DS_Store' --exclude '*.bak' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude 'htmlcov/' \
  --exclude '*.sqlite' --exclude '*.sqlite-*' \
  "$ROOT/" "$OUT/"

mkdir -p "$OUT/data" "$OUT/logs" "$OUT/secrets"
: > "$OUT/data/.gitkeep"; : > "$OUT/logs/.gitkeep"; : > "$OUT/secrets/.gitkeep"
echo "The database is created empty on first run." > "$OUT/data/README.txt"

# Scrub the owner's name and home path out of comments, test fixtures and docs.
# These are not secrets, but a handoff should not read like someone else's diary.
find "$OUT" -type f \( -name '*.py' -o -name '*.md' -o -name '*.sh' -o -name '*.jsx' \
     -o -name '*.js' \) -print0 | xargs -0 sed -i \
  -e 's#/Users/you[a-zA-Z0-9._-]*/Desktop/wayup/dayup/market/tradecrypto#/path/to/tradecrypto#g' \
  -e 's#/Users/you[a-zA-Z0-9._-]*#/Users/you#g' \
  -e 's/owner-mac/owner-mac/g' \
  -e "s/the operator's/the operator's/g" \
  -e 's/the operator/the operator/g' \
  -e 's/owner/owner/g'

# Prove it before anyone sends it anywhere.
if grep -rliE "owner|/Users/you(?!you)" "$OUT" --exclude=make_share_bundle.sh -P >/dev/null 2>&1; then
  echo "REFUSING: personal strings survived the scrub:" >&2
  grep -rliE "owner|/Users/you(?!you)" "$OUT" --exclude=make_share_bundle.sh -P | head >&2
  exit 1
fi
for bad in data/tradecrypto.sqlite secrets/lan_token.txt secrets/robinhood_mcp_token.json .env; do
  if [ -s "$OUT/$bad" ]; then echo "REFUSING: $bad is in the bundle" >&2; exit 1; fi
done
if [ ! -d "$OUT/backend/app/data" ]; then
  echo "REFUSING: backend/app/data was dropped -- an exclude matched too deep" >&2
  exit 1
fi
MISSING=""
for d in backend/app/data backend/app/core backend/app/strategy backend/app/execution \
         backend/app/research backend/app/risk backend/tests frontend/src scripts; do
  [ -d "$OUT/$d" ] || MISSING="$MISSING $d"
done
if [ -n "$MISSING" ]; then echo "REFUSING: missing source dirs:$MISSING" >&2; exit 1; fi
echo "clean: no personal strings, no database, no secrets, no .env"
echo "complete: every source directory present"

# ZIP IT HERE, inside the script, immediately after the checks.
#
# 2026-09-24: the checks passed, then the bundle was tested by running the app
# inside it, which AUTO-GENERATED a LAN token into secrets/ -- and the zip built
# afterwards carried it. A check that runs before the last thing to touch the
# directory is not a check. So: verify, then zip, in one step, and verify the
# ZIP's own contents rather than the directory's.
# The archive is a convenience, not the deliverable -- the folder is. Some
# destinations refuse new files (a folder shared with an assistant), and that
# must not fail a build whose checks have already passed.
ZIP="${OUT}.zip"
rm -f "$ZIP" 2>/dev/null || true
if ! ( cd "$(dirname "$OUT")" && zip -rq "$ZIP" "$(basename "$OUT")" \
       -x '*/__pycache__/*' '*/.pytest_cache/*' '*/node_modules/*' ) 2>/dev/null; then
  echo "note: could not write $ZIP here (destination is read-only for new files)."
  echo "      The FOLDER is built and verified; that is what gets shared."
  exit 0
fi

BAD="$(unzip -l "$ZIP" | grep -iE "/\.env$|/secrets/[^/]+\.(json|txt)$|\.sqlite|/logs/[^/]+\.log$" || true)"
if [ -n "$BAD" ]; then
  echo "REFUSING: the archive contains files that must not be shared:" >&2
  echo "$BAD" >&2
  rm -f "$ZIP"
  exit 1
fi
echo "archive: $ZIP"
echo "verified: the ARCHIVE itself carries no .env, no secrets, no database, no logs"
