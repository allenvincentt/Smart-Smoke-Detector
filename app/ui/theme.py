"""HUD palette, fonts and the one global stylesheet (applied once at startup).

Light neumorphic theme: panels share the background colour and are lifted off it by a white
highlight (top-left) and a cool grey shadow (bottom-right); blue is the accent.
"""

from PySide6.QtGui import QColor, QFont

from app.telemetry import CLEAN, CRIT, LED_BLUE, LED_GREEN, LED_OFF, LED_RED, LED_YELLOW, PRE

BG = QColor("#e4e9f0")
PANEL = QColor("#e4e9f0")      # neumorphic faces are the background colour
GROOVE = QColor("#dce2ea")     # inset wells and tracks
SHADOW = QColor("#a3b1c6")     # dark neumorphic shadow
HILITE = QColor("#ffffff")     # light neumorphic highlight
SHADOW_A = 150
HILITE_A = 235
LINE = QColor("#c9d1dd")
ACCENT = QColor("#4a86d4")
ACCENT_DIM = QColor("#9fbbe0")
TEXT = QColor("#334155")
TEXT_DIM = QColor("#7a8599")
ON_COLOR = QColor("#ffffff")   # text on filled accent / status pills
GREEN = QColor("#1a9f62")
YELLOW = QColor("#dd8c0c")
RED = QColor("#e0434b")
BLUE = QColor("#3f5be0")
OFF = QColor("#c2cad6")

LED_COLORS = {LED_GREEN: GREEN, LED_YELLOW: YELLOW, LED_RED: RED, LED_BLUE: BLUE, LED_OFF: OFF}
LEVEL_COLORS = {CLEAN: GREEN, PRE: YELLOW, CRIT: RED}
SEVERITY_COLORS = {"RED": RED, "YELLOW": YELLOW, "GREEN": GREEN, "ACCENT": ACCENT, "BLUE": BLUE, "DIM": TEXT_DIM}

_UI_FAMILIES = ["Bahnschrift", "DejaVu Sans Condensed", "DejaVu Sans", "Segoe UI"]
_MONO_FAMILIES = ["Consolas", "DejaVu Sans Mono", "Courier New"]


def font(px: int, weight=QFont.Normal, mono: bool = False, spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(_MONO_FAMILIES if mono else _UI_FAMILIES)
    f.setPixelSize(px)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


def alpha(color: QColor, a: int) -> QColor:
    c = QColor(color)
    c.setAlpha(a)
    return c


STYLESHEET = f"""
QWidget {{ background: {BG.name()}; color: {TEXT.name()}; }}
QPushButton#nav {{ border: none; }}
QScrollBar:vertical {{ background: {GROOVE.name()}; width: 8px; margin: 2px 0; border-radius: 4px; }}
QScrollBar::handle:vertical {{ background: {ACCENT_DIM.name()}; min-height: 30px; border-radius: 4px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""
