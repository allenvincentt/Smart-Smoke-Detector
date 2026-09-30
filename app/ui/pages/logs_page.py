"""Event & alarm logger: timestamped log of threshold breaches, user interventions and system
events, read from the always-on EventLog (app/event_log.py). Filter by category and time range;
export the current view to CSV (USB stick on the Pi when one is mounted)."""

import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QHBoxLayout, QScroller, QSizePolicy, QVBoxLayout, QWidget

from app.event_log import COLUMNS
from app.events import BREACH, SYSTEM, USER
from app.ui import theme as T
from app.ui.hud import HudButton, HudPanel, ReadoutStrip, round_path

CATEGORIES = (("ALL", None), ("BREACHES", BREACH), ("USER", USER), ("SYSTEM", SYSTEM))
RANGES = (("24H", 86400), ("7D", 7 * 86400), ("30D", 30 * 86400))
CATEGORY_COLORS = {BREACH: T.RED, USER: T.ACCENT, SYSTEM: T.TEXT_DIM}
MAX_ROWS = 1000  # newest rows kept in the table (bounded: the page lives for the app's lifetime)
_I = {name: i for i, name in enumerate(COLUMNS)}


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s // 60 % 60:02d}m"


class LogTable(QAbstractScrollArea):
    """Painted, virtualised log table (only visible rows are drawn); drag or wheel to scroll."""

    ROW_H = 30
    HEAD_H = 24

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows = []
        self._f_empty = T.font(10, QFont.DemiBold, spacing=1.6)
        self._f_time = T.font(12, mono=True)
        self._f_tag = T.font(9, QFont.Bold, spacing=1.2)
        self._f_text = T.font(13, QFont.DemiBold, spacing=0.6)
        self._f_detail = T.font(11, spacing=0.6)
        self._f_num = T.font(13, QFont.DemiBold, mono=True)
        self.setFrameShape(QAbstractScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, self.HEAD_H, 0, 0)
        self._header = _LogHeader(self)
        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)  # touch drag

    def set_rows(self, rows: list) -> None:
        self.rows = rows
        self.verticalScrollBar().setValue(0)
        self._relayout()

    def prepend(self, row: tuple) -> None:
        self.rows.insert(0, row)
        del self.rows[MAX_ROWS:]
        bar = self.verticalScrollBar()
        keep = bar.value() > 0  # reading older entries: don't let new rows push the view
        self._relayout()
        if keep:
            bar.setValue(bar.value() + self.ROW_H)

    def replace(self, row: tuple) -> None:
        for i, r in enumerate(self.rows):
            if r[_I["id"]] == row[_I["id"]]:
                self.rows[i] = row
                self.viewport().update()
                return

    def _relayout(self) -> None:
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, len(self.rows) * self.ROW_H - self.viewport().height()))
        bar.setPageStep(self.viewport().height())
        bar.setSingleStep(self.ROW_H)
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._header.setGeometry(0, 0, self.viewport().width(), self.HEAD_H)
        self._relayout()

    def scrollContentsBy(self, _dx: int, _dy: int) -> None:
        self.viewport().update()

    def _columns(self, w: float) -> dict:
        """x positions: time | type | event | ppm | level | duration."""
        wide = w >= 700
        cols = {"time": 10, "type": 86 if not wide else 150}
        cols["event"] = cols["type"] + 76
        cols["dur"] = w - (150 if wide else 96)
        cols["level"] = cols["dur"] - 56
        cols["ppm"] = cols["level"] - 60
        cols["wide"] = wide
        return cols

    def paintEvent(self, _event) -> None:
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.viewport().width(), self.viewport().height()
        c = self._columns(w)

        if not self.rows:
            p.setFont(self._f_empty)
            p.setPen(T.TEXT_DIM)
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "NO EVENTS")
            return

        top = self.verticalScrollBar().value()
        first = top // self.ROW_H
        y = first * self.ROW_H - top
        for row in self.rows[first:]:
            if y > h:
                break
            self._paint_row(p, row, y, w, c, first % 2)
            first += 1
            y += self.ROW_H

    def _paint_row(self, p: QPainter, row: tuple, y: float, w: float, c: dict, odd: int) -> None:
        sev = T.SEVERITY_COLORS.get(row[_I["severity"]], T.TEXT_DIM)
        rh = self.ROW_H
        if odd:
            p.fillPath(round_path(QRectF(0, y + 1, w, rh - 2), 8), T.alpha(T.HILITE, 120))
        p.setPen(Qt.NoPen)
        p.setBrush(sev)
        p.drawRoundedRect(QRectF(1, y + 7, 4, rh - 14), 2, 2)

        def text(x, width, s, font, color, align=Qt.AlignLeft):
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(x, y, width, rh), align | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(s, Qt.ElideRight, int(width)))

        ts = row[_I["ts"]]
        stamp = time.strftime("%m-%d %H:%M:%S" if c["wide"] else "%H:%M:%S", time.localtime(ts))
        text(c["time"], c["type"] - c["time"] - 6, stamp, self._f_time, T.TEXT_DIM)

        cat = row[_I["category"]]
        cat_color = sev if cat == BREACH else CATEGORY_COLORS[cat]
        chip = QRectF(c["type"], y + 7, 64, rh - 14)
        p.setPen(QPen(T.alpha(cat_color, 150), 1))
        p.setBrush(T.alpha(cat_color, 30))
        p.drawPath(round_path(chip, chip.height() / 2))
        p.setBrush(Qt.NoBrush)
        text(chip.x(), chip.width(), cat, self._f_tag, cat_color, Qt.AlignHCenter)

        # event text + dim detail
        event_w = c["ppm"] - c["event"] - 10
        p.setFont(self._f_text)
        label = row[_I["text"]]
        label_w = min(p.fontMetrics().horizontalAdvance(label), event_w)
        text(c["event"], label_w + 2, label, self._f_text, sev if sev != T.TEXT_DIM else T.TEXT)
        detail = row[_I["detail"]]
        if detail and event_w - label_w > 40:
            text(c["event"] + label_w + 12, event_w - label_w - 12, detail, self._f_detail, T.TEXT_DIM)

        ppm = row[_I["ppm"]]
        text(c["ppm"], 54, "" if ppm is None else str(ppm), self._f_num, T.TEXT)
        level = row[_I["level"]]
        text(c["level"], 52, level or "", self._f_tag, T.LEVEL_COLORS.get(level, T.TEXT_DIM))

        if row[_I["ongoing"]]:
            text(c["dur"], w - c["dur"], "ONGOING", self._f_tag, T.RED)
        elif row[_I["duration"]] is not None:
            peak = row[_I["peak"]]
            dur = fmt_duration(row[_I["duration"]])
            text(c["dur"], w - c["dur"], f"{dur} · {peak}" if c["wide"] else dur, self._f_num, T.TEXT)


class _LogHeader(QWidget):
    """Column labels, drawn in the scroll area's top margin."""

    LABELS = (("time", "TIME"), ("type", "TYPE"), ("event", "EVENT"), ("ppm", "PPM"),
              ("level", "LEVEL"), ("dur", "DURATION · PEAK"))

    def __init__(self, table: LogTable) -> None:
        super().__init__(table)
        self._table = table
        self._font = T.font(10, QFont.DemiBold, spacing=1.6)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        c = self._table._columns(self.width())
        p.setFont(self._font)
        p.setPen(T.TEXT_DIM)
        for key, label in self.LABELS:
            p.drawText(QRectF(c[key], 0, 150, self.height()), Qt.AlignLeft | Qt.AlignVCenter, label)
        p.setPen(QPen(T.alpha(T.SHADOW, 110), 1))
        p.drawLine(0, self.height() - 2, self.width(), self.height() - 2)
        p.setPen(QPen(T.HILITE, 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)


class LogsPage(QWidget):
    USES_EVENT_LOG = True  # the shell passes the app's EventLog

    def __init__(self, link, event_log, parent=None) -> None:
        super().__init__(parent)
        self._log = event_log
        self._category = None
        self._range_s = RANGES[0][1]

        self.summary = ReadoutStrip(("BREACHES", "CRITICAL", "PRE-ALARM", "FIRE", "USER ACTIONS", "PEAK PPM"))
        summary_panel = HudPanel()
        summary_panel.body.addWidget(self.summary)

        # Summary and filters share one card: counts on top, the filters that scope them below.
        controls = QHBoxLayout()
        controls.setSpacing(4)
        self._cat_buttons = []
        for label, category in CATEGORIES:
            btn = self._chip(label, lambda _=False, c=category: self._set_category(c))
            self._cat_buttons.append((btn, category))
            controls.addWidget(btn)
        controls.addStretch(1)
        self._range_buttons = []
        for label, seconds in RANGES:
            btn = self._chip(label, lambda _=False, s=seconds: self._set_range(s))
            self._range_buttons.append((btn, seconds))
            controls.addWidget(btn)
        controls.addSpacing(18)
        self.export_btn = HudButton("EXPORT CSV", T.GREEN)
        self.export_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.export_btn.clicked.connect(self._export)
        controls.addWidget(self.export_btn)
        summary_panel.body.setSpacing(10)
        summary_panel.body.addLayout(controls)

        self.table = LogTable()
        self.toast = Toast()
        table_panel = HudPanel()
        table_panel.body.addWidget(self.table, 1)
        table_panel.body.addWidget(self.toast)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)  # panels carry their own shadow margin
        col.addWidget(summary_panel)
        col.addWidget(table_panel, 1)

        event_log.entry_added.connect(self._on_added)
        event_log.entry_updated.connect(self._on_updated)
        self._reload()

    @staticmethod
    def _chip(label: str, slot) -> HudButton:
        btn = HudButton(label, T.ACCENT)
        btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        btn.clicked.connect(slot)
        return btn

    def _since(self) -> float:
        return time.time() - self._range_s

    def _set_category(self, category) -> None:
        self._category = category
        self._reload()

    def _set_range(self, seconds: int) -> None:
        self._range_s = seconds
        self._reload()

    def _reload(self) -> None:
        for btn, category in self._cat_buttons:
            btn.set_state(btn.text(), T.ACCENT, category == self._category)
        for btn, seconds in self._range_buttons:
            btn.set_state(btn.text(), T.ACCENT, seconds == self._range_s)
        self.table.set_rows(self._log.query(self._category, self._since(), MAX_ROWS))
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        s = self._log.summary(self._since())

        def count(n, color):
            return str(n), color if n else T.TEXT

        self.summary.set_values([
            count(s["breaches"], T.YELLOW),
            count(s["crit"], T.RED),
            count(s["pre"], T.YELLOW),
            count(s["fire"], T.RED),
            (str(s["user"]), T.ACCENT if s["user"] else T.TEXT),
            (str(s["peak"] or 0), T.TEXT),
        ])

    # While the page is hidden, new rows are ignored: showEvent reloads everything, so the summary
    # query (a scan over up to 30 days of rows) never runs on the GUI thread for an unseen page.
    def _on_added(self, row: tuple) -> None:
        if not self.isVisible():
            return
        if self._category in (None, row[_I["category"]]):
            self.table.prepend(row)
        self._refresh_summary()

    def _on_updated(self, row: tuple) -> None:
        if not self.isVisible():
            return
        self.table.replace(row)
        self._refresh_summary()

    def showEvent(self, event) -> None:
        self._reload()  # time ranges are relative to now
        super().showEvent(event)

    def _export(self) -> None:
        try:
            path, count, usb = self._log.export_csv(self._category, self._since())
        except OSError as exc:
            self.toast.show_message(f"EXPORT FAILED · {exc.strerror or exc}", T.RED)
            return
        where = "USB" if usb else "LOCAL"
        self.toast.show_message(f"EXPORTED {count} ROWS → {where} · {path}", T.GREEN)


class Toast(QWidget):
    """One-line status message under the table (export result)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._text, self._color = "", T.TEXT_DIM
        self._font = T.font(11, QFont.DemiBold, spacing=1)
        self.setFixedHeight(18)
        self.hide()

    def show_message(self, text: str, color) -> None:
        self._text, self._color = text, color
        self.show()
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setFont(self._font)
        p.setPen(self._color)
        p.drawText(QRectF(4, 0, self.width() - 8, self.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(self._text, Qt.ElideMiddle, self.width() - 8))
