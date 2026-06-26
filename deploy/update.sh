#!/usr/bin/env bash
# Auto-update the wall from GitHub. Run by familycal-update.timer (as root).
# Fast-forward only, never clobber the wall's local data, and restart the web
# service only when server-side code actually changed. The browser reloads
# itself via /api/version, so HTML/JS changes need no restart at all.
set -uo pipefail

APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c '%U' "$APPDIR")"            # the user who owns (and cloned) the repo
asowner() { sudo -u "$OWNER" -H git -C "$APPDIR" "$@"; }

# A detached/branchless checkout can't be pulled — bail quietly.
asowner rev-parse --abbrev-ref HEAD | grep -qx main || { echo "not on main; skipping"; exit 0; }

before="$(asowner rev-parse HEAD)"
asowner fetch --quiet origin || { echo "fetch failed (offline?)"; exit 0; }
# ff-only keeps it safe: if the wall ever has divergent local commits we stop
# rather than risk a messy merge.
asowner merge --ff-only --quiet origin/main || { echo "no fast-forward; skipping"; exit 0; }
after="$(asowner rev-parse HEAD)"

[ "$before" = "$after" ] && exit 0           # already up to date

echo "updated $before -> $after"
changed="$(asowner diff --name-only "$before" "$after")"

# Python deps changed → reinstall into the existing venv.
if grep -q '^requirements.txt$' <<<"$changed"; then
  echo "→ requirements changed; updating venv"
  sudo -u "$OWNER" -H "$APPDIR/.venv/bin/pip" install -q -r "$APPDIR/requirements.txt" || true
fi

# Server-side code changed → restart the Flask service (the kiosk auto-reloads).
if grep -qE '\.py$' <<<"$changed"; then
  echo "→ python changed; restarting familycal-web"
  systemctl restart familycal-web || true
fi

echo "update complete"
