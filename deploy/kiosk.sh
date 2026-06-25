#!/usr/bin/env bash
# Launch a fullscreen Chromium kiosk pointed at the local family calendar.
# Disables screen blanking so the wall stays on (sleep-mode handles dimming).
set -e

URL="http://localhost:8080"

# keep the display awake (XFCE / X11)
xset s off || true
xset -dpms || true
xset s noblank || true

# wait for the web service to answer before opening the browser
for i in $(seq 1 30); do
  if curl -fs "$URL" >/dev/null 2>&1; then break; fi
  sleep 1
done

# prefer chromium, fall back to chromium-browser or google-chrome
BROWSER="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
if [ -z "$BROWSER" ]; then
  echo "No chromium/chrome found. Install with: sudo apt install chromium" >&2
  exit 1
fi

exec "$BROWSER" \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --incognito \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  "$URL"
