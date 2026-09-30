#!/usr/bin/env bash
# Smart Smoke Detector @VERSION@: one-file installer for Raspberry Pi 4 (Raspberry Pi OS 64-bit, desktop).
# Run it once as your normal user (not root):   bash smart-smoke-detector-@VERSION@-installer.sh
# It unpacks the app to ~/smart-smoke-detector, installs everything it needs, makes it start on
# power-up and starts it. Run a newer installer the same way to update (the event log is kept).
# Options: --no-launch  --no-autostart  --no-autologin  --keep-blanking
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi  # started with `sh`
set -euo pipefail

VERSION="@VERSION@"
SHA256="@SHA256@"
DEST="$HOME/smart-smoke-detector"
SELF="$(readlink -f "$0")"

LAUNCH=1
AUTOSTART=1
PAUSE=0
PASS=()
for arg in "$@"; do
  case "$arg" in
    --no-launch) LAUNCH=0 ;;
    --pause-at-end) PAUSE=1 ;;
    --no-autostart) AUTOSTART=0; PASS+=("$arg") ;;
    -h|--help) sed -n '2,6p' "$SELF"; exit 0 ;;
    *) PASS+=("$arg") ;;
  esac
done

has_display() { [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; }

# Double-clicked in the file manager: there is no terminal for sudo's password prompt or for
# progress, so reopen in one.
if [ ! -t 0 ] && has_display && [ "$PAUSE" = 0 ]; then
  for term in x-terminal-emulator lxterminal; do
    if command -v "$term" >/dev/null; then
      exec "$term" -e "$(printf '%q ' bash "$SELF" --pause-at-end "$@")"
    fi
  done
fi

tmp=""
finish() {
  code=$?
  [ -n "$tmp" ] && rm -rf "$tmp"
  if [ "$code" -ne 0 ]; then echo; echo "Installation failed (see the messages above)."; fi
  if [ "$PAUSE" = 1 ]; then read -rp "Press Enter to close this window. " _ || true; fi
}
trap finish EXIT

[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not with sudo." >&2; exit 1; }

echo "==> Smart Smoke Detector $VERSION"

# An older copy that is still running would keep the old code loaded: close it first.
if pgrep -f 'run_pi\.sh' >/dev/null; then
  echo "==> Closing the running app"
  pkill -TERM -f 'run_pi\.sh' || true
  for _ in $(seq 20); do pgrep -f 'run_pi\.sh' >/dev/null || break; sleep 0.5; done
fi

echo "==> Unpacking to $DEST"
tmp=$(mktemp -d)
line=$(awk '/^__PAYLOAD_BELOW__$/ {print NR + 1; exit}' "$SELF")
tail -n +"$line" "$SELF" > "$tmp/payload.tar.gz"
if ! echo "$SHA256  $tmp/payload.tar.gz" | sha256sum -c --status -; then
  echo "This installer file is damaged (incomplete download or copy). Copy it again." >&2
  exit 1
fi
mkdir -p "$DEST"
rm -rf "$DEST/app" "$DEST/wheels"  # drop files removed in this version; .venv is kept and updated
tar xzf "$tmp/payload.tar.gz" -C "$DEST" --strip-components=1

"$DEST/install_pi.sh" "${PASS[@]}"

[ "$LAUNCH" = 1 ] || exit 0
if has_display; then
  echo "==> Starting the app"
  if id -nG | grep -qw bluetooth; then
    setsid -f "$DEST/run_pi.sh" >/dev/null 2>&1
  else
    # Just added to the bluetooth group: this login session doesn't have it yet.
    setsid -f sg bluetooth -c "$(printf '%q' "$DEST/run_pi.sh")" >/dev/null 2>&1
  fi
  echo "    It is starting full screen now, and will start by itself whenever the Pi powers on."
elif [ "$AUTOSTART" = 1 ]; then
  echo "==> Installed. No screen session here (SSH?), so the app starts on the Pi's display after a reboot."
  if [ -t 0 ]; then
    read -rp "Reboot now? [Y/n] " answer || answer=n
    case "$answer" in [nN]*) ;; *) sudo reboot ;; esac
  fi
fi
exit 0
__PAYLOAD_BELOW__
