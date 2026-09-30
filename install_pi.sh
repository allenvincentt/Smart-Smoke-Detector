#!/usr/bin/env bash
# Smart Smoke Detector installer for Raspberry Pi 4 (Raspberry Pi OS Bookworm or Trixie, 64-bit,
# with desktop). Run it from the unpacked package as the user who is logged in on the display:
#     ./install_pi.sh                   install, auto-login + start on power-up, keep the screen on
#     ./install_pi.sh --no-autostart    don't start the app at login
#     ./install_pi.sh --no-autologin    leave the desktop login setting alone
#     ./install_pi.sh --keep-blanking   leave the desktop's screen blanking setting alone
# Uses sudo for apt and Bluetooth setup. Safe to run again (e.g. after copying in a new version).
# The one-file installer (tools/package.py) unpacks the app and runs this for you.
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

AUTOSTART=1
AUTOLOGIN=1
BLANKING=1
for arg in "$@"; do
  case "$arg" in
    --no-autostart) AUTOSTART=0 ;;
    --no-autologin) AUTOLOGIN=0 ;;
    --keep-blanking) BLANKING=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
NOTES=()

# ---- preflight
[ "$(id -u)" -ne 0 ] || die "run as the desktop user, not root (sudo is used where needed)"
[ "$(uname -m)" = aarch64 ] || die "needs 64-bit Raspberry Pi OS (aarch64), found $(uname -m)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10+ required"
glibc=$(getconf GNU_LIBC_VERSION | awk '{print $2}')
python3 - "$glibc" <<'EOF' || die "glibc $glibc is too old: the PySide6 wheel needs 2.31+ (use Raspberry Pi OS Bookworm or newer)"
import sys
sys.exit(tuple(map(int, sys.argv[1].split(".")[:2])) < (2, 31))
EOF
avail_mb=$(( $(df --output=avail -k . | tail -1) / 1024 ))
[ "$avail_mb" -ge 700 ] || die "only ${avail_mb} MB free here; the install needs about 700 MB"
grep -q "Raspberry Pi 4" /proc/device-tree/model 2>/dev/null ||
  NOTES+=("This is not a Raspberry Pi 4 ($(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)); the app is tuned for one.")

# ---- system packages (Qt's xcb plugin needs libxcb-cursor0 since Qt 6.5; most others ship with the desktop)
say "Installing system packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3-venv bluez rfkill \
  libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-shape0 libxcb-xkb1 libxcb-randr0 libxcb-xfixes0 libegl1 libgl1 libfontconfig1 libdbus-1-3 \
  fonts-dejavu-core
sudo apt-get clean   # the .deb cache is dead weight on a 32 GB card

# ---- Bluetooth (bleak talks to BlueZ over D-Bus)
say "Enabling Bluetooth"
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth || true
if ! id -nG "$USER" | grep -qw bluetooth; then
  sudo usermod -aG bluetooth "$USER"
  NOTES+=("Added $USER to the 'bluetooth' group (applies from the next login; the one-file installer handles the first start).")
fi

# ---- Python environment. Wheels only: nothing may compile on the Pi. Bundled wheels (offline
# package, see tools/package.py) are used when present.
say "Creating the Python environment (.venv)"
python3 -m venv .venv
PIP=(.venv/bin/python -m pip install --no-cache-dir --only-binary=:all: --disable-pip-version-check)
if [ -d wheels ]; then
  "${PIP[@]}" --no-index --find-links wheels -r requirements.txt
else
  "${PIP[@]}" -r requirements.txt
fi
# Precompile once so the app never writes bytecode to the SD card at startup.
.venv/bin/python -m compileall -q app

# ---- verify: Qt libraries resolve and the whole app imports
say "Checking the install"
qt_dir=$(.venv/bin/python -c 'import os, PySide6; print(os.path.dirname(PySide6.__file__))')
missing=$(ldd "$qt_dir/Qt/plugins/platforms/libqxcb.so" 2>/dev/null | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')
[ -z "$missing" ] || die "Qt's xcb plugin is missing system libraries: $missing(install them with apt)"
QT_QPA_PLATFORM=offscreen SSD_DATA_DIR="$(mktemp -d)" .venv/bin/python - <<'EOF'
import PySide6, bleak
from PySide6.QtWidgets import QApplication
qapp = QApplication([])
import app.ui.main_window, app.ble_link, app.event_log  # noqa: F401  (imports every module the shell loads)
for page in ("dashboard_page", "zone_map_page", "control_panel_page", "logs_page"):
    __import__(f"app.ui.pages.{page}")
print(f"    PySide6 {PySide6.__version__}, bleak OK")
EOF
chmod +x run_pi.sh

# ---- start at login (XDG autostart works on the Wayland (labwc/wayfire) and X11 desktops)
entry="[Desktop Entry]
Type=Application
Name=Smart Smoke Detector
Comment=Smoke detector dashboard (ESP32 over BLE)
Exec=\"$APP_DIR/run_pi.sh\"
Terminal=false
Categories=Utility;"
mkdir -p ~/.local/share/applications
printf '%s\n' "$entry" > ~/.local/share/applications/smart-smoke-detector.desktop
if [ "$AUTOSTART" -eq 1 ]; then
  say "Starting the app at login"
  mkdir -p ~/.config/autostart
  printf '%s\n' "$entry" > ~/.config/autostart/smart-smoke-detector.desktop
  # A monitoring display must come back by itself after a power cut: boot straight to the desktop.
  if [ "$AUTOLOGIN" -eq 1 ] && command -v raspi-config >/dev/null; then
    say "Enabling desktop auto-login (starts the app on power-up)"
    sudo raspi-config nonint do_boot_behaviour B4 ||
      NOTES+=("Could not enable auto-login; set it in raspi-config > System Options > Boot / Auto Login.")
  fi
else
  rm -f ~/.config/autostart/smart-smoke-detector.desktop
fi

# ---- a monitoring display must not go dark
if [ "$BLANKING" -eq 1 ] && command -v raspi-config >/dev/null; then
  say "Disabling screen blanking"
  sudo raspi-config nonint do_blanking 1 || NOTES+=("Could not disable screen blanking; do it in raspi-config > Display.")
fi

say "Done. Installed in $APP_DIR ($(du -sh .venv | cut -f1) Python environment)"
echo "    Run now:        $APP_DIR/run_pi.sh           (demo without an ESP32: add --demo)"
echo "    Event log:      ~/.local/share/smart-smoke-detector/events.db"
echo "    App log (RAM):  \$XDG_RUNTIME_DIR/smart-smoke-detector.log"
echo "    Thermal check:  vcgencmd measure_temp; vcgencmd get_throttled   (0x0 = never throttled)"
for n in "${NOTES[@]}"; do echo "NOTE: $n"; done
