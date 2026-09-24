#!/usr/bin/env bash
# Push the vault to a private git remote, so one dead disk is not the end of it.
#
# The vault (core/vault.py) already survives the database, the app and the
# project folder. It does not survive this Mac, and that is the one failure
# nothing local can cover -- so a second copy goes off the machine.
#
# Rules this script keeps:
#   * append only. It commits and pushes. It never rewrites history, never
#     force-pushes, and never deletes. `git push --force` is the one command
#     that could undo the whole point of a write-once store.
#   * loud when it cannot work. A mirror that silently does nothing is worse
#     than no mirror, because you believe you have one.
#   * it is not required for the vault to be correct. If this never runs, every
#     record is still in the local vault, fsynced and hash-chained.
#
# Usage:  bash scripts/vault_mirror.sh [--local] [--dir <vault>] [--quiet]
#         --local  version it here and push nowhere (TC_VAULT_LOCAL_ONLY=1)
set -uo pipefail

VAULT="${TC_VAULT_DIR:-$HOME/tradecrypto-vault}"
QUIET=0
LOCAL_ONLY=${TC_VAULT_LOCAL_ONLY:-0}
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) VAULT="$2"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    --local) LOCAL_ONLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
say() { [ "$QUIET" = 1 ] || echo "$@"; }

if [ ! -d "$VAULT/records" ]; then
  echo "vault-mirror: no vault at $VAULT — nothing to mirror yet." >&2
  echo "  The app creates it on its next order, trade or reconcile pass." >&2
  exit 1
fi

command -v git >/dev/null 2>&1 || { echo "vault-mirror: git is not installed" >&2; exit 1; }
cd "$VAULT" || exit 1

if [ ! -d .git ]; then
  say "vault-mirror: initialising a repository in $VAULT"
  git init -q
  git symbolic-ref HEAD refs/heads/main 2>/dev/null || true
  # Nothing is ignored. The whole point is that all of it is preserved.
  printf '# the vault is mirrored whole — nothing here is ignored\n' > .gitignore
fi

# --local: version the vault on THIS machine and stop there. Nothing leaves the
# Mac, so nothing about the trades can leak, and you still get the one thing a
# remote was for -- every version of every day file recoverable after a bad
# write, a bad edit, or a mistake by me. It is not off-machine backup: a dead
# disk still takes it. Say so rather than letting it feel like one.
if [ "$LOCAL_ONLY" = 1 ]; then
  git add -A -- . >/dev/null 2>&1
  if git diff --cached --quiet 2>/dev/null; then
    say "vault-mirror: nothing new to commit (local history is current)"
  else
    N=$(git diff --cached --name-only | wc -l | tr -d ' ')
    git -c user.name="tradecrypto" -c user.email="vault@localhost" \
        commit -q -m "vault $(date -u '+%Y-%m-%dT%H:%M:%SZ') — $N file(s)" || {
          echo "vault-mirror: commit failed" >&2; exit 1; }
    say "vault-mirror: committed $N file(s) to the local history in $VAULT"
  fi
  say "vault-mirror: local only — nothing was pushed anywhere."
  say "              This protects against bad writes and edits, NOT against"
  say "              this disk dying. Add a remote when you want that."
  exit 0
fi

# Commit BEFORE asking about the remote. Until 2026-09-19 a vault with no
# remote exited here without committing, so the hourly job versioned nothing
# for as long as the remote was missing -- the local history the operator
# thought he had was one hand-run commit. Local history costs nothing and is
# the part that survives a bad write; it must not wait on GitHub.
git add -A -- . >/dev/null 2>&1
if ! git diff --cached --quiet 2>/dev/null; then
  N=$(git diff --cached --name-only | wc -l | tr -d ' ')
  git -c user.name="tradecrypto" -c user.email="vault@localhost" \
      commit -q -m "vault $(date -u '+%Y-%m-%dT%H:%M:%SZ') — $N file(s)" || {
        echo "vault-mirror: commit failed" >&2; exit 1; }
  say "vault-mirror: committed $N file(s) to the local history"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  cat >&2 <<'MSG'
vault-mirror: no remote is configured, so nothing left this machine.

  The local vault is fine and complete — this step is only the off-machine copy.
  To set it up once, from a terminal:

      gh repo create tradecrypto-vault --private --source ~/tradecrypto-vault --push

  or, without the gh CLI, create an empty PRIVATE repo on GitHub and then:

      cd ~/tradecrypto-vault
      git remote add origin git@github.com:<you>/tradecrypto-vault.git
      git push -u origin main

  Make it PRIVATE. It contains every order and trade this desk has made.
MSG
  exit 3
fi

# Never --force. If this is rejected, the remote has something we do not, and a
# human should look rather than a script deciding whose history wins.
if OUT=$(git push origin HEAD 2>&1); then
  say "vault-mirror: pushed to $(git remote get-url origin)"
else
  echo "vault-mirror: push failed" >&2
  echo "$OUT" >&2
  exit 1
fi
