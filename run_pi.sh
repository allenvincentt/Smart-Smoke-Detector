#!/usr/bin/env bash
# Launcher for Raspberry Pi 4 (the autostart entry runs this too). From the app dir: ./run_pi.sh [--demo]
# Restarts the app if it crashes; closing the window normally ends the script.
set -uo pipefail
cd "$(dirname "$0")"

# One instance only: two copies would fight over the ESP32's single BLE connection and the event log.
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/smart-smoke-detector.lock"
if ! flock -n 9; then
  echo "Smart Smoke Detector is already running." >&2
  exit 0
fi

# Lightweight defaults. Set QT_QPA_PLATFORM=eglfs to run without a desktop session, or wayland to
# skip XWayland on the Wayland desktop. The app log goes to $XDG_RUNTIME_DIR (RAM, see app/config.py).
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export QT_LOGGING_RULES="*.debug=false"
export SSD_FULLSCREEN="${SSD_FULLSCREEN:-1}"

PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python

release_esp32() {
  # After a crash BlueZ can stay connected to the ESP32, which then stops advertising, so the next
  # scan would never find it. Drop any such leftover connection before (re)starting the app.
  command -v bluetoothctl >/dev/null || return 0
  timeout 5 bluetoothctl devices 2>/dev/null | awk '/SmokeGuard-ESP32/ {print $2}' |
    while read -r addr; do timeout 5 bluetoothctl disconnect "$addr" >/dev/null 2>&1 || true; done
}

pid=
trap '[ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; exit 0' TERM INT HUP

delay=2
while true; do
  release_esp32
  start=$(date +%s)
  "$PY" -m app "$@" &
  pid=$!
  wait "$pid"
  code=$?
  pid=
  [ "$code" -eq 0 ] && exit 0
  echo "$(date '+%F %T') app exited with code $code, restarting in ${delay}s" >&2
  sleep "$delay"
  # Back off while it keeps failing straight away; reset after a run that lasted a while.
  if [ $(( $(date +%s) - start )) -gt 60 ]; then delay=2; else delay=$(( delay < 30 ? delay * 2 : 30 )); fi
done
