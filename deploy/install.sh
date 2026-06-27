#!/usr/bin/env bash
# One-shot installer for the Family Calendar appliance on Linux Mint / Ubuntu.
# Run from the repo root as the user who will own the wall:  bash deploy/install.sh
set -euo pipefail

APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
echo "Installing Family Calendar from: $APPDIR  (user: $USER_NAME)"

# 1. Python venv + deps (rebuild if missing or copied from another OS)
if [ ! -x "$APPDIR/.venv/bin/python" ] || ! "$APPDIR/.venv/bin/python" -c '' 2>/dev/null; then
  echo "→ creating virtualenv"
  rm -rf "$APPDIR/.venv"
  python3 -m venv "$APPDIR/.venv"
fi
"$APPDIR/.venv/bin/pip" install -q --upgrade pip
"$APPDIR/.venv/bin/pip" install -q -r "$APPDIR/requirements.txt"
mkdir -p "$APPDIR/photos"   # sleep-mode photo frame

# 2. First fetch so the wall has data immediately
echo "→ priming events.json"
"$APPDIR/.venv/bin/python" "$APPDIR/fetcher.py" || echo "  (initial fetch failed — feeds may be empty; that's OK)"
chmod 600 "$APPDIR/data/feeds.json" 2>/dev/null || true

# 3. systemd units (web + fetch + self-update), with paths/user filled in
echo "→ installing systemd units"
render() { sed -e "s|__APPDIR__|$APPDIR|g" -e "s|__USER__|$USER_NAME|g" "$1"; }
for unit in familycal-web.service familycal-fetch.service familycal-fetch.timer \
            familycal-update.service familycal-update.timer \
            familycal-voice.service; do
  render "$APPDIR/deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now familycal-web.service
sudo systemctl enable --now familycal-fetch.timer

# Keep auto-update from ever fighting the wall's own data: feeds.json is
# tracked (for the initial seed) but the wall rewrites it when you add a
# calendar — tell git to leave the local copy alone so pulls never conflict.
chmod +x "$APPDIR/deploy/update.sh"
git -C "$APPDIR" update-index --skip-worktree data/feeds.json 2>/dev/null || true
sudo systemctl enable --now familycal-update.timer

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
echo
echo "Optional add-ons (docs-grocery-voice-plan.md):"
echo "  • Photo list reading uses Claude vision — add your key, then restart web:"
echo "      sudo systemctl edit familycal-web   # add: Environment=ANTHROPIC_API_KEY=sk-ant-..."
echo "      sudo systemctl restart familycal-web"
echo "    Free on-box OCR fallback (no key needed):  sudo apt install -y tesseract-ocr"
echo "  • Local voice control needs a mic + audio libs, then enable its service:"
echo "      sudo apt install -y libportaudio2"
echo "      sudo systemctl edit familycal-voice  # add the same ANTHROPIC_API_KEY line"
echo "      sudo systemctl enable --now familycal-voice"
