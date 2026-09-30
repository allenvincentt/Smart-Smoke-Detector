"""Firmware / app event codes -> display text, log category and severity. Shared by the dashboard
ticker and the event log. Codes mirror pushEvent() calls in firmware/esp32/esp32.ino."""

from app.telemetry import CLEAN, CRIT, PRE

# Log categories (instructor brief: threshold breaches and user interventions).
BREACH, USER, SYSTEM = "BREACH", "USER", "SYSTEM"

# Severity names map to theme colours in the UI (theme.SEVERITY_COLORS).
RED, YELLOW, GREEN, ACCENT, BLUE, DIM = "RED", "YELLOW", "GREEN", "ACCENT", "BLUE", "DIM"
LEVEL_SEVERITY = {CLEAN: GREEN, PRE: YELLOW, CRIT: RED}

EVENT_INFO = {
    # sensing (the log derives these from telemetry state, so none are lost to the firmware's queue)
    "FLAME_DETECTED": ("FIRE DETECTED · {src}", BREACH, RED),
    "FLAME_CLEARED": ("FIRE CLEARED · {src}", BREACH, GREEN),
    "SENSOR_FAULT": ("MQ-2 FAULT", BREACH, YELLOW),
    "SENSOR_OK": ("MQ-2 RESTORED", BREACH, GREEN),
    # user interventions
    "BTN_RESET": ("RESET · BUTTON", USER, ACCENT),
    "APP_RESET": ("RESET · APP", USER, ACCENT),
    "APP_THR": ("THRESHOLDS SET", USER, ACCENT),
    "APP_THR_REJECTED": ("THRESHOLDS REJECTED", USER, YELLOW),
    "APP_CAL": ("MQ-2 CALIBRATED", USER, ACCENT),
    "APP_CAL_REFUSED": ("CALIBRATION REFUSED", USER, YELLOW),
    "APP_FAN_ON": ("FAN · MANUAL ON", USER, YELLOW),
    "APP_FAN_AUTO": ("FAN · AUTO", USER, ACCENT),
    "APP_FAN_REJECTED": ("FAN COMMAND REJECTED", USER, YELLOW),
    "APP_MUTE": ("BUZZER MUTED", USER, YELLOW),
    "APP_UNMUTE": ("BUZZER UNMUTED", USER, ACCENT),
    "APP_UNSUPPORTED": ("UNSUPPORTED COMMAND", USER, YELLOW),
    # system
    "BOOT": ("ESP32 BOOT", SYSTEM, ACCENT),
    "WARMUP_DONE": ("MQ-2 WARM-UP DONE", SYSTEM, BLUE),
    "AUTO_CAL": ("MQ-2 AUTO RE-ZERO", SYSTEM, ACCENT),
    "RESET_HOLD_END": ("RESET HOLD END", SYSTEM, ACCENT),
    "MUTE_END": ("MUTE END", SYSTEM, ACCENT),
    "MUTE_CANCELLED": ("MUTE CANCELLED · ESCALATION", SYSTEM, RED),
    # app-side (not from the firmware)
    "APP_START": ("APP START", SYSTEM, DIM),
    "LINK_UP": ("LINK UP", SYSTEM, ACCENT),
    "LINK_LOST": ("LINK LOST", SYSTEM, RED),
}


def describe(code: str, fire_src: str = "IR") -> tuple:
    """(text, category, severity) for an event code, including STATE_<old>-><new> transitions."""
    if code.startswith("STATE_") and "->" in code:
        old, new = code[6:].split("->", 1)
        return f"{old} → {new}", BREACH, LEVEL_SEVERITY.get(new, DIM)
    text, category, severity = EVENT_INFO.get(code, (code.replace("_", " "), SYSTEM, DIM))
    return text.format(src=fire_src), category, severity
