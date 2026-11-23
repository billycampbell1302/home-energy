# Home Energy

A heating and energy assistant for the flat. It runs on a free Oracle Cloud server and is controlled from a web app you add to your iPhone home screen.

## Phase 1 (this version): watch and record
- Connects to **Hive**, using a one-off SMS code; after that the server stays logged in as a remembered device.
- Connects to **Smart Life** (the kids' lights and any Smart Life plugs) by scanning a QR code with the Smart Life app. No Tuya developer account is needed.
- Records inside and outside temperature, the target, whether the boiler is firing, who's home and the kids' lights **every 5 minutes**.
- On the dashboard:
  - **Heating:** boost, change the target, switch between schedule, manual and off, and boost the hot water
  - **Lights:** on, off, night red and white
  - **Presence:** the people at home, and a "kids/guests home" hold
- **History tab:** temperature chart, boiler hours and an activity log.
- **iPhone Shortcuts:** "Arrive home" and "Leave home" automations report when each person comes and goes.

Nothing is automatic yet. Phase 1 builds up the data that the learning in phases 3 and 4 will use.

## Roadmap
1. ✅ Foundations: connections, logging and the dashboard
2. ✅ Smart meter data from Bright, Economy 7 vs single-rate check, typical day, always-on use
3. Smart heating v1: lower the temperature when away and at night, falling back to the normal Hive schedule
4. Learning: how fast the flat warms up, predicted arrivals and pre-heating
5. Smart Life plugs for standby cut-offs, bedtime and wake-up light routines, and a monthly savings report

## How it's deployed
- `deploy/cloud-init.sh` is pasted into Oracle when the server is created. It downloads and runs `deploy/install.sh`.
- `install.sh` sets up:
  - Python, the app and a systemd service
  - Caddy, for automatic HTTPS on `<ip>.sslip.io`
  - firewall rules
  - an auto-update timer
- The server checks this repo every 15 minutes and updates itself (`deploy/update.sh`).
- Secrets are never stored in this repo:
  - The dashboard password is in `/etc/home-energy.env` on the server.
  - The Hive and Smart Life credentials are in `/var/lib/home-energy/secrets.json` (readable by the app only).

The Smart Life login uses Tuya's official device-sharing SDK with Home Assistant's public app registration. This is the same QR login that the Home Assistant Tuya integration uses.
