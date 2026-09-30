# Smart-Smoke-Detector

Lightweight PySide6 desktop app for a smoke detector, built to run on a **Raspberry Pi 4 (32GB SD card)**.
The ESP32 (`firmware/esp32/esp32.ino`) runs all sensors, actuators and safety logic; the Pi app
displays its telemetry over BLE.

## Development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app                    # connects to "SmokeGuard-ESP32" over BLE
python -m app --demo             # simulated telemetry, no ESP32 needed
```

## Raspberry Pi install

Needs Raspberry Pi OS **64-bit with desktop** (Bookworm or Trixie). The Pi's onboard Bluetooth is used.

1. Build the one-file installer on any machine: `python tools/package.py`
   (add `--offline` to bundle the ~90 MB of aarch64 wheels when the Pi has no internet).
2. Copy `dist/smart-smoke-detector-<version>-installer.sh` to the Pi and run it once, as the desktop user:

   ```bash
   bash smart-smoke-detector-0.1.0-installer.sh
   ```

It unpacks the app to `~/smart-smoke-detector`, adds the few system libraries Qt needs, enables
Bluetooth, creates `.venv` from prebuilt wheels only, verifies the install, turns on desktop
auto-login and autostart (so the app comes back after a power cut), turns off screen blanking,
and starts the app. Opt out with `--no-autostart`, `--no-autologin`, `--keep-blanking`, `--no-launch`.
Run a newer installer the same way to update; the event log is kept.

`~/smart-smoke-detector/run_pi.sh` starts the app by hand. It runs full screen, allows a single
instance and restarts the app if it crashes.
The Pi's case fan is the ESP32's exhaust fan and is off in clean air, so give the Pi 4 heatsinks;
`vcgencmd get_throttled` should stay `0x0`.

## Data

The event & alarm log is a SQLite file created automatically on first run (no database setup):
`~/.local/share/smart-smoke-detector/events.db` on the Pi, `%LOCALAPPDATA%\smart-smoke-detector\events.db` on Windows.
CSV exports go to a plugged-in USB stick, otherwise to the `exports` folder next to the log.
The app's own log is kept in RAM (`$XDG_RUNTIME_DIR/smart-smoke-detector.log`).

See [CLAUDE.md](CLAUDE.md) for performance guidelines.
