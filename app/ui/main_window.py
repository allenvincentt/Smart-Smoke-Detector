"""App shell: top bar (brand, navigation, link, clock, window buttons), header, and the main
content area. The window is frameless: drag the top bar to move, double-click it to maximize,
and resize from the bottom-right grip.

Pages live in app/ui/pages/. Each page is a QWidget subclass taking `(link, parent=None)`, where
`link` is the BleLink (or DemoLink) with `telemetry` / `link_state` signals and `send_command()`.
A page that sets `USES_EVENT_LOG = True` is built as `(link, event_log)` instead. Pages are imported and built on first visit, except the dashboard, which starts immediately so
its history keeps filling.
"""

import importlib
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QPushButton, QSizeGrip, QStackedWidget, QVBoxLayout, QWidget,
)

from app import config
from app.ble_link import LINKED, STALE
from app.telemetry import DEVICE_NAME, Telemetry
from app.ui import theme as T
from app.ui.hud import draw_lamp, draw_pill, neu_blit

# (nav label, module, class)
PAGES = (
    ("DASHBOARD", "app.ui.pages.dashboard_page", "DashboardPage"),
    ("ZONE MAP", "app.ui.pages.zone_map_page", "ZoneMapPage"),
    ("CONTROL", "app.ui.pages.control_panel_page", "ControlPanelPage"),
    ("LOGS", "app.ui.pages.logs_page", "LogsPage"),
)


class LinkIndicator(QWidget):
    COLORS = {LINKED: T.GREEN, STALE: T.YELLOW}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "OFFLINE"
        self._font = T.font(11, QFont.DemiBold, spacing=1.6)
        self.setFixedWidth(116)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # clicks fall through to drag the bar

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = self.COLORS.get(self._state, T.TEXT_DIM)
        draw_lamp(p, QPointF(10, self.height() / 2), 4, color, self._state in self.COLORS)
        p.setFont(self._font)
        p.setPen(color)
        p.drawText(QRectF(22, 0, self.width() - 22, self.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   self._state)


class WindowButton(QPushButton):
    """Painted minimize / maximize-restore / close glyph button."""

    MIN, MAX, CLOSE = "min", "max", "close"

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._maximized = False
        self.setFixedSize(QSize(36, 40))
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)  # repaint on hover enter/leave
        self.setToolTip({self.MIN: "Minimize", self.MAX: "Maximize", self.CLOSE: "Close"}[kind])

    def set_maximized(self, maximized: bool) -> None:
        if maximized != self._maximized:
            self._maximized = maximized
            self.setToolTip("Restore" if maximized else "Maximize")
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse()
        c = QPointF(self.width() / 2, self.height() / 2)
        r = 11.0
        neu_blit(p, self, ("rect", c.x() - r, c.y() - r, 2 * r, 2 * r, r), 1.5 if self.isDown() else 2,
                 2.5, inset=self.isDown(), convex=True)
        glyph = (T.RED if self._kind == self.CLOSE else T.ACCENT) if hot else T.TEXT_DIM
        p.setPen(QPen(glyph, 1.6, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        s = 4.0
        if self._kind == self.MIN:
            p.drawLine(QPointF(c.x() - s, c.y()), QPointF(c.x() + s, c.y()))
        elif self._kind == self.MAX and self._maximized:
            p.drawRect(QRectF(c.x() - s, c.y() - s + 2, 2 * s - 2, 2 * s - 2))
            p.drawPolyline(QPolygonF([QPointF(c.x() - s + 2, c.y() - s), QPointF(c.x() + s, c.y() - s),
                                      QPointF(c.x() + s, c.y() + s - 2)]))
        elif self._kind == self.MAX:
            p.drawRect(QRectF(c.x() - s, c.y() - s, 2 * s, 2 * s))
        else:
            p.drawLine(QPointF(c.x() - s, c.y() - s), QPointF(c.x() + s, c.y() + s))
            p.drawLine(QPointF(c.x() - s, c.y() + s), QPointF(c.x() + s, c.y() - s))


class NavButton(QPushButton):
    """Segment of the pill navigation: the checked page is a glowing blue pill."""

    def __init__(self, text: str, font: QFont, parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setFont(font)
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(QFontMetrics(self.font()).horizontalAdvance(self.text()) + 18, 40)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.isChecked():
            face = QRectF(self.rect()).adjusted(1, 9, -1, -9)
            draw_pill(p, face, face.height() / 2, T.ACCENT)
            p.setPen(T.ON_COLOR)
        else:
            p.setPen(T.TEXT if self.underMouse() else T.TEXT_DIM)
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text())


class TopBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(40)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)  # window buttons sit flush with the right edge
        row.setSpacing(0)

        self.brand = QLabel("◈ SMOKEDETECT")
        self.brand.setFont(T.font(15, QFont.Bold, spacing=2))
        self.brand.setStyleSheet(f"color: {T.ACCENT.name()}; background: transparent;")
        self.brand.setFixedWidth(self.brand.sizeHint().width())
        row.addWidget(self.brand)
        row.addStretch(1)

        # Navigation is positioned manually (see resizeEvent) so it stays centred on the window,
        # not just between the unequal left and right groups.
        self._nav_box = QWidget(self)
        nav_row = QHBoxLayout(self._nav_box)
        nav_row.setContentsMargins(0, 0, 0, 0)
        nav_row.setSpacing(0)
        self.nav = QButtonGroup(self)
        nav_font = T.font(12, QFont.DemiBold, spacing=1.5)
        for i, (label, _module, _cls) in enumerate(PAGES):
            btn = NavButton(label, nav_font)
            self.nav.addButton(btn, i)
            nav_row.addWidget(btn)
        self._nav_box.setStyleSheet("background: transparent;")

        self.link = LinkIndicator()
        row.addWidget(self.link)
        self.clock = QLabel()
        self.clock.setFont(T.font(14, QFont.DemiBold, mono=True))
        self.clock.setStyleSheet(f"color: {T.TEXT.name()}; background: transparent;")
        row.addWidget(self.clock)
        row.addSpacing(10)

        self.min_btn = WindowButton(WindowButton.MIN)
        self.max_btn = WindowButton(WindowButton.MAX)
        self.close_btn = WindowButton(WindowButton.CLOSE)
        for btn in (self.min_btn, self.max_btn, self.close_btn):
            row.addWidget(btn)

    def resizeEvent(self, event) -> None:
        # The layout has already placed the side groups; centre the nav on the full width and
        # only shift it when a narrow window would make it overlap either side.
        super().resizeEvent(event)
        w = self._nav_box.sizeHint().width()
        lo = self.brand.geometry().right() + 12
        hi = self.link.geometry().left() - 12 - w
        x = max(lo, min((self.width() - w) // 2, hi))
        self._nav_box.setGeometry(x, 0, w, self.height())
        self.update()  # the nav track is painted by the bar

    # Empty areas of the bar (and the labels, which don't take clicks) move the window.
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self.window().isFullScreen():
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.max_btn.click()
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), T.BG)
        track = QRectF(self._nav_box.geometry()).adjusted(-3, 6, 3, -6)
        neu_blit(p, self, ("rect", track.x(), track.y(), track.width(), track.height(), track.height() / 2),
                 2, 2.5, inset=True)
        # engraved bottom edge
        h = self.height()
        p.setPen(QPen(T.alpha(T.SHADOW, 110), 1))
        p.drawLine(QPointF(0, h - 1.5), QPointF(self.width(), h - 1.5))
        p.setPen(QPen(T.HILITE, 1))
        p.drawLine(QPointF(0, h - 0.5), QPointF(self.width(), h - 0.5))


class Header(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(34)
        self._title = ""
        self._meta = f"{DEVICE_NAME}"
        self._f_title = T.font(18, QFont.Bold, spacing=4)
        self._f_meta = T.font(12, mono=True)

    def set_title(self, title: str) -> None:
        self._title = title
        self.update()

    def set_meta(self, meta: str) -> None:
        if meta != self._meta:
            self._meta = meta
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, x0, x1 = self.height(), 7.0, self.width() - 7.0
        draw_pill(p, QRectF(x0, 8, 4, h - 16), 2, T.ACCENT)
        p.setFont(self._f_title)
        p.setPen(T.TEXT)
        p.drawText(QRectF(x0 + 14, 0, x1 / 2, h), Qt.AlignLeft | Qt.AlignVCenter, self._title)
        p.setFont(self._f_meta)
        p.setPen(T.TEXT_DIM)
        p.drawText(QRectF(x1 / 2, 0, x1 / 2, h), Qt.AlignRight | Qt.AlignVCenter, self._meta)
        p.setPen(QPen(T.alpha(T.SHADOW, 100), 1))
        p.drawLine(QPointF(x0 + 14, h - 1.5), QPointF(x1, h - 1.5))
        p.setPen(QPen(T.HILITE, 1))
        p.drawLine(QPointF(x0 + 14, h - 0.5), QPointF(x1, h - 0.5))


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle("Smart Smoke Detector")
        self.setMinimumSize(800, 480)
        self.resize(1024, 600)
        self._normal_geometry = None  # geometry to go back to on restore

        if config.DEMO:
            from app.demo_link import DemoLink
            self.link = DemoLink(self)
        else:
            from app.ble_link import BleLink
            self.link = BleLink(self)
        from app.event_log import EventLog
        self.event_log = EventLog(self.link, parent=self)  # records from startup, whatever page is open

        self.top_bar = TopBar()
        self.header = Header()
        self.content = QStackedWidget()
        self._pages = [None] * len(PAGES)
        for _ in PAGES:
            self.content.addWidget(QWidget())  # placeholder until the page is first opened

        body = QVBoxLayout()
        body.setContentsMargins(3, 4, 3, 3)  # panels carry their own 7 px shadow margin
        body.setSpacing(2)
        body.addWidget(self.header)
        body.addWidget(self.content, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)  # room for the 1 px window outline
        root.setSpacing(0)
        root.addWidget(self.top_bar)
        root.addLayout(body, 1)

        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(16, 16)
        self._grip.setStyleSheet("background: transparent;")

        self.top_bar.min_btn.clicked.connect(self.showMinimized)
        self.top_bar.max_btn.clicked.connect(self._toggle_maximized)
        self.top_bar.close_btn.clicked.connect(self.close)
        self.top_bar.nav.idClicked.connect(self.show_page)
        self.link.link_state.connect(self.top_bar.link.set_state)
        self.link.telemetry.connect(self._on_telemetry)

        self._clock = QTimer(self)
        self._clock.setTimerType(Qt.CoarseTimer)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()

        self.show_page(0)
        self.link.start()

    def show_page(self, index: int) -> None:
        if self._pages[index] is None:
            self._pages[index] = self._build_page(index)
            old = self.content.widget(index)
            self.content.insertWidget(index, self._pages[index])
            self.content.removeWidget(old)
            old.deleteLater()
        self.content.setCurrentIndex(index)
        self.top_bar.nav.button(index).setChecked(True)
        self.header.set_title(PAGES[index][0])

    def _build_page(self, index: int) -> QWidget:
        _label, module, cls_name = PAGES[index]
        page_cls = getattr(importlib.import_module(module), cls_name, None)
        if page_cls is None:
            return QWidget()  # page not implemented yet
        if getattr(page_cls, "USES_EVENT_LOG", False):
            return page_cls(self.link, self.event_log)
        return page_cls(self.link)

    def _on_telemetry(self, t: Telemetry) -> None:
        up = int(t.uptime_s)
        self.header.set_meta(f"{DEVICE_NAME}  ·  UP {up // 3600:02d}:{up // 60 % 60:02d}:{up % 60:02d}")

    def _tick(self) -> None:
        self.top_bar.clock.setText(time.strftime("%H:%M:%S"))

    def _toggle_maximized(self) -> None:
        # A maximized frameless window covers the whole screen, so on Windows Qt also flags it
        # WindowFullScreen; restore from either state (also from the Pi's kiosk full screen).
        if self._framed():
            self._normal_geometry = self.geometry()
            self.showMaximized()
        else:
            self.showNormal()
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)

    def _framed(self) -> bool:
        return not (self.isMaximized() or self.isFullScreen())

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.WindowStateChange:
            self.top_bar.max_btn.set_maximized(not self._framed())
            self._grip.setVisible(self._framed())
            self.layout().setContentsMargins(*([1] * 4 if self._framed() else [0] * 4))
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:
        self._grip.move(self.width() - self._grip.width(), self.height() - self._grip.height())
        self._grip.raise_()
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), T.BG)
        if self._framed():
            p.setPen(QPen(T.SHADOW, 1))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))
            # resize-grip marks
            p.setPen(QPen(T.ACCENT_DIM, 1.5))
            w, h = self.width(), self.height()
            for d in (5, 9):
                p.drawLine(w - d, h - 3, w - 3, h - d)

    def closeEvent(self, event) -> None:
        self._clock.stop()
        self.link.stop()
        self.event_log.close()
        super().closeEvent(event)
