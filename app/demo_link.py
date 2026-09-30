"""Stand-in for BleLink when no ESP32 is available (SSD_DEMO=1 or --demo).

Emits firmware-format telemetry at 2 Hz and applies the firmware's alarm rules (fire -> CRIT,
warm-up ignores the MQ-2, 0.9 hysteresis, 15 s reset hold) to a scripted smoke scenario, and
accepts the same commands (reset, thr, cal, fan, mute, unmute).
"""

import math
import random
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal

from app.ble_link import LINKED
from app.telemetry import CLEAN, CRIT, FAN_AUTO, FAN_ON, MUTE_S, PRE, RESET_HOLD_S, Telemetry

_WARMUP_S = 20     # shortened from the firmware's 120 s so the demo gets going
_CYCLE_S = 90      # scenario: clean -> pre-alarm -> critical (+ fire) -> decay
_HYSTERESIS = 0.9
_MAX_PPM = 10000   # MQ2_MAX_PPM


class DemoLink(QObject):
    telemetry = Signal(object)
    link_state = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._t0 = time.monotonic()
        self._ppm = 0.0
        self._level = 0
        self._fire = False
        self._warming = True
        self._hold_until = 0.0
        self._mute_until = 0.0
        self._fan_forced = False
        self._thr = [250, 500]
        self._events = deque(["BOOT"], maxlen=6)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.link_state.emit(LINKED)
        self._timer.start(500)

    def stop(self) -> None:
        self._timer.stop()

    def send_command(self, cmd: dict) -> bool:
        name, now = cmd.get("cmd"), time.monotonic()
        if name == "reset":
            self._hold_until = now + RESET_HOLD_S
            self._mute_until = 0.0
            self._fan_forced = False
            self._events.append("APP_RESET")
        elif name == "thr":
            try:
                pre, crit = (float(v) for v in cmd["mq"])
            except (KeyError, TypeError, ValueError):
                pre = crit = 0.0
            if 0 < pre < crit <= _MAX_PPM:  # AlarmLogic::validThresholds
                self._thr = [round(pre), round(crit)]
                self._events.append("APP_THR")
            else:
                self._events.append("APP_THR_REJECTED")
        elif name == "cal":
            self._events.append("APP_CAL_REFUSED" if self._warming else "APP_CAL")
        elif name == "fan" and cmd.get("mode") in (FAN_ON, FAN_AUTO):
            self._fan_forced = cmd["mode"] == FAN_ON
            self._events.append("APP_FAN_ON" if self._fan_forced else "APP_FAN_AUTO")
        elif name == "fan":
            self._events.append("APP_FAN_REJECTED")
        elif name == "mute":
            self._mute_until = now + MUTE_S
            self._events.append("APP_MUTE")
        elif name == "unmute":
            self._mute_until = 0.0
            self._events.append("APP_UNMUTE")
        else:
            self._events.append("APP_UNSUPPORTED")
        return True

    def _target_ppm(self, t: float) -> float:
        phase = (t - _WARMUP_S) % _CYCLE_S if t > _WARMUP_S else 0.0
        if phase < 25:
            base = 40
        elif phase < 45:
            base = 40 + (phase - 25) * 16      # ramp into pre-alarm
        elif phase < 60:
            base = 360 + (phase - 45) * 25     # ramp into critical
        else:
            base = 735 * math.exp(-(phase - 60) / 8)
        return max(0.0, base + random.gauss(0, 6))

    def _tick(self) -> None:
        now = time.monotonic()
        t = now - self._t0
        warmup_left = max(0, math.ceil(_WARMUP_S - t))
        warming = warmup_left > 0
        if self._warming and not warming:
            self._events.append("WARMUP_DONE")
        self._warming = warming

        self._ppm += 0.3 * (self._target_ppm(t) - self._ppm)  # firmware PPM_EMA_ALPHA
        phase = (t - _WARMUP_S) % _CYCLE_S if not warming else 0
        fire = 52 <= phase < 58
        fire_started = fire and not self._fire
        if fire != self._fire:
            self._events.append("FLAME_DETECTED" if fire else "FLAME_CLEARED")
            self._fire = fire

        prev = self._level
        pre, crit = self._thr
        if fire:
            level = 2
        elif warming:
            level = 0
        elif self._ppm >= crit * (_HYSTERESIS if prev >= 2 else 1):
            level = 2
        elif self._ppm >= pre * (_HYSTERESIS if prev >= 1 else 1):
            level = 1
        else:
            level = 0
        names = (CLEAN, PRE, CRIT)
        if level != prev:
            self._events.append(f"STATE_{names[prev]}->{names[level]}")
        self._level = level
        if self._mute_until and (level > prev or fire_started):
            self._mute_until = 0.0
            self._events.append("MUTE_CANCELLED")

        hold_left = max(0, math.ceil(self._hold_until - now))
        if self._hold_until and not hold_left:
            self._hold_until = 0.0
            self._events.append("RESET_HOLD_END")
        mute_left = max(0, math.ceil(self._mute_until - now))
        if self._mute_until and not mute_left:
            self._mute_until = 0.0
            self._events.append("MUTE_END")

        volts = min(4.9, 0.35 + 0.11 * math.sqrt(self._ppm))
        self.telemetry.emit(Telemetry.from_dict({
            "t": round(t, 1), "mq": round(self._ppm), "v": round(volts, 2),
            "st": names[level], "mode": FAN_ON if self._fan_forced else FAN_AUTO,
            "fan": int((level == 2 or self._fan_forced) and not hold_left),
            "sil": int(hold_left > 0), "do": 0, "wu": warmup_left, "flt": 0, "fl": int(fire),
            "rh": hold_left, "mu": mute_left, "thr": list(self._thr), "src": "IR",
            "ev": self._events.popleft() if self._events else "",
        }))
