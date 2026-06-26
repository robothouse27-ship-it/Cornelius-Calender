# Deploying to the Mint box

Goal: the HP All-in-One boots straight into the fullscreen family calendar,
syncs every 10 minutes, and stays up 24/7. One script does almost all of it.

## 0. Prereqs (on the box)
Linux Mint 22.3 / Ubuntu 24.04, with Python 3 and Chromium:
```bash
sudo apt update
sudo apt install -y python3 python3-venv git chromium  # or chromium-browser
```

## 1. Get the code onto the box
**Either** clone it (if you've pushed the repo somewhere):
```bash
cd ~ && git clone <your-repo-url> familycal && cd familycal
```
**or** copy the folder over (USB stick / scp) — but **don't copy `.venv/`**
(it's Mac-specific; the installer rebuilds it). `git clone` already excludes it.

## 2. Run the installer
```bash
bash deploy/install.sh
```
It will:
- build the Python venv + install deps, create `photos/`
- prime `data/events.json` from `data/feeds.json`
- install + enable the **web service** (`familycal-web`) and the **10-min
  fetch timer** (`familycal-fetch`)
- install the **Chromium kiosk autostart** for your user

`sudo` is used only for the systemd unit installs; it'll prompt once.

## 3. Verify
```bash
systemctl status familycal-web          # active (running)
systemctl list-timers familycal-fetch   # next run shown
curl -s localhost:8080/events.json | head -c 200
```
Open `http://localhost:8080` in a browser on the box. Then **reboot** — it
should come up fullscreen on its own.

## 4. Daily use
- **Add a calendar:** on the wall, 🎨 → 📲 Add a calendar → scan with a phone.
- **Photos:** drop images into `~/familycal/photos/` — they show in sleep mode.
- **Weather:** auto-detected by IP. To pin it, edit
  `/etc/systemd/system/familycal-web.service` and add
  `Environment=FAMILYCAL_LAT=33.78` / `FAMILYCAL_LON=-117.23`, then
  `sudo systemctl daemon-reload && sudo systemctl restart familycal-web`.

## Troubleshooting
- **Blank/again screen on boot:** the kiosk waits for the web service; check
  `systemctl status familycal-web` and `journalctl -u familycal-web -e`.
- **Calendar empty:** `~/familycal/.venv/bin/python fetcher.py` and read the log.
- **Screen keeps blanking:** `kiosk.sh` already runs `xset s off -dpms`; if your
  desktop overrides it, also disable the XFCE screensaver/power blanking in
  Settings.
- **Phone can't reach `/add`:** the box and phone must be on the same Wi-Fi;
  the QR encodes the box's LAN IP (detected at runtime).
