"""Event & alarm logger: timestamped SQLite log of threshold breaches, user interventions and
system events. Owned by the app shell, so it records from startup whichever page is open.

- Breaches (alarm level, fire, MQ-2 fault) are derived from telemetry state changes, not from the
  firmware's one-event-per-packet queue, so none are lost. The row that opens a breach is later
  filled in with its duration and peak ppm ("episode").
- Interventions and system events come from the firmware's `ev` codes (the ESP32 acknowledges
  every command it acts on), plus the app's own link up / lost.
- SD card: WAL mode; breach/intervention rows commit at once (they are rare), routine rows are
  batched; rows older than LOG_RETENTION_DAYS are pruned.
"""

import csv
import os
import sqlite3
import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from app import config
from app.ble_link import LINKED, STALE
from app.events import BREACH, USER, describe
from app.telemetry import CLEAN, CRIT, MUTE_S, PRE, RESET_HOLD_S, Telemetry

COLUMNS = ("id", "ts", "uptime", "category", "code", "text", "severity", "source", "ppm", "level",
           "detail", "duration", "peak", "episode", "ongoing")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,     -- Unix time, Pi clock (the ESP32 has no RTC)
    uptime   REAL,              -- ESP32 uptime in s, NULL for app-side events
    category TEXT NOT NULL,     -- BREACH / USER / SYSTEM
    code     TEXT NOT NULL,     -- firmware event code, or STATE_x->y / LINK_UP / ...
    text     TEXT NOT NULL,
    severity TEXT NOT NULL,     -- RED / YELLOW / GREEN / ACCENT / BLUE / DIM
    source   TEXT,              -- MQ-2 / IR / BUTTON / APP / ESP32 / PI
    ppm      INTEGER,           -- MQ-2 reading when logged
    level    TEXT,              -- CLEAN / PRE / CRIT when logged
    detail   TEXT,
    duration REAL,              -- breach episodes: seconds until cleared
    peak     INTEGER,           -- breach episodes: highest ppm during the episode
    episode  INTEGER NOT NULL DEFAULT 0,  -- 1 = this row opened a breach episode
    ongoing  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""
_RANK = {CLEAN: 0, PRE: 1, CRIT: 2}
_DERIVED = {"FLAME_DETECTED", "FLAME_CLEARED", "SENSOR_FAULT", "SENSOR_OK"}  # + STATE_*
_RESETS = ("BTN_RESET", "APP_RESET")


def export_target() -> tuple:
    """(directory, is_usb). A mounted USB stick on the Pi (/media/<user>/<label>), else DATA_DIR."""
    if sys.platform.startswith("linux"):
        user = os.environ.get("USER", "")
        for base in (os.path.join("/media", user), "/media", "/mnt"):
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if e.is_dir() and os.path.ismount(e.path) and os.access(e.path, os.W_OK):
                    return e.path, True
    return os.path.join(config.DATA_DIR, "exports"), False


class EventLog(QObject):
    entry_added = Signal(tuple)    # row in COLUMNS order
    entry_updated = Signal(tuple)  # row after its episode closed

    def __init__(self, link, path: str | None = None, parent=None) -> None:
        super().__init__(parent)
        name = "events-demo.db" if config.DEMO else "events.db"  # keep demo data out of the real log
        self.path = path or os.path.join(config.DATA_DIR, name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        # Episodes still open from a crash or power loss: their end time is unknown.
        self._db.execute("UPDATE events SET ongoing = 0, detail = 'INTERRUPTED' WHERE ongoing = 1")
        self._db.commit()
        self.prune()

        self._last: Telemetry | None = None
        self._episodes = {}   # key -> [row id, start ts, peak ppm]
        self._link_up = False

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(int(config.LOG_FLUSH_S * 1000))
        self._flush.timeout.connect(self._db.commit)
        self._pruner = QTimer(self)
        self._pruner.setTimerType(Qt.VeryCoarseTimer)
        self._pruner.timeout.connect(self.prune)
        self._pruner.start(6 * 3600 * 1000)

        link.telemetry.connect(self._on_telemetry)
        link.link_state.connect(self._on_link_state)
        self._add("APP_START", source="PI")

    # ---- recording
    def _add(self, code: str, t: Telemetry | None = None, source: str = "", detail: str | None = None,
             episode: bool = False) -> int:
        snap = t or self._last
        text, category, severity = describe(code, snap.fire_src if snap else "IR")
        row = (time.time(), t.uptime_s if t else None, category, code, text, severity, source,
               snap.ppm if snap else None, snap.state if snap else None, detail, None, None,
               int(episode), int(episode))
        cur = self._db.execute(f"INSERT INTO events ({', '.join(COLUMNS[1:])}) VALUES "
                               f"({', '.join('?' * (len(COLUMNS) - 1))})", row)
        if category in (BREACH, USER):
            self._db.commit()
            self._flush.stop()
        elif not self._flush.isActive():
            self._flush.start()
        self.entry_added.emit((cur.lastrowid,) + row)
        return cur.lastrowid

    def _open(self, key, code: str, t: Telemetry, source: str, detail: str) -> None:
        row_id = self._add(code, t, source, detail, episode=True)
        self._episodes[key] = [row_id, time.time(), t.ppm]

    def _close(self, key, detail: str | None = None) -> None:
        ep = self._episodes.pop(key, None)
        if ep is None:
            return
        row_id, start, peak = ep
        self._db.execute(
            "UPDATE events SET duration = ?, peak = ?, ongoing = 0, detail = coalesce(?, detail) WHERE id = ?",
            (time.time() - start, peak, detail, row_id))
        self._db.commit()
        self.entry_updated.emit(self.row(row_id))

    def _on_telemetry(self, t: Telemetry) -> None:
        prev, self._last = self._last, t
        for ep in self._episodes.values():
            ep[2] = max(ep[2], t.ppm)
        first = prev is None  # first packet after (re)connect: compare against a clean baseline
        old = CLEAN if first else prev.state

        if _RANK[t.state] > _RANK[old]:
            cause = f"FIRE · {t.fire_src}" if t.fire else (
                f"≥ {t.thr_crit if t.state == CRIT else t.thr_pre} PPM")
            self._open(("level", t.state), f"STATE_{old}->{t.state}", t,
                       t.fire_src if t.fire else "MQ-2", "ALREADY ACTIVE AT LINK" if first else cause)
        elif _RANK[t.state] < _RANK[old]:
            for level in (CRIT, PRE):
                if _RANK[level] > _RANK[t.state]:
                    self._close(("level", level))
            self._add(f"STATE_{old}->{t.state}", t, "MQ-2")

        if t.fire != (False if first else prev.fire):
            if t.fire:
                self._open("fire", "FLAME_DETECTED", t, t.fire_src, "ALREADY ACTIVE AT LINK" if first else None)
            else:
                self._close("fire")
                self._add("FLAME_CLEARED", t, t.fire_src)
        if t.fault != (False if first else prev.fault):
            if t.fault:
                self._open("fault", "SENSOR_FAULT", t, "MQ-2", "FAIL-SAFE PRE-ALARM")
            else:
                self._close("fault")
                self._add("SENSOR_OK", t, "MQ-2")

        code = t.event
        if code and code not in _DERIVED and not code.startswith("STATE_"):
            detail = None
            if code in _RESETS:
                detail = f"OUTPUTS HELD {RESET_HOLD_S} S"
            elif code == "APP_THR":
                detail = f"PRE {t.thr_pre} · CRIT {t.thr_crit} PPM"
            elif code == "APP_MUTE":
                detail = f"{MUTE_S} S"
            source = "BUTTON" if code.startswith("BTN_") else "APP" if code.startswith("APP_") else "ESP32"
            self._add(code, t, source, detail)

    def _on_link_state(self, state: str) -> None:
        if state == LINKED and not self._link_up:
            self._link_up = True
            self._add("LINK_UP", source="PI")
        elif state not in (LINKED, STALE) and self._link_up:
            self._link_up = False
            self._close_all("LINK LOST")
            self._add("LINK_LOST", source="PI")
            self._last = None

    def _close_all(self, detail: str) -> None:
        for key in list(self._episodes):
            self._close(key, detail)

    # ---- maintenance
    def prune(self) -> None:
        cutoff = time.time() - config.LOG_RETENTION_DAYS * 86400
        self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self._db.commit()

    def close(self) -> None:
        self._flush.stop()
        self._pruner.stop()
        self._close_all("APP CLOSED")
        self._db.commit()
        self._db.close()

    # ---- queries (category None = all)
    @staticmethod
    def _where(category: str | None, since: float) -> tuple:
        if category:
            return "WHERE ts >= ? AND category = ?", (since, category)
        return "WHERE ts >= ?", (since,)

    def row(self, row_id: int) -> tuple:
        return self._db.execute(f"SELECT {', '.join(COLUMNS)} FROM events WHERE id = ?", (row_id,)).fetchone()

    def query(self, category: str | None, since: float, limit: int = 1000) -> list:
        where, args = self._where(category, since)
        return self._db.execute(f"SELECT {', '.join(COLUMNS)} FROM events {where} "
                                f"ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()

    def summary(self, since: float) -> dict:
        r = self._db.execute(
            """SELECT
                 coalesce(sum(episode), 0),
                 coalesce(sum(episode AND code LIKE 'STATE_%->CRIT'), 0),
                 coalesce(sum(episode AND code LIKE 'STATE_%->PRE'), 0),
                 coalesce(sum(code = 'FLAME_DETECTED'), 0),
                 coalesce(sum(category = 'USER'), 0),
                 max(max(coalesce(ppm, 0)), max(coalesce(peak, 0)))
               FROM events WHERE ts >= ?""", (since,)).fetchone()
        return dict(zip(("breaches", "crit", "pre", "fire", "user", "peak"), r))

    def export_csv(self, category: str | None, since: float) -> tuple:
        """Write matching rows (oldest first) to CSV. Returns (path, row count, is_usb)."""
        self._db.commit()
        folder, usb = export_target()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, time.strftime("smokedetect-events-%Y%m%d-%H%M%S.csv"))
        where, args = self._where(category, since)
        rows = self._db.execute(
            f"SELECT ts, uptime, category, text, code, source, ppm, level, detail, duration, peak "
            f"FROM events {where} ORDER BY ts, id", args)
        count = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(("timestamp", "esp32_uptime_s", "category", "event", "code", "source", "ppm",
                        "level", "detail", "duration_s", "peak_ppm"))
            for ts, uptime, *rest, duration, peak in rows:
                w.writerow((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                            "" if uptime is None else f"{uptime:.1f}", *("" if v is None else v for v in rest),
                            "" if duration is None else f"{duration:.1f}", "" if peak is None else peak))
                count += 1
        return path, count, usb
