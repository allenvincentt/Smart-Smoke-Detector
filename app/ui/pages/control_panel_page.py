"""Control panel: exhaust-fan override, buzzer silence / reset, and MQ-2 calibration.

Every control sends a firmware command (esp32.ino handleCommand) and every indicator shows what
the ESP32 reports back in telemetry, never what was requested, so the page cannot show a state
the hardware is not in.
"""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from app.ble_link import LINKED
from app.telemetry import (
    BUZZ_OFF, CLEAN, CRIT, FAN_AUTO, FAN_ON, MQ2_MAX_PPM, MUTE_S, RESET_HOLD_S, Telemetry,
)
from app.ui import theme as T
from app.ui.hud import (
    EventTicker, FanGlyph, HudButton, HudPanel, HudStepper, ReadoutStrip, RingIndicator, SegmentBar,
    SegmentReadout, StatusTile,
)
from app.ui.pages.dashboard_page import BUZZ_COLORS, describe_event

THR_STEP = 10
THR_CONFIRM_S = 3.0     # how long to wait for the ESP32 to echo new thresholds
REZERO_ARM_MS = 3000    # RE-ZERO needs a second tap within this window
# Firmware events that answer a command or end an override; shown in the command log.
COMMAND_EVENTS = {"BTN_RESET", "RESET_HOLD_END", "MUTE_END", "MUTE_CANCELLED", "AUTO_CAL"}


class ControlPanelPage(QWidget):
    def __init__(self, link, parent=None) -> None:
        super().__init__(parent)
        self._link = link
        self._t = None
        self._thr_edited = False
        self._thr_pending = None      # (pre, crit) sent, awaiting the ESP32's echo
        self._thr_pending_until = 0.0
        self._rezero_armed = False
        self._rezero_timer = QTimer(self)
        self._rezero_timer.setSingleShot(True)
        self._rezero_timer.setInterval(REZERO_ARM_MS)
        self._rezero_timer.timeout.connect(self._disarm_rezero)

        # ---- exhaust fan
        self.fan_glyph = FanGlyph()
        self.fan_relay = StatusTile("RELAY")
        self.fan_mode = StatusTile("MODE")
        self.fan_auto_btn = HudButton("AUTO")
        self.fan_on_btn = HudButton("MANUAL ON", T.YELLOW)
        self.fan_auto_btn.clicked.connect(lambda: self._send({"cmd": "fan", "mode": FAN_AUTO}, "FAN AUTO"))
        self.fan_on_btn.clicked.connect(lambda: self._send({"cmd": "fan", "mode": FAN_ON}, "FAN MANUAL ON"))
        fan_panel = self._panel("EXHAUST FAN", self.fan_glyph, (self.fan_relay, self.fan_mode),
                                (self.fan_auto_btn, self.fan_on_btn))

        # ---- alarm
        self.silence_ring = RingIndicator("SILENCE")
        self.buzzer_tile = StatusTile("BUZZER")
        self.alarm_tile = StatusTile("ALARM")
        self.mute_btn = HudButton(f"MUTE {MUTE_S}S", T.YELLOW)
        self.reset_btn = HudButton(f"RESET {RESET_HOLD_S}S")
        self.mute_btn.clicked.connect(self._toggle_mute)
        self.reset_btn.clicked.connect(lambda: self._send({"cmd": "reset"}, "RESET"))
        alarm_panel = self._panel("ALARM", self.silence_ring, (self.buzzer_tile, self.alarm_tile),
                                  (self.mute_btn, self.reset_btn))

        # ---- command log
        self.log = EventTicker(rows=10)
        log_panel = HudPanel("COMMAND LOG")
        log_panel.body.addWidget(self.log)

        # ---- MQ-2 calibration
        self.ppm = SegmentReadout("PPM", digits=5)
        self.ppm.setFixedWidth(130)
        self.readouts = ReadoutStrip(("AO V", "WARM-UP", "SENSOR", "PRE", "CRIT"))
        self.level_bar = SegmentBar()
        self.pre_step = HudStepper("PRE", THR_STEP, MQ2_MAX_PPM - THR_STEP, THR_STEP)
        self.crit_step = HudStepper("CRIT", 2 * THR_STEP, MQ2_MAX_PPM, THR_STEP)
        self.apply_btn = HudButton("APPLY")
        self.revert_btn = HudButton("REVERT", T.TEXT_DIM)
        self.rezero_btn = HudButton("RE-ZERO")
        for step in (self.pre_step, self.crit_step):
            step.edited.connect(self._on_thr_edited)
        self.apply_btn.clicked.connect(self._apply_thresholds)
        self.revert_btn.clicked.connect(self._revert_thresholds)
        self.rezero_btn.clicked.connect(self._on_rezero)

        # threshold editing (steppers + apply / revert) as one group; re-zero set apart on the right
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self.pre_step, 3)
        controls.addWidget(self.crit_step, 3)
        controls.addWidget(self.apply_btn, 2)
        controls.addWidget(self.revert_btn, 2)
        controls.addSpacing(18)
        controls.addWidget(self.rezero_btn, 2)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self.readouts)
        right.addWidget(self.level_bar)
        right.addLayout(controls)
        cal_panel = HudPanel("MQ-2 CALIBRATION")
        row = QHBoxLayout()
        row.setSpacing(18)
        row.addWidget(self.ppm, 0, Qt.AlignVCenter)
        row.addLayout(right, 1)
        cal_panel.body.addLayout(row)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)  # panels carry their own shadow margin
        grid.addWidget(alarm_panel, 0, 0)  # most urgent controls first
        grid.addWidget(fan_panel, 0, 1)
        grid.addWidget(log_panel, 0, 2)
        grid.addWidget(cal_panel, 1, 0, 1, 3)
        grid.setColumnStretch(0, 4)  # the two control cards get the width; the log is a side column
        grid.setColumnStretch(1, 4)
        grid.setColumnStretch(2, 3)
        grid.setRowStretch(0, 1)

        self._controls = (self.fan_auto_btn, self.fan_on_btn, self.mute_btn, self.reset_btn,
                          self.pre_step, self.crit_step, self.apply_btn, self.revert_btn, self.rezero_btn)
        self._go_offline()

        link.telemetry.connect(self.on_telemetry)
        link.link_state.connect(self.on_link_state)

    @staticmethod
    def _panel(tag: str, visual: QWidget, tiles: tuple, buttons: tuple) -> HudPanel:
        """Visual on the left, status tiles on the right, action buttons along the bottom.

        Tiles keep a compact height and sit vertically centred beside the visual, so a tall card
        gains breathing room instead of oversized tiles."""
        panel = HudPanel(tag)
        panel.body.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        visual.setMaximumHeight(190)  # centred like the tiles, not pinned to the card's top
        visual_col = QVBoxLayout()
        visual_col.addStretch(1)
        visual_col.addWidget(visual, 8)
        visual_col.addStretch(1)
        top.addLayout(visual_col, 2)
        column = QVBoxLayout()
        column.setSpacing(10)
        column.addStretch(1)
        for tile in tiles:
            tile.setMaximumHeight(64)
            column.addWidget(tile)
        column.addStretch(1)
        top.addLayout(column, 3)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        for btn in buttons:
            bottom.addWidget(btn)
        panel.body.addLayout(top, 1)
        panel.body.addLayout(bottom)
        return panel

    # ---- link
    def on_link_state(self, state: str) -> None:
        if state != LINKED:
            self._go_offline()  # telemetry turns everything back on once it resumes

    def _go_offline(self) -> None:
        self._t = None
        self._thr_edited = False
        self._thr_pending = None
        self._disarm_rezero()
        for w in self._controls:
            w.setEnabled(False)
        self.fan_glyph.set_state(False, T.OFF)
        for tile in (self.fan_relay, self.fan_mode, self.buzzer_tile, self.alarm_tile):
            tile.set_state("--", T.TEXT_DIM, False)
        self.silence_ring.set_state(0.0, "--", T.TEXT_DIM)
        self.ppm.set_value(None, T.TEXT_DIM)
        self.readouts.set_values([("--", T.TEXT_DIM)] * 5)

    def _send(self, cmd: dict, label: str) -> bool:
        ok = self._link.send_command(cmd)
        self.log.add(f"→ {label}" if ok else f"✕ {label} · NO LINK", T.TEXT_DIM if ok else T.RED)
        return ok

    # ---- telemetry
    def on_telemetry(self, t: Telemetry) -> None:
        first = self._t is None
        self._t = t
        if first:
            for w in (self.fan_auto_btn, self.fan_on_btn, self.mute_btn, self.reset_btn,
                      self.pre_step, self.crit_step):
                w.setEnabled(True)
        self._show_fan(t)
        self._show_alarm(t)
        self._show_calibration(t)
        if t.event and (t.event.startswith("APP_") or t.event in COMMAND_EVENTS):
            self.log.add(*describe_event(t.event, t.fire_src))

    def _show_fan(self, t: Telemetry) -> None:
        manual = t.fan_mode == FAN_ON
        color = T.YELLOW if manual else T.ACCENT
        self.fan_glyph.set_state(t.fan, color)
        self.fan_relay.set_state("ON" if t.fan else "OFF", color, t.fan)
        self.fan_mode.set_state("MANUAL" if manual else "AUTO", color, manual)
        self.fan_auto_btn.set_state("AUTO", T.ACCENT, not manual)
        self.fan_on_btn.set_state("MANUAL ON", T.YELLOW, manual)

    def _show_alarm(self, t: Telemetry) -> None:
        _led, _blink, buzz = t.output_plan()
        wanted = t.alarm_plan()[2]
        if t.hold:
            self.buzzer_tile.set_state("HELD", T.ACCENT, False)
            self.silence_ring.set_state(t.hold_s / RESET_HOLD_S, f"{t.hold_s}s", T.ACCENT)
        elif t.muted:
            self.buzzer_tile.set_state("MUTED" if wanted != BUZZ_OFF else "OFF", T.YELLOW, False)
            self.silence_ring.set_state(t.mute_s / MUTE_S, f"{t.mute_s}s", T.YELLOW)
        else:
            color = BUZZ_COLORS.get(buzz, T.TEXT_DIM)
            self.buzzer_tile.set_state(buzz, color, buzz != BUZZ_OFF)
            self.silence_ring.set_state(0.0, "OFF", T.TEXT_DIM)

        level = t.state + (" · FIRE" if t.fire else "") + (" · FAULT" if t.fault else "")
        self.alarm_tile.set_state(level, T.LEVEL_COLORS[t.state], t.state != CLEAN)
        if t.muted:
            self.mute_btn.set_state("UNMUTE", T.YELLOW, True)
        else:
            self.mute_btn.set_state(f"MUTE {MUTE_S}S", T.YELLOW, False)
        if t.hold:
            self.reset_btn.set_state(f"HOLD {t.hold_s}S", T.ACCENT, True)
        else:
            self.reset_btn.set_state(f"RESET {RESET_HOLD_S}S", T.ACCENT, False)

    def _show_calibration(self, t: Telemetry) -> None:
        if t.warming and t.state != CRIT:
            color = T.BLUE
        else:
            color = T.LEVEL_COLORS[t.state]
        self.ppm.set_value(t.ppm, color)
        self.readouts.set_values([
            (f"{t.volts:.2f}", T.TEXT),
            (f"{t.warmup_s}s", T.BLUE) if t.warming else ("DONE", T.GREEN),
            ("FAULT", T.YELLOW) if t.fault else ("OK", T.GREEN),
            (f"{t.thr_pre}", T.YELLOW),
            (f"{t.thr_crit}", T.RED),
        ])

        if self._thr_pending is not None and (
            t.event in ("APP_THR", "APP_THR_REJECTED")
            or (t.thr_pre, t.thr_crit) == self._thr_pending
            or time.monotonic() > self._thr_pending_until
        ):
            self._thr_pending = None
        if not self._thr_edited and self._thr_pending is None:
            self.pre_step.set_value(t.thr_pre, T.TEXT)
            self.crit_step.set_value(t.thr_crit, T.TEXT)
        self._refresh_thresholds()

        can_zero = not t.warming and not t.fault
        self.rezero_btn.setEnabled(can_zero)
        if not can_zero:
            self._disarm_rezero()

    # ---- thresholds
    def _on_thr_edited(self, _value: int) -> None:
        self._thr_edited = True
        self._refresh_thresholds()

    def _refresh_thresholds(self) -> None:
        t = self._t
        if t is None:
            return
        pre, crit = self.pre_step.value(), self.crit_step.value()
        valid = 0 < pre < crit <= MQ2_MAX_PPM  # firmware AlarmLogic::validThresholds
        changed = self._thr_edited and (pre, crit) != (t.thr_pre, t.thr_crit)
        if self._thr_edited:
            draft = T.ACCENT if valid else T.RED
            self.pre_step.set_value(pre, draft if pre != t.thr_pre else T.TEXT)
            self.crit_step.set_value(crit, draft if crit != t.thr_crit else T.TEXT)
        self.apply_btn.setEnabled(changed and valid and self._thr_pending is None)
        self.revert_btn.setEnabled(changed)
        self.level_bar.set_level(t.ppm, pre, crit, t.warming)

    def _apply_thresholds(self) -> None:
        thr = (self.pre_step.value(), self.crit_step.value())
        if self._send({"cmd": "thr", "mq": list(thr)}, f"THRESHOLDS {thr[0]} / {thr[1]}"):
            self._thr_pending = thr
            self._thr_pending_until = time.monotonic() + THR_CONFIRM_S
            self._thr_edited = False
            self._refresh_thresholds()

    def _revert_thresholds(self) -> None:
        self._thr_edited = False
        if self._t is not None:
            self.pre_step.set_value(self._t.thr_pre, T.TEXT)
            self.crit_step.set_value(self._t.thr_crit, T.TEXT)
        self._refresh_thresholds()

    # ---- re-zero: re-baselines the MQ-2 against the current air, so it takes two taps
    def _on_rezero(self) -> None:
        if not self._rezero_armed:
            self._rezero_armed = True
            self.rezero_btn.set_state("CONFIRM", T.YELLOW, True)
            self._rezero_timer.start()
            return
        self._disarm_rezero()
        self._send({"cmd": "cal"}, "RE-ZERO MQ-2")

    def _disarm_rezero(self) -> None:
        self._rezero_armed = False
        self._rezero_timer.stop()
        self.rezero_btn.set_state("RE-ZERO", T.ACCENT, False)

    def _toggle_mute(self) -> None:
        if self._t is not None and self._t.muted:
            self._send({"cmd": "unmute"}, "UNMUTE")
        else:
            self._send({"cmd": "mute"}, "MUTE")
