# Family Calendar — Kiosk / Appliance Setup Spec

**Purpose:** Turn the Linux Mint box into an appliance that boots straight into the fullscreen calendar, never sleeps, hides the desktop, and recovers from crashes — so it behaves like a Skylight, not a PC. Build brief for Claude Code.

---

## 1. Goal

Power on → fullscreen calendar, no login screen, no desktop, no cursor clutter, screen always on, auto-recovers if anything crashes, and remotely fixable without a keyboard at the wall.

Environment: HP AIO 24-f0xx, Linux Mint 22.3 **XFCE** (display manager: **LightDM**; Mint's screen locker: **light-locker**). Browser: **Firefox** in `--kiosk` (chosen earlier; the voice service owns the mic via Python, so the browser doesn't need speech APIs).

---

## 2. Components

### 2a. Auto-login (no password at boot)
- LightDM autologin in `/etc/lightdm/lightdm.conf`: set `autologin-user=<user>` and the autologin session. (May already be set if "Log in automatically" was checked at install.)

### 2b. Auto-launch the app fullscreen
- XFCE autostart entry (`~/.config/autostart/familycal.desktop`) runs a launch script on session start.
- Launch script:
  1. Wait for the Flask server to answer on `http://localhost:8080` (curl-poll loop with timeout) — don't launch the browser before the app is up.
  2. Disable screen blanking (§2c).
  3. Launch `firefox --kiosk http://localhost:8080`.
- **Crash recovery:** wrap the browser launch in a `while true; do …; sleep 2; done` loop (or a `systemd --user` service with `Restart=always`) so a crash relaunches instantly.

### 2c. Never sleep / blank / lock the screen
- In the launch script: `xset s off`, `xset s noblank`, `xset -dpms`.
- **Disable light-locker** (it can blank/lock a kiosk): remove from autostart / `light-locker --no-late-locking` disabled.
- XFCE Power Manager: display sleep = Never, DPMS off, on AC.
- Mask system sleep so nothing suspends it: `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
- **Important interaction:** the OS must NOT blank the screen, because the app's own idle **sleep-mode slideshow** is the intended "screensaver." Let the app handle idle visuals; keep the OS display always on.

### 2d. Hide cursor when idle
- Install `unclutter` (or `unclutter-xfixes`); start it in the launch script so the mouse pointer disappears after a couple seconds of no movement.

### 2e. On-screen keyboard for touch input
- `onboard` installed and set to auto-show on text fields (for the rare typed entry; most input is voice or phone). Confirms the single-touch limitation is covered — the app already does touch drag-scroll.

### 2f. Quiet the desktop
- Disable update-manager popups / notification nags that could appear over the calendar. (Updates still install on your schedule; just not interrupting the wall.)

---

## 3. Remote management (so you never need a keyboard at the wall)
- **Enable SSH** (`openssh-server`) for headless fixes/updates.
- Optional **VNC** (e.g., x11vnc) or Tailscale (if added for the export feature) for remote screen access.
- A **maintenance escape**: a key combo or a hidden corner tap to quit kiosk to the desktop for hands-on work.

---

## 4. Optional niceties
- **Nightly reboot** (cron, e.g., 4 AM) to keep the box fresh — common for always-on kiosks. Optional.
- **Night dimming:** lower screen brightness on a schedule (xrandr `--brightness` or backlight) so it's not glaring at 2 AM — nice for a kitchen/counter. Can tie to the app's sleep mode.
- **Boot splash:** hide the Linux boot text with a simple Plymouth theme for a cleaner "appliance" power-on.

---

## 5. How this fits the other services
The app's backend services are system `systemd` units (from the other specs): `familycal-web.service` (Flask), `familycal-fetch.timer` (import), the voice service, Ollama. **This spec wires the display/session side** (autologin + kiosk browser + always-on screen). Ordering: the kiosk launch script waits for `familycal-web` before opening the browser.

---

## 6. Build order
1. Autologin + autostart launching Firefox kiosk at `localhost:8080`.
2. Screen-always-on (xset/DPMS, disable light-locker, mask sleep) — verify it never blanks overnight.
3. Crash-relaunch loop + wait-for-server.
4. unclutter + onboard.
5. SSH + maintenance escape.
6. Optional: nightly reboot, night dimming, boot splash.

---

## 7. Things Benji decides
- Nightly reboot: yes/no (and time).
- Night dimming: yes/no (and schedule).
- Remote access: SSH only, or also VNC/Tailscale.
