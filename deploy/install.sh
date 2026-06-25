#!/usr/bin/env bash
# One-shot installer for the Family Calendar appliance on Linux Mint / Ubuntu.
# Run from the repo root as the user who will own the wall:  bash deploy/install.sh
set -euo pipefail

APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
echo "Installing Family Calendar from: $APPDIR  (user: $USER_NAME)"

# 1. Python venv + deps
if [ ! -d "$APPDIR/.venv" ]; then
  echo "→ creating virtualenv"
  python3 -m venv "$APPDIR/.venv"
fi
"$APPDIR/.venv/bin/pip" install -q --upgrade pip
"$APPDIR/.venv/bin/pip" install -q -r "$APPDIR/requirements.txt"

# 2. First fetch so the wall has data immediately
echo "→ priming events.json"
"$APPDIR/.venv/bin/python" "$APPDIR/fetcher.py" || echo "  (initial fetch failed — feeds may be empty; that's OK)"
chmod 600 "$APPDIR/data/feeds.json" 2>/dev/null || true

# 3. systemd units (web service + fetch timer), with paths/user filled in
echo "→ installing systemd units"
render() { sed -e "s|__APPDIR__|$APPDIR|g" -e "s|__USER__|$USER_NAME|g" "$1"; }
for unit in familycal-web.service familycal-fetch.service familycal-fetch.timer; do
  render "$APPDIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now familycal-web.service
sudo systemctl enable --now familycal-fetch.timer

# 4. Kiosk autostart (.desktop in the user's autostart dir)
echo "→ installing kiosk autostart"
chmod +x "$APPDIR/deploy/kiosk.sh"
mkdir -p "$HOME/.config/autostart"
render "$APPDIR/deploy/familycal-kiosk.desktop" > "$HOME/.config/autostart/familycal-kiosk.desktop"

echo
echo "Done. The wall is live at http://localhost:8080"
echo "  • web service:  sudo systemctl status familycal-web"
echo "  • fetch timer:  systemctl list-timers familycal-fetch"
echo "  • reboot to test the kiosk autostart, or run: deploy/kiosk.sh"
