"""Tunables. Defaults are chosen for a Raspberry Pi 4 with a 32GB SD card."""

import os
import sys
import tempfile


def _data_dir() -> str:
    """Persistent per-machine app data (event log, CSV exports) — never inside the repo."""
    if os.environ.get("SSD_DATA_DIR"):
        return os.environ["SSD_DATA_DIR"]
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, "smart-smoke-detector")


# Logging: WARNING by default to reduce SD card writes; small rotating file in RAM. /tmp is NOT
# tmpfs on Raspberry Pi OS Bookworm, so prefer the per-user runtime dir (/run/user/<uid>, tmpfs).
LOG_LEVEL = os.environ.get("SSD_LOG_LEVEL", "WARNING")
LOG_DIR = os.environ.get("SSD_LOG_DIR") or os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 1

# Run full screen (set by run_pi.sh on the Pi).
FULLSCREEN = os.environ.get("SSD_FULLSCREEN", "0") == "1"

# BLE link to the ESP32.
BLE_SCAN_S = 8.0     # scan window per attempt
BLE_RETRY_S = 3.0    # pause between attempts
STALE_AFTER_S = 3.0  # no packet for this long while linked = stale (firmware sends at 2 Hz)
STALE_DROP_S = 15.0  # still no packet: drop the connection and rescan

# Dashboard history: 600 samples at 2 Hz = last 5 minutes (bounded, see CLAUDE.md).
HISTORY_SIZE = 600

# Zone map: the room the ESP32 detector is installed in (must match a name in zone_map_page.ROOMS).
SENSOR_ROOM = os.environ.get("SSD_SENSOR_ROOM", "KITCHEN")

# Event & alarm log (SQLite). On the SD card, not /tmp: it must survive reboots.
DATA_DIR = _data_dir()
LOG_RETENTION_DAYS = 30
LOG_FLUSH_S = 30     # routine rows are committed in batches; breaches/interventions immediately

# Demo mode: replay firmware-format telemetry without an ESP32 (SSD_DEMO=1 or --demo).
DEMO = os.environ.get("SSD_DEMO", "0") == "1"
