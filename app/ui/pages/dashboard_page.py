"""Live monitoring dashboard. Every value shown comes from ESP32 telemetry (app/telemetry.py)."""

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from app import config
from app.ble_link import LINKED, STALE
from app.events import describe
from app.telemetry import (
    BUZZ_CHIRP, BUZZ_CONTINUOUS, BUZZ_INTERMITTENT, BUZZ_OFF, CLEAN, CRIT, DEVICE_NAME, FAN_ON, LED_OFF,
    MQ2_SUPPLY_V, MQ2_WARMUP_S, NOTIFY_HZ, PRE, RESET_HOLD_S, Telemetry,
)
from app.ui import theme as T
from app.ui.hud import (
    ArcGauge, EventTicker, HudPanel, ReadoutStrip, RingIndicator, SegmentBar, StaticLayer, StatusTile,
    TrendChart, draw_lamp, draw_pill, neu_inset, neu_raised, round_path,
)

LEVEL_COLORS = T.LEVEL_COLORS
TREND_COLORS = {CLEAN: T.ACCENT, PRE: T.YELLOW, CRIT: T.RED}
BUZZ_COLORS = {BUZZ_CONTINUOUS: T.RED, BUZZ_INTERMITTENT: T.YELLOW, BUZZ_CHIRP: T.YELLOW}


def describe_event(code: str, fire_src: str) -> tuple:
    """Firmware event code -> (display text, colour)."""
    text, _category, severity = describe(code, fire_src)
    return text, T.SEVERITY_COLORS[severity]


class AirQualityBanner(QWidget):
    """Mirrors the ESP32 RGB LED (OutputPolicy::plan), including its blink and reset-hold states."""

    SEGMENTS = ((CLEAN, "CLEAN"), (PRE, "PRE-ALARM"), (CRIT, "CRITICAL"))
    MARGIN = HudPanel.MARGIN
    RADIUS = 20
    SEG_W, SEG_GAP = 84, 6

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._color = T.OFF
        self._title = "NO SIGNAL"
        self._detail = f"{DEVICE_NAME} · SCANNING"
        self._lamp = False
        self._blink = False
        self._blink_on = True
        self._level = None
        self._f_title = T.font(24, QFont.Bold, spacing=3)
        self._f_detail = T.font(12, QFont.DemiBold, spacing=1.6)
        self._f_seg = T.font(10, QFont.Bold, spacing=1.4)
        self._blinker = QTimer(self)
        self._blinker.setInterval(500)  # firmware BLINK_ON_MS / BLINK_OFF_MS
        self._blinker.timeout.connect(self._toggle_blink)
        self._static = StaticLayer()
        self.setAttribute(Qt.WA_OpaquePaintEvent)  # the static layer paints every pixel
        self.setFixedHeight(76)

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        self.update(0, 0, 80, self.height())  # only the lamp (and its glow) blinks

    def _apply(self, color, title, detail, lamp, blink, level) -> None:
        if blink != self._blink:
            self._blink, self._blink_on = blink, True
            if blink:
                self._blinker.start()
            else:
                self._blinker.stop()
        new = (color, title, detail, lamp, level)
        if new != (self._color, self._title, self._detail, self._lamp, self._level):
            self._color, self._title, self._detail, self._lamp, self._level = new
            self.update()

    def show_link(self, state: str) -> None:
        self._apply(T.OFF, "NO SIGNAL", f"{DEVICE_NAME} · {state}", False, False, None)

    def show_telemetry(self, t: Telemetry) -> None:
        led, blink, _buzz = t.output_plan()
        color = T.LED_COLORS[led]
        if t.hold:
            title = "OUTPUTS HELD"
            detail = f"RESET · {t.hold_s} S · SENSING {t.state}"
        elif t.state == CRIT:
            title = "CRITICAL SMOKE HAZARD"
            causes = []
            if t.fire:
                causes.append(f"FIRE · {t.fire_src}")
            if not t.warming and t.ppm >= t.thr_crit:
                causes.append(f"MQ-2 {t.ppm} ≥ {t.thr_crit} PPM")
            if not causes:
                causes.append(f"MQ-2 {t.ppm} PPM")
            if t.fan:
                causes.append("EXHAUST ON")
            detail = " · ".join(causes)
        elif t.fault:
            title = "MQ-2 SENSOR FAULT"
            detail = "FAIL-SAFE PRE-ALARM"
        elif t.state == PRE:
            title = "ELEVATED SMOKE"
            detail = f"PRE-ALARM · MQ-2 {t.ppm} PPM"
        elif t.warming:
            title = "SENSOR WARM-UP"
            detail = f"MQ-2 HEATER · {t.warmup_s} S"
        else:
            title = "CLEAN AIR"
            detail = f"MQ-2 {t.ppm} PPM"
        self._apply(color, title, detail, led != LED_OFF, blink, t.state)

    def _face(self) -> QRectF:
        m = self.MARGIN
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _segments_rect(self, face: QRectF) -> QRectF | None:
        """Level selector track on the right, or None when the banner is too narrow for it."""
        if self.width() <= 640:
            return None
        total = 3 * self.SEG_W + 2 * self.SEG_GAP
        return QRectF(face.right() - 18 - total, face.center().y() - 12, total, 24)

    def _lamp_center(self, face: QRectF) -> QPointF:
        return QPointF(face.left() + 32, face.center().y())

    def _paint_static(self, p: QPainter) -> None:
        """Raised card, lamp socket and level track: change only with size."""
        face = self._face()
        neu_raised(p, round_path(face, self.RADIUS), 3, 4)
        c = self._lamp_center(face)
        neu_inset(p, round_path(QRectF(c.x() - 20, c.y() - 20, 40, 40), 20), 2, 3)
        seg = self._segments_rect(face)
        if seg is not None:
            track = seg.adjusted(-5, -5, 5, 5)
            neu_inset(p, round_path(track, track.height() / 2), 2, 3)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static.get(self, None, self._paint_static))
        p.setRenderHint(QPainter.Antialiasing)
        face = self._face()

        # soft wash of the LED colour from the lamp side
        grad = QLinearGradient(face.left(), 0, face.right(), 0)
        grad.setColorAt(0, T.alpha(self._color, 60))
        grad.setColorAt(0.5, T.alpha(self._color, 10))
        grad.setColorAt(1, T.alpha(self._color, 0))
        p.fillPath(round_path(face, self.RADIUS), grad)

        lit = self._lamp and (not self._blink or self._blink_on)
        draw_lamp(p, self._lamp_center(face), 12, self._color, lit)
        p.setBrush(Qt.NoBrush)

        seg = self._segments_rect(face)
        text_w = face.width() - 70 - (seg.width() + 30 if seg is not None else 12)
        p.setFont(self._f_title)
        p.setPen(T.TEXT if self._lamp else T.TEXT_DIM)
        p.drawText(QRectF(face.left() + 66, face.top() + 4, text_w, 34), Qt.AlignLeft | Qt.AlignVCenter,
                   self._title)
        p.setFont(self._f_detail)
        p.setPen(self._color if self._color != T.OFF else T.TEXT_DIM)
        p.drawText(QRectF(face.left() + 67, face.top() + 36, text_w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   self._detail)

        if seg is None:
            return
        # Sensed alarm level (firmware "st"); dimmed while the LED itself is held off.
        x = seg.left()
        p.setFont(self._f_seg)
        for level, label in self.SEGMENTS:
            c = LEVEL_COLORS[level]
            box = QRectF(x, seg.top(), self.SEG_W, seg.height())
            if level == self._level:
                draw_pill(p, box, box.height() / 2, c if self._lamp else T.alpha(c, 120), glow=self._lamp)
                p.setPen(T.ON_COLOR)
            else:
                p.setPen(T.alpha(c, 150))
            p.drawText(box, Qt.AlignCenter, label)
            x += self.SEG_W + self.SEG_GAP


class DashboardPage(QWidget):
    def __init__(self, link, parent=None) -> None:
        super().__init__(parent)
        self._ppm = deque(maxlen=config.HISTORY_SIZE)
        self._volts = deque(maxlen=config.HISTORY_SIZE)
        self._link_state = None

        self.banner = AirQualityBanner()

        self.gauge = ArcGauge("PPM")
        gauge_panel = HudPanel("MQ-2")
        gauge_panel.body.addWidget(self.gauge)
        self._gauge_panel = gauge_panel

        # Analysis card: the trend with its concentration bar and statistics directly beneath it.
        self.chart = TrendChart(self._ppm, self._volts, MQ2_SUPPLY_V, NOTIFY_HZ)
        self.level_bar = SegmentBar()
        self.readouts = ReadoutStrip(("Δ 10S", "RATE/S", "PEAK", "AVG", "MIN", "% CRIT"))
        trend_panel = HudPanel()
        trend_panel.body.setSpacing(10)
        trend_panel.body.addWidget(self.chart, 1)
        trend_panel.body.addWidget(self.level_bar)
        trend_panel.body.addWidget(self.readouts)

        self.fire_tile = StatusTile("FIRE · IR")
        self.fan_tile = StatusTile("EXHAUST FAN")
        self.buzzer_tile = StatusTile("BUZZER")
        self._tiles = (self.fire_tile, self.fan_tile, self.buzzer_tile)
        status_panel = HudPanel()
        status_panel.body.setSpacing(10)
        status_panel.body.addStretch(1)
        for tile in self._tiles:
            tile.setMaximumHeight(72)  # compact tiles, centred in the card
            status_panel.body.addWidget(tile)
        status_panel.body.addStretch(1)

        self.warmup_ring = RingIndicator("MQ-2")
        self.hold_ring = RingIndicator("RESET")
        rings_panel = HudPanel()
        rings_row = QHBoxLayout()
        rings_row.setSpacing(12)
        rings_row.addStretch(1)
        rings_row.addWidget(self.warmup_ring)
        rings_row.addWidget(self.hold_ring)
        rings_row.addStretch(1)
        rings_panel.body.addLayout(rings_row)

        self.events = EventTicker()
        events_panel = HudPanel()
        events_panel.body.addWidget(self.events)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)  # panels carry their own shadow margin
        # Banner across the top, then three columns: sensor (gauge over readiness rings),
        # analysis (full height), outputs (status tiles over the event feed).
        grid.addWidget(self.banner, 0, 0, 1, 3)
        grid.addWidget(gauge_panel, 1, 0)
        grid.addWidget(rings_panel, 2, 0)
        grid.addWidget(trend_panel, 1, 1, 2, 1)
        grid.addWidget(status_panel, 1, 2)
        grid.addWidget(events_panel, 2, 2)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 7)
        grid.setColumnStretch(2, 3)
        grid.setRowStretch(1, 1)
        rings_panel.setFixedHeight(126)
        events_panel.setFixedHeight(126)
        gauge_panel.setMinimumWidth(204)
        status_panel.setMinimumWidth(204)

        link.telemetry.connect(self.on_telemetry)
        link.link_state.connect(self.on_link_state)

    def on_link_state(self, state: str) -> None:
        was_up = self._link_state in (LINKED, STALE)
        self._link_state = state
        if state == LINKED:
            if not was_up:
                self.events.add("LINK UP", T.ACCENT)
            return
        # No live data: blank the live readouts so old values are never mistaken for current ones.
        self.gauge.clear()
        for tile in self._tiles:
            tile.set_state("--", T.TEXT_DIM, False)
        self.warmup_ring.set_state(0.0, "--", T.TEXT_DIM)
        self.hold_ring.set_state(0.0, "--", T.TEXT_DIM)
        self._gauge_panel.set_accent(T.ACCENT_DIM)
        if state == STALE:
            self.banner.show_link("NO DATA")
            self.events.add("TELEMETRY STALLED", T.YELLOW)
        else:
            self.banner.show_link(state)
            if was_up:
                self.events.add("LINK LOST", T.RED)

    def on_telemetry(self, t: Telemetry) -> None:
        self._ppm.append(t.ppm)
        self._volts.append(t.volts)
        led, _blink, buzz = t.output_plan()

        self.banner.show_telemetry(t)

        level_color = TREND_COLORS[t.state]
        gauge_color = T.BLUE if t.warming and t.state == CLEAN else level_color
        self.gauge.set_data(t.ppm, t.thr_pre, t.thr_crit, gauge_color, f"{t.volts:.2f} V",
                            "WARM-UP" if t.warming else "")
        self._gauge_panel.set_accent(T.LED_COLORS[led] if led != LED_OFF else T.ACCENT_DIM)
        self.chart.set_style(t.thr_pre, t.thr_crit, level_color)
        self.chart.update()

        self.fire_tile.set_key(f"FIRE · {t.fire_src}")
        self.fire_tile.set_state("DETECTED" if t.fire else "CLEAR", T.RED if t.fire else T.GREEN, t.fire)
        manual = t.fan_mode == FAN_ON
        self.fan_tile.set_state(("ON" if t.fan else "OFF") + (" · MANUAL" if manual else ""),
                                T.YELLOW if manual else T.ACCENT, t.fan)
        if t.muted and not t.hold and t.alarm_plan()[2] != BUZZ_OFF:
            self.buzzer_tile.set_state(f"MUTED · {t.mute_s}s", T.YELLOW, False)
        else:
            buzz_color = BUZZ_COLORS.get(buzz, T.TEXT_DIM)
            self.buzzer_tile.set_state(buzz, buzz_color, buzz_color is not T.TEXT_DIM)

        if t.fault:
            self.warmup_ring.set_state(1.0, "FAULT", T.YELLOW)
        elif t.warming:
            self.warmup_ring.set_state(t.warmup_s / MQ2_WARMUP_S, f"{t.warmup_s}s", T.BLUE)
        else:
            self.warmup_ring.set_state(1.0, "READY", T.GREEN)
        if t.hold:
            self.hold_ring.set_state(t.hold_s / RESET_HOLD_S, f"{t.hold_s}s", T.ACCENT)
        else:
            self.hold_ring.set_state(0.0, "ARMED", T.TEXT_DIM)

        self._update_stats(t)
        if t.event:
            self.events.add(*describe_event(t.event, t.fire_src))

    def _update_stats(self, t: Telemetry) -> None:
        h = self._ppm
        n = len(h)
        back10 = h[-1 - min(n - 1, 10 * NOTIFY_HZ)]
        back5 = h[-1 - min(n - 1, 5 * NOTIFY_HZ)]
        delta = h[-1] - back10
        rate = (h[-1] - back5) / max(1, min(n - 1, 5 * NOTIFY_HZ)) * NOTIFY_HZ
        pct = t.ppm / t.thr_crit * 100 if t.thr_crit else 0

        def signed(v, fmt):
            return (f"+{v:{fmt}}" if v > 0 else f"{v:{fmt}}"), (T.YELLOW if v > 0 else T.GREEN if v < 0 else T.TEXT)

        pct_color = T.RED if t.ppm >= t.thr_crit else T.YELLOW if t.ppm >= t.thr_pre else T.TEXT
        self.readouts.set_values([
            signed(delta, "d"),
            signed(rate, ".1f"),
            (f"{max(h)}", T.TEXT),
            (f"{sum(h) / n:.0f}", T.TEXT),
            (f"{min(h)}", T.TEXT),
            (f"{pct:.0f}%", pct_color),
        ])
        self.level_bar.set_level(t.ppm, t.thr_pre, t.thr_crit, t.warming)
