"""ESP32 telemetry model.

Field names, constants and the output policy mirror firmware/esp32/esp32.ino. Keep them in sync.
"""

import json
import time

# ---- BLE GATT (firmware: DEVICE_NAME, SERVICE_UUID, COMMAND_UUID, TELEMETRY_UUID)
DEVICE_NAME = "SmokeGuard-ESP32"
SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
COMMAND_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"    # write       (Pi -> ESP32)
TELEMETRY_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # read+notify (ESP32 -> Pi)

# ---- firmware constants the UI needs
NOTIFY_HZ = 2            # NOTIFY_MS = 500
RESET_HOLD_S = 15        # RESET_HOLD_MS
MUTE_S = 60              # MUTE_MS
MQ2_WARMUP_S = 120       # MQ2_WARMUP_S
MQ2_SUPPLY_V = 5.0       # MQ2_SUPPLY_V (AO range after the divider gain)
MQ2_MAX_PPM = 10000      # MQ2_MAX_PPM (upper bound for thresholds, AlarmLogic::validThresholds)

# ---- alarm levels (firmware Level / LevelText)
CLEAN, PRE, CRIT = "CLEAN", "PRE", "CRIT"

# ---- RGB LED colours (firmware COLOR_*) and buzzer patterns (BuzzPattern)
LED_OFF, LED_GREEN, LED_YELLOW, LED_RED, LED_BLUE = "OFF", "GREEN", "YELLOW", "RED", "BLUE"
BUZZ_OFF, BUZZ_CONTINUOUS, BUZZ_INTERMITTENT, BUZZ_CHIRP = "OFF", "CONTINUOUS", "INTERMITTENT", "CHIRP"

# ---- exhaust fan mode ("mode"): AUTO = alarm logic, ON = app override (firmware fanForced)
FAN_AUTO, FAN_ON = "AUTO", "ON"


class Telemetry:
    """One telemetry packet:
    {"t","mq","v","st","mode","fan","sil","do","wu","flt","fl","rh","mu","thr","src","ev"}."""

    __slots__ = (
        "uptime_s", "ppm", "volts", "state", "fan_mode", "fan", "hold", "do_trip", "warmup_s",
        "fault", "fire", "hold_s", "mute_s", "thr_pre", "thr_crit", "fire_src", "event", "rx",
    )

    @classmethod
    def parse(cls, raw) -> "Telemetry | None":
        try:
            return cls.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError, IndexError, UnicodeDecodeError):
            return None  # truncated or malformed packet: drop it

    @classmethod
    def from_dict(cls, d: dict) -> "Telemetry":
        t = cls()
        t.uptime_s = float(d["t"])
        t.ppm = int(d["mq"])
        t.volts = float(d["v"])
        t.state = d["st"] if d["st"] in (CLEAN, PRE, CRIT) else CLEAN
        t.fan_mode = FAN_ON if d.get("mode") == FAN_ON else FAN_AUTO
        t.fan = bool(d["fan"])
        t.hold = bool(d["sil"])
        t.do_trip = bool(d["do"])
        t.warmup_s = int(d["wu"])
        t.fault = bool(d["flt"])
        t.fire = bool(d["fl"])
        t.hold_s = int(d["rh"])
        t.mute_s = int(d.get("mu", 0))
        t.thr_pre, t.thr_crit = int(d["thr"][0]), int(d["thr"][1])
        t.fire_src = str(d.get("src", "IR"))
        t.event = str(d.get("ev", ""))
        t.rx = time.monotonic()
        return t

    @property
    def warming(self) -> bool:
        return self.warmup_s > 0

    @property
    def muted(self) -> bool:
        return self.mute_s > 0

    def output_plan(self) -> tuple:
        """(led, blink, buzzer) exactly as firmware OutputPolicy::plan() drives the hardware."""
        if self.hold:
            return LED_OFF, False, BUZZ_OFF
        led, blink, buzz = self.alarm_plan()
        return led, blink, BUZZ_OFF if self.muted else buzz

    def alarm_plan(self) -> tuple:
        """(led, blink, buzzer) the alarm level calls for, before Reset hold and mute (OutputPolicy::alarm)."""
        if self.state == CRIT:
            return LED_RED, False, BUZZ_CONTINUOUS
        if self.fault:
            return LED_YELLOW, True, BUZZ_CHIRP
        if self.state == PRE:
            return LED_YELLOW, False, BUZZ_INTERMITTENT
        if self.warming:
            return LED_BLUE, True, BUZZ_OFF
        return LED_GREEN, False, BUZZ_OFF
