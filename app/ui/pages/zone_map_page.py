"""Interactive smoke zone map: floor plan with the monitored room lit by live ESP32 telemetry.

One ESP32 node = one monitored room (config.SENSOR_ROOM). The other rooms are drawn but marked
unmonitored, so the map never implies sensor coverage that does not exist. Click a room to
inspect it.
"""

import math
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QWidget

from app import config
from app.ble_link import LINKED
from app.telemetry import BUZZ_CHIRP, BUZZ_CONTINUOUS, BUZZ_INTERMITTENT, CLEAN, CRIT, NOTIFY_HZ, PRE, Telemetry
from app.ui import theme as T
from app.ui.hud import (
    HudPanel, SegmentBar, SegmentReadout, Sparkline, StaticLayer, StatusTile, draw_lamp, draw_pill, neu_inset,
    neu_raised, round_path,
)

# name, (x, y, w, h) as fractions of the plan. Edit to match the real building.
ROOMS = (
    ("KITCHEN", (0.00, 0.00, 0.36, 0.52)),
    ("DINING", (0.36, 0.00, 0.26, 0.52)),
    ("BEDROOM 1", (0.62, 0.00, 0.38, 0.52)),
    ("LIVING ROOM", (0.00, 0.52, 0.46, 0.48)),
    ("BATH", (0.46, 0.52, 0.18, 0.48)),
    ("BEDROOM 2", (0.64, 0.52, 0.36, 0.48)),
)
PLAN_ASPECT = 1.7                 # plan width / height
SENSOR_POS = (0.5, 0.58)          # detector position inside its room (fractions)
ROOM_RADIUS = 12
LEVEL_TEXT = {CLEAN: "CLEAN", PRE: "PRE-ALARM", CRIT: "CRITICAL"}


def zone_color(t: Telemetry):
    """Room tint: the sensed smoke level; blue while the MQ-2 is still warming up."""
    if t.warming and t.state == CLEAN:
        return T.BLUE
    return T.LEVEL_COLORS[t.state]


def zone_label(t: Telemetry) -> str:
    if t.fault:
        return "SENSOR FAULT"
    if t.warming and t.state == CLEAN:
        return "WARM-UP"
    return LEVEL_TEXT[t.state]


class FloorPlan(QWidget):
    room_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._t = None
        self._online = False
        self._selected = config.SENSOR_ROOM
        self._hover = None
        self._rects = {}
        self._plan = QRectF()
        self._pulse = False
        self._pulser = QTimer(self)
        self._pulser.setInterval(500)
        self._pulser.timeout.connect(self._toggle_pulse)
        self._f_room = T.font(12, QFont.Bold, spacing=2)
        self._f_value = T.font(13, QFont.DemiBold, mono=True)
        self._f_small = T.font(10, QFont.DemiBold, spacing=1.4)
        # Grid, unmonitored rooms and legend only change on resize / hover / selection; telemetry
        # and the alarm pulse repaint just the monitored room over this cached layer.
        self._static = StaticLayer()
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 220)

    # ---- data
    def set_telemetry(self, t: Telemetry) -> None:
        self._t, self._online = t, True
        # Pulse only while something is actively alarming; otherwise no timer runs.
        alarming = t.state == CRIT or t.fire
        if alarming and not self._pulser.isActive():
            self._pulser.start()
        elif not alarming and self._pulser.isActive():
            self._pulser.stop()
            self._pulse = False
        self._update_monitored()

    def set_offline(self) -> None:
        self._online = False
        self._pulser.stop()
        self._pulse = False
        self._update_monitored()

    def _toggle_pulse(self) -> None:
        self._pulse = not self._pulse
        self._update_monitored()

    def _update_monitored(self) -> None:
        r = self._rects.get(config.SENSOR_ROOM)
        if r is None:
            self.update()
        else:
            self.update(r.adjusted(-3, -3, 3, 3).toAlignedRect())  # + selection outline width

    # ---- geometry / interaction
    def resizeEvent(self, event) -> None:
        legend_h = 22
        w, h = self.width() - 8, self.height() - 8 - legend_h
        pw, ph = (w, w / PLAN_ASPECT) if w / h < PLAN_ASPECT else (h * PLAN_ASPECT, h)
        self._plan = QRectF((self.width() - pw) / 2, 4 + (h - ph) / 2, pw, ph)
        gap = 12  # room for each room's raised shadow
        self._rects = {
            name: QRectF(self._plan.x() + x * pw + gap / 2, self._plan.y() + y * ph + gap / 2,
                         rw * pw - gap, rh * ph - gap)
            for name, (x, y, rw, rh) in ROOMS
        }
        super().resizeEvent(event)

    def _room_at(self, pos) -> str | None:
        for name, r in self._rects.items():
            if r.contains(pos):
                return name
        return None

    def mouseMoveEvent(self, event) -> None:
        room = self._room_at(event.position())
        if room != self._hover:
            self._hover = room
            self.setCursor(Qt.PointingHandCursor if room else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, _event) -> None:
        if self._hover:
            self._hover = None
            self.update()

    def mousePressEvent(self, event) -> None:
        room = self._room_at(event.position())
        if room and room != self._selected:
            self._selected = room
            self.room_selected.emit(room)
            self.update()

    def select(self, room: str) -> None:
        self._selected = room
        self.update()

    # ---- painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static.get(self, (self._selected, self._hover), self._paint_static))
        p.setRenderHint(QPainter.Antialiasing)
        r = self._rects.get(config.SENSOR_ROOM)
        if r is not None:
            self._paint_monitored(p, config.SENSOR_ROOM, r)

    def _paint_static(self, p: QPainter) -> None:
        # inset floor well with a faint blueprint grid
        well = round_path(self._plan, 14)
        neu_inset(p, well, 3, 4)
        p.save()
        p.setClipPath(well)
        p.setPen(QPen(T.alpha(T.LINE, 150), 1))
        step = 22
        x = self._plan.left()
        while x <= self._plan.right():
            p.drawLine(QPointF(x, self._plan.top()), QPointF(x, self._plan.bottom()))
            x += step
        y = self._plan.top()
        while y <= self._plan.bottom():
            p.drawLine(QPointF(self._plan.left(), y), QPointF(self._plan.right(), y))
            y += step
        p.restore()

        # every room is a raised tile; the monitored one is tinted live over its tile
        for r in self._rects.values():
            neu_raised(p, round_path(r, ROOM_RADIUS), 3, 3)
        for name, r in self._rects.items():
            if name != config.SENSOR_ROOM:
                self._paint_unmonitored(p, name, r)
        self._paint_legend(p)

    def _outline(self, p: QPainter, name: str, path: QPainterPath, base) -> None:
        if name == self._selected:
            p.setPen(QPen(T.ACCENT, 2))
        elif name == self._hover:
            p.setPen(QPen(T.alpha(T.ACCENT, 160), 1.5))
        elif base is not None:
            p.setPen(QPen(base, 1.2))
        else:
            return
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def _paint_unmonitored(self, p: QPainter, name: str, r: QRectF) -> None:
        self._outline(p, name, round_path(r, ROOM_RADIUS), None)
        p.setFont(self._f_room)
        p.setPen(T.TEXT_DIM)
        p.drawText(QPointF(r.left() + 10, r.top() + 20), name)
        p.setFont(self._f_small)
        p.setPen(T.alpha(T.TEXT_DIM, 140))
        p.drawText(QPointF(r.left() + 10, r.top() + 36), "NO SENSOR")

    def _paint_monitored(self, p: QPainter, name: str, r: QRectF) -> None:
        path = round_path(r, ROOM_RADIUS)
        t = self._t
        live = self._online and t is not None
        color = zone_color(t) if live else T.OFF
        sensor = QPointF(r.left() + SENSOR_POS[0] * r.width(), r.top() + SENSOR_POS[1] * r.height())

        # tint strength follows the smoke level relative to the CRIT threshold
        level = min(1.0, t.ppm / t.thr_crit) if live and t.thr_crit else 0.0
        p.fillPath(path, T.alpha(color, int(22 + 50 * level) if live else 14))

        if live:
            # smoke plume around the detector, confined to this room
            p.save()
            p.setClipPath(path)
            radius = max(r.width(), r.height()) * (0.18 + 0.9 * level)
            if self._pulse:
                radius *= 1.08
            plume = QRadialGradient(sensor, radius)
            plume.setColorAt(0, T.alpha(color, 150))
            plume.setColorAt(0.6, T.alpha(color, 55))
            plume.setColorAt(1, T.alpha(color, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(plume)
            p.drawEllipse(sensor, radius, radius)
            p.setBrush(Qt.NoBrush)
            for k in (0.35, 0.65, 1.0):
                p.setPen(QPen(T.alpha(color, int(120 * (1.1 - k))), 1, Qt.DashLine))
                p.drawEllipse(sensor, radius * k, radius * k)
            p.restore()

        self._outline(p, name, path, T.alpha(color, 230) if live else None)
        if name != self._selected and live:
            p.setPen(QPen(T.alpha(color, 230), 2))
            p.drawPath(path)

        # labels
        p.setFont(self._f_room)
        p.setPen(T.TEXT)
        p.drawText(QPointF(r.left() + 10, r.top() + 20), name)
        p.setFont(self._f_value)
        p.setPen(color if live else T.TEXT_DIM)
        p.drawText(QPointF(r.left() + 10, r.top() + 38), f"{t.ppm} PPM" if live else "NO SIGNAL")
        if live:
            p.setFont(self._f_small)
            p.drawText(QPointF(r.left() + 10, r.top() + 53), zone_label(t))

        # detector marker
        led, blink, _ = t.output_plan() if live else ("OFF", False, None)
        led_color = T.LED_COLORS[led]
        p.setPen(QPen(T.alpha(T.TEXT, 110), 1))
        p.setBrush(T.alpha(T.HILITE, 150))
        p.drawEllipse(sensor, 11, 11)
        for a in range(0, 360, 90):
            rad = math.radians(a)
            p.drawLine(QPointF(sensor.x() + 13 * math.cos(rad), sensor.y() + 13 * math.sin(rad)),
                       QPointF(sensor.x() + 17 * math.cos(rad), sensor.y() + 17 * math.sin(rad)))
        draw_lamp(p, sensor, 5, led_color, live and led != "OFF")

        if live and t.fire:
            self._paint_flame(p, QPointF(sensor.x() + 26, sensor.y() - 6), 1.15 if self._pulse else 1.0)
        if live and t.fan:
            self._paint_fan(p, QPointF(r.right() - 22, r.top() + 22), 11)

    @staticmethod
    def _paint_flame(p: QPainter, c: QPointF, scale: float) -> None:
        s = 12 * scale
        path = QPainterPath(QPointF(c.x(), c.y() - s))
        path.cubicTo(c.x() + s * 0.9, c.y() - s * 0.2, c.x() + s * 0.8, c.y() + s * 0.8, c.x(), c.y() + s * 0.8)
        path.cubicTo(c.x() - s * 0.8, c.y() + s * 0.8, c.x() - s * 0.9, c.y() - s * 0.2, c.x(), c.y() - s)
        p.setPen(QPen(T.RED, 1.5))
        p.setBrush(T.alpha(T.RED, 150))
        p.drawPath(path)
        p.setBrush(T.YELLOW)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(c.x(), c.y() + s * 0.35), s * 0.28, s * 0.38)

    @staticmethod
    def _paint_fan(p: QPainter, c: QPointF, radius: float) -> None:
        p.setPen(QPen(T.ACCENT, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, radius + 3, radius + 3)
        p.setBrush(T.alpha(T.ACCENT, 170))
        for a in (0, 120, 240):
            p.save()
            p.translate(c)
            p.rotate(a)
            p.drawEllipse(QRectF(-radius * 0.3, -radius, radius * 0.6, radius))
            p.restore()

    def _paint_legend(self, p: QPainter) -> None:
        items = ((T.GREEN, "CLEAN"), (T.YELLOW, "PRE-ALARM"), (T.RED, "CRITICAL"),
                 (T.BLUE, "WARM-UP"), (T.OFF, "NO SENSOR"))
        p.setFont(self._f_small)
        x = self._plan.left()
        y = self.height() - 12
        for color, label in items:
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, y - 5, 10, 10), 3, 3)
            p.setPen(T.TEXT_DIM)
            p.drawText(QPointF(x + 15, y + 4), label)
            x += 15 + p.fontMetrics().horizontalAdvance(label) + 16


class ZoneHeader(QWidget):
    """Selected room name + status chip."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._name, self._chip, self._color = "", "", T.OFF
        self._f_name = T.font(18, QFont.Bold, spacing=3)
        self._f_chip = T.font(10, QFont.Bold, spacing=1.4)
        self.setFixedHeight(56)

    def set_state(self, name: str, chip: str, color) -> None:
        if (name, chip, color) != (self._name, self._chip, self._color):
            self._name, self._chip, self._color = name, chip, color
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._f_name)
        p.setPen(T.TEXT)
        p.drawText(QRectF(0, 0, self.width(), 26), Qt.AlignLeft | Qt.AlignVCenter, self._name)
        p.setFont(self._f_chip)
        chip = QRectF(3, 30, p.fontMetrics().horizontalAdvance(self._chip) + 26, 22)
        if self._color != T.OFF:
            draw_pill(p, chip, chip.height() / 2, self._color)
            p.setPen(T.ON_COLOR)
        else:
            neu_inset(p, round_path(chip, chip.height() / 2), 1.5, 2)
            p.setPen(T.TEXT_DIM)
        p.drawText(chip, Qt.AlignCenter, self._chip)


class ZoneMapPage(QWidget):
    def __init__(self, link, parent=None) -> None:
        super().__init__(parent)
        self._t = None
        self._online = True
        self._selected = config.SENSOR_ROOM
        self._history = deque(maxlen=120 * NOTIFY_HZ)  # last 2 minutes

        self.plan = FloorPlan()
        map_panel = HudPanel()
        map_panel.body.addWidget(self.plan)

        self.header = ZoneHeader()
        self.readout = SegmentReadout("PPM")
        self.level_bar = SegmentBar()
        self.spark = Sparkline(self._history)
        self.fire_tile = StatusTile("FIRE · IR")
        self.fan_tile = StatusTile("EXHAUST FAN")
        self.buzzer_tile = StatusTile("BUZZER")
        self._tiles = (self.fire_tile, self.fan_tile, self.buzzer_tile)

        side = HudPanel()
        side.setFixedWidth(264)
        side.body.setSpacing(8)
        side.body.addWidget(self.header)
        side.body.addWidget(self.readout, 2)
        side.body.addWidget(self.level_bar)
        side.body.addWidget(self.spark, 2)
        side.body.addSpacing(4)  # live readings above, output tiles below
        for tile in self._tiles:
            tile.setMaximumHeight(52)
            side.body.addWidget(tile, 1)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)  # panels carry their own shadow margin
        row.addWidget(map_panel, 1)
        row.addWidget(side)

        self.plan.room_selected.connect(self._on_select)
        link.telemetry.connect(self.on_telemetry)
        link.link_state.connect(self.on_link_state)
        self._refresh_side()

    def on_link_state(self, state: str) -> None:
        self._online = state == LINKED
        if not self._online:
            self.plan.set_offline()
            self._refresh_side()

    def on_telemetry(self, t: Telemetry) -> None:
        self._t, self._online = t, True
        self._history.append(t.ppm)
        self.plan.set_telemetry(t)
        self._refresh_side()

    def _on_select(self, room: str) -> None:
        self._selected = room
        self._refresh_side()

    def _refresh_side(self) -> None:
        t = self._t
        monitored = self._selected == config.SENSOR_ROOM
        live = monitored and self._online and t is not None

        if not monitored:
            self.header.set_state(self._selected, "NO SENSOR", T.OFF)
        elif not live:
            self.header.set_state(self._selected, "NO SIGNAL", T.OFF)
        else:
            self.header.set_state(self._selected, zone_label(t), zone_color(t))

        if not live:
            self.readout.set_value(None, T.TEXT_DIM)
            self.level_bar.set_level(0, 250, 500, True)
            for tile in self._tiles:
                tile.set_state("--", T.TEXT_DIM, False)
            self.spark.setEnabled(monitored)
            self.spark.update()
            return

        color = zone_color(t)
        _led, _blink, buzz = t.output_plan()
        self.readout.set_value(t.ppm, color)
        self.level_bar.set_level(t.ppm, t.thr_pre, t.thr_crit, t.warming)
        self.spark.setEnabled(True)
        self.spark.set_style(t.thr_pre, t.thr_crit, color)
        self.spark.update()
        self.fire_tile.set_key(f"FIRE · {t.fire_src}")
        self.fire_tile.set_state("DETECTED" if t.fire else "CLEAR", T.RED if t.fire else T.GREEN, t.fire)
        self.fan_tile.set_state("ON" if t.fan else "OFF", T.ACCENT, t.fan)
        buzz_color = {BUZZ_CONTINUOUS: T.RED, BUZZ_INTERMITTENT: T.YELLOW, BUZZ_CHIRP: T.YELLOW}.get(buzz, T.TEXT_DIM)
        self.buzzer_tile.set_state(buzz, buzz_color, buzz_color is not T.TEXT_DIM)
