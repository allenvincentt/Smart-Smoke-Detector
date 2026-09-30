"""Custom-painted HUD widgets (light neumorphic style).

Everything is drawn with QPainter (no QtCharts, no QGraphicsEffects) and repaints only when its
data changes, so the UI costs almost nothing between 2 Hz telemetry packets. Neumorphic soft
shadows are never redrawn per packet: the large widgets (panels, gauge, trend chart, floor plan)
keep them with their other slow-changing parts in a cached StaticLayer pixmap and paint opaquely,
and small widgets (buttons, tiles, rings) blit shared pixmaps from a bounded cache (neu_blit).
"""

import math
import time
from collections import deque
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPainterPathStroker, QPen,
    QPixmap, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from app.ui import theme as T


def round_path(r: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(r, radius, radius)
    return path


def soft_shadow(p: QPainter, path: QPainterPath, dx: float, dy: float, color, alpha: int, blur: float,
                fill: bool = True) -> None:
    """Blurred copy of `path` offset by (dx, dy). A few stacked strokes of growing width fake a
    Gaussian falloff; callers cache the result, so it is never redrawn per packet."""
    shifted = path.translated(dx, dy)
    steps = 4
    c = T.alpha(color, max(1, round(alpha / (steps + 1))))
    if fill:
        p.fillPath(shifted, c)
    p.setBrush(Qt.NoBrush)
    for i in range(1, steps + 1):
        p.setPen(QPen(c, 2 * blur * i / steps, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(shifted)


def rim(p: QPainter, path: QPainterPath, r: QRectF, top_left, bottom_right) -> None:
    """1 px edge light: `top_left` fading out across the shape into `bottom_right`."""
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, top_left)
    g.setColorAt(0.5, T.alpha(top_left, 0))
    g.setColorAt(1, bottom_right)
    p.setPen(QPen(QBrush(g), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def neu_raised(p: QPainter, path: QPainterPath, depth: float, blur: float | None = None, face=None,
               convex: bool = False) -> None:
    """Neumorphic raised shape: light halo top-left, shadow bottom-right, face on top."""
    blur = depth * 1.6 if blur is None else blur
    face = T.PANEL if face is None else face
    soft_shadow(p, path, -depth, -depth, T.HILITE, T.HILITE_A, blur)
    soft_shadow(p, path, depth, depth, T.SHADOW, T.SHADOW_A, blur)
    r = path.boundingRect()
    if convex:
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, face.lighter(104))
        g.setColorAt(1, face.darker(104))
        p.fillPath(path, g)
    else:
        p.fillPath(path, face)
    rim(p, path, r, T.alpha(T.HILITE, 230), T.alpha(T.SHADOW, 80))


def neu_inset(p: QPainter, path: QPainterPath, depth: float, blur: float | None = None, well=None) -> None:
    """Neumorphic pressed-in well: inner shadow top-left, inner highlight bottom-right."""
    blur = depth * 1.4 if blur is None else blur
    p.fillPath(path, T.GROOVE if well is None else well)
    r = path.boundingRect()
    pad = 2 * (depth + blur) + 2
    p.save()
    p.setClipPath(path)
    for d, color, a in ((depth, T.SHADOW, T.SHADOW_A), (-depth, T.HILITE, int(T.HILITE_A * 0.8))):
        outside = QPainterPath()
        outside.addRect(r.adjusted(-pad, -pad, pad, pad))
        soft_shadow(p, outside.subtracted(path.translated(d, d)), 0, 0, color, a, blur)
    p.restore()
    rim(p, path, r, T.alpha(T.SHADOW, 90), T.alpha(T.HILITE, 230))  # also hides the clip's hard edge


def draw_pill(p: QPainter, rect: QRectF, radius: float, color, glow: bool = True) -> None:
    """Filled, glowing status / selection pill (lit buttons, active nav, level chips)."""
    path = round_path(rect, radius)
    if glow:
        soft_shadow(p, path, 0, 2, color, 110, 3)
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    g.setColorAt(0, color.lighter(118))
    g.setColorAt(1, color)
    p.fillPath(path, g)
    rim(p, path, rect, T.alpha(T.HILITE, 150), T.alpha(color.darker(140), 120))


def _shape_path(shape: tuple) -> QPainterPath:
    kind, x, y, w, h, extra = shape
    if kind == "ring":  # annulus of thickness `extra` (odd-even fill leaves the hole open)
        path = QPainterPath()
        path.addEllipse(QRectF(x, y, w, h))
        path.addEllipse(QRectF(x + extra, y + extra, w - 2 * extra, h - 2 * extra))
        return path
    return round_path(QRectF(x, y, w, h), extra)


@lru_cache(maxsize=160)
def _neu_pixmap(w: int, h: int, dpr: float, shape: tuple, depth: float, blur, inset: bool,
                convex: bool) -> QPixmap:
    pm = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    path = _shape_path(shape)
    if inset:
        neu_inset(p, path, depth, blur)
    else:
        neu_raised(p, path, depth, blur, convex=convex)
    p.end()
    return pm


def neu_blit(p: QPainter, widget: QWidget, shape: tuple, depth: float, blur: float | None = None,
             inset: bool = False, convex: bool = False) -> None:
    """Draw a cached neumorphic shape for a small widget. `shape` is ("rect", x, y, w, h, radius)
    or ("ring", x, y, w, h, thickness). Pixmaps are shared between identical widgets and bounded
    by the LRU cache, so repaints (hover, state changes) never re-render the shadows."""
    shape = tuple(round(v, 1) if isinstance(v, float) else v for v in shape)
    p.drawPixmap(0, 0, _neu_pixmap(widget.width(), widget.height(), widget.devicePixelRatioF(), shape,
                                   depth, blur, inset, convex))


def draw_lamp(p: QPainter, c: QPointF, radius: float, color, lit: bool) -> None:
    """Glossy LED dome with a soft painted glow when lit."""
    p.setPen(Qt.NoPen)
    if lit:
        glow = QRadialGradient(c, radius * 2.6)
        glow.setColorAt(0, T.alpha(color, 140))
        glow.setColorAt(1, T.alpha(color, 0))
        p.setBrush(glow)
        p.drawEllipse(c, radius * 2.6, radius * 2.6)
    base = QColor(color if lit else T.OFF)
    base.setAlpha(255)
    ball = QRadialGradient(QPointF(c.x() - radius * 0.35, c.y() - radius * 0.4), radius * 1.4)
    ball.setColorAt(0, base.lighter(150 if lit else 115))
    ball.setColorAt(1, base.darker(108 if lit else 104))
    p.setPen(QPen(T.alpha(base.darker(125) if lit else T.SHADOW, 150), 1))
    p.setBrush(ball)
    p.drawEllipse(c, radius, radius)


def nice_ceil(v: float) -> float:
    step = 10 ** math.floor(math.log10(max(v, 1)))
    for m in (1, 2, 2.5, 5, 10):
        if m * step >= v:
            return m * step
    return 10 * step


class StaticLayer:
    """Cached pixmap of a widget's slow-changing background (panel fill, grid, scales, labels).

    `get()` rebuilds it only when the widget size or the caller's key changes. The pixmap is
    filled with the panel colour, so a widget that draws it first can set WA_OpaquePaintEvent and
    Qt stops repainting the HudPanel behind it on every update.
    """

    def __init__(self) -> None:
        self._key = None
        self._pixmap = None

    def get(self, widget: QWidget, key, paint) -> QPixmap:
        dpr = widget.devicePixelRatioF()
        full_key = (widget.width(), widget.height(), dpr, key)
        if full_key != self._key:
            # +1 device pixel: at fractional scaling (e.g. 125 %) an opaque widget can own one more
            # device column/row than width * dpr rounds to, which would otherwise stay unpainted.
            pm = QPixmap(math.ceil(widget.width() * dpr) + 1, math.ceil(widget.height() * dpr) + 1)
            pm.setDevicePixelRatio(dpr)
            pm.fill(T.PANEL)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            paint(p)
            p.end()
            self._key, self._pixmap = full_key, pm
        return self._pixmap


class SevenSegment:
    """Seven-segment digit renderer: lit segments glow, unlit ones show as faint ghosts."""

    SEGMENTS = {
        "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
        "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
    }
    WIDTH = 0.54   # digit width / height
    GAP = 0.2      # space between digits / height
    SKEW = 0.07    # slight italic, like a real LED meter

    @classmethod
    def width_for(cls, digits: int, height: float) -> float:
        return height * (digits * cls.WIDTH + (digits - 1) * cls.GAP)

    @staticmethod
    def _segment(x1: float, y1: float, x2: float, y2: float, t: float) -> QPolygonF:
        """Hexagonal bar from (x1, y1) to (x2, y2), horizontal or vertical, thickness t."""
        h = t / 2
        if y1 == y2:
            return QPolygonF([QPointF(x1, y1), QPointF(x1 + h, y1 - h), QPointF(x2 - h, y1 - h),
                              QPointF(x2, y1), QPointF(x2 - h, y1 + h), QPointF(x1 + h, y1 + h)])
        return QPolygonF([QPointF(x1, y1), QPointF(x1 + h, y1 + h), QPointF(x1 + h, y2 - h),
                          QPointF(x1, y2), QPointF(x1 - h, y2 - h), QPointF(x1 - h, y1 + h)])

    @classmethod
    def draw(cls, p: QPainter, text: str, origin: QPointF, height: float, color) -> None:
        t = max(2.0, height * 0.12)
        w = height * cls.WIDTH
        g = t * 0.18  # gap between neighbouring segments
        l, r = t / 2, w - t / 2
        top, mid, bot = t / 2, height / 2, height - t / 2
        shapes = {
            "a": cls._segment(l + g, top, r - g, top, t),
            "g": cls._segment(l + g, mid, r - g, mid, t),
            "d": cls._segment(l + g, bot, r - g, bot, t),
            "f": cls._segment(l, top + g, l, mid - g, t),
            "b": cls._segment(r, top + g, r, mid - g, t),
            "e": cls._segment(l, mid + g, l, bot - g, t),
            "c": cls._segment(r, mid + g, r, bot - g, t),
        }
        ghost = T.alpha(color, 22)
        glow = QPen(T.alpha(color, 70), t * 0.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.save()
        p.translate(origin)
        p.shear(-cls.SKEW, 0)
        p.translate(cls.SKEW * height, 0)
        for ch in text:
            lit = cls.SEGMENTS.get(ch, "")
            p.setPen(Qt.NoPen)
            p.setBrush(ghost)
            for name, poly in shapes.items():
                if name not in lit:
                    p.drawPolygon(poly)
            for name in lit:
                p.setPen(glow)
                p.setBrush(color)
                p.drawPolygon(shapes[name])
            p.translate(w + height * cls.GAP, 0)
        p.restore()


class HudPanel(QWidget):
    """Raised neumorphic card. Add children to `body`. The face is inset by MARGIN so its soft
    shadows fit inside the widget; the whole card is cached, so child updates cost one blit."""

    MARGIN = 7
    RADIUS = 16
    DEPTH = 3

    def __init__(self, tag: str = "", parent=None) -> None:
        super().__init__(parent)
        self._tag = tag
        self._tag_font = T.font(10, QFont.DemiBold, spacing=1.6)
        self._accent = T.ACCENT_DIM
        self._static = StaticLayer()
        self.setAttribute(Qt.WA_OpaquePaintEvent)  # the static layer paints every pixel
        m = self.MARGIN
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(m + 9, m + (22 if tag else 9), m + 9, m + 8)
        self.body.setSpacing(6)

    def set_accent(self, color) -> None:
        if color != self._accent:
            self._accent = color
            self.update()

    def _paint_static(self, p: QPainter) -> None:
        m = self.MARGIN
        face = QRectF(self.rect()).adjusted(m, m, -m, -m)
        neu_raised(p, round_path(face, self.RADIUS), self.DEPTH, 4)
        if self._tag:
            draw_lamp(p, QPointF(face.left() + 15, face.top() + 14), 3.5, self._accent, True)
            p.setFont(self._tag_font)
            p.setPen(T.TEXT_DIM)
            p.drawText(QPointF(face.left() + 25, face.top() + 18), self._tag)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static.get(self, QColor(self._accent).rgba(), self._paint_static))


class ArcGauge(QWidget):
    """270-degree gauge: zone track (clean / pre / crit), value arc, threshold markers."""

    def __init__(self, unit: str, parent=None) -> None:
        super().__init__(parent)
        self._unit = unit
        self._value = 0
        self._pre, self._crit = 250, 500
        self._color = T.ACCENT
        self._sub = ""
        self._note = ""
        self._valid = False
        self._f_unit = T.font(11, QFont.DemiBold, spacing=2)
        self._f_sub = T.font(13, mono=True)
        self._f_note = T.font(10, QFont.DemiBold, spacing=1.6)
        self._static = StaticLayer()
        self.setAttribute(Qt.WA_OpaquePaintEvent)  # the static layer paints every pixel
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, value: int, pre: int, crit: int, color, sub: str, note: str = "") -> None:
        new = (value, pre, crit, color, sub, note, True)
        if new != (self._value, self._pre, self._crit, self._color, self._sub, self._note, self._valid):
            (self._value, self._pre, self._crit, self._color, self._sub, self._note, self._valid) = new
            self.update()

    def clear(self) -> None:
        if self._valid:
            self._valid, self._sub, self._note, self._color = False, "", "", T.TEXT_DIM
            self.update()

    @staticmethod
    def _angle(f: float) -> float:
        return 225.0 - 270.0 * f

    def _geometry(self) -> tuple:
        side = min(self.width(), self.height()) - 6
        return side, self.width() / 2, self.height() / 2 + side * 0.05, side / 2 - 3

    def _arc(self, p: QPainter, cx: float, cy: float, radius: float, f0: float, f1: float, color,
             width: float, cap=Qt.FlatCap) -> None:
        p.setPen(QPen(color, width, Qt.SolidLine, cap))
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        p.drawArc(rect, int(self._angle(f0) * 16), int(-270 * (f1 - f0) * 16))

    def _groove(self, cx: float, cy: float, radius: float, width: float) -> QPainterPath:
        """Outline of the 270-degree value track, for the inset well."""
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        arc = QPainterPath()
        arc.arcMoveTo(rect, 225)
        arc.arcTo(rect, 225, -270)
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.RoundCap)
        return stroker.createStroke(arc).simplified()

    def _paint_static(self, p: QPainter) -> None:
        """Zone scale, inset value groove, centre knob and ticks: change only with size or thresholds."""
        _side, cx, cy, outer = self._geometry()
        vmax = nice_ceil(self._crit * 2)
        fp, fc = self._pre / vmax, self._crit / vmax
        self._arc(p, cx, cy, outer, 0, fp, T.alpha(T.GREEN, 170), 3)
        self._arc(p, cx, cy, outer, fp, fc, T.alpha(T.YELLOW, 190), 3)
        self._arc(p, cx, cy, outer, fc, 1, T.alpha(T.RED, 200), 3)
        neu_inset(p, self._groove(cx, cy, outer - 11, 14), 2, 3)
        knob = outer - 31
        if knob > 12:
            neu_raised(p, round_path(QRectF(cx - knob, cy - knob, 2 * knob, 2 * knob), knob), 4, 6, convex=True)
        p.setPen(QPen(T.alpha(T.TEXT_DIM, 170), 1))
        for i in range(41):
            a = math.radians(self._angle(i / 40))
            r1 = outer - 21
            r2 = r1 - (5 if i % 10 == 0 else 2.5)
            p.drawLine(QPointF(cx + r1 * math.cos(a), cy - r1 * math.sin(a)),
                       QPointF(cx + r2 * math.cos(a), cy - r2 * math.sin(a)))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(0, 0, self._static.get(self, (self._pre, self._crit), self._paint_static))
        p.setRenderHint(QPainter.Antialiasing)
        side, cx, cy, outer = self._geometry()
        vmax = nice_ceil(self._crit * 2)
        fp, fc = self._pre / vmax, self._crit / vmax

        # value arc
        val_r = outer - 11
        f = min(self._value / vmax, 1.0) if self._valid else 0.0
        if f > 0:
            self._arc(p, cx, cy, val_r, 0, f, T.alpha(self._color, 50), 14, Qt.RoundCap)  # glow
            self._arc(p, cx, cy, val_r, 0, f, self._color, 8, Qt.RoundCap)

        # threshold markers
        for frac, color in ((fp, T.YELLOW), (fc, T.RED)):
            a = math.radians(self._angle(frac))
            p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx + (outer + 2) * math.cos(a), cy - (outer + 2) * math.sin(a)),
                       QPointF(cx + (val_r - 6) * math.cos(a), cy - (val_r - 6) * math.sin(a)))

        # seven-segment readout (4 digits; 5 at the firmware's 10000 ppm ceiling)
        text = str(self._value) if self._valid else ""
        slots = max(4, len(text))
        text = text.rjust(slots) if self._valid else "-" * slots
        digit_h = min(side * 0.21, (val_r - 16) * 1.45 / SevenSegment.width_for(slots, 1.0))
        top = cy - digit_h * 0.78
        SevenSegment.draw(p, text, QPointF(cx - SevenSegment.width_for(slots, digit_h) / 2, top), digit_h,
                          self._color if self._valid else T.TEXT_DIM)
        below = top + digit_h + 6
        p.setFont(self._f_unit)
        p.setPen(T.TEXT_DIM)
        p.drawText(QRectF(cx - side / 2, below, side, 16), Qt.AlignCenter, self._unit)
        p.setFont(self._f_sub)
        p.setPen(T.ACCENT if self._valid else T.TEXT_DIM)
        p.drawText(QRectF(cx - side / 2, below + 16, side, 18), Qt.AlignCenter, self._sub)
        if self._note:
            p.setFont(self._f_note)
            p.setPen(T.BLUE)
            p.drawText(QRectF(cx - side / 2, cy + outer * 0.62, side, 16), Qt.AlignCenter, self._note)


class TrendChart(QWidget):
    """Rolling PPM trend (filled) with threshold bands and the MQ-2 AO voltage on a 0-5 V axis."""

    def __init__(self, ppm: deque, volts: deque, volts_max: float, sample_hz: int, parent=None) -> None:
        super().__init__(parent)
        self._ppm, self._volts = ppm, volts
        self._vmax = volts_max
        self._hz = sample_hz
        self._pre, self._crit = 250, 500
        self._color = T.ACCENT
        self._f_axis = T.font(10, mono=True)
        self._f_legend = T.font(10, QFont.DemiBold, spacing=1.4)
        self._static = StaticLayer()
        self.setAttribute(Qt.WA_OpaquePaintEvent)  # the static layer paints every pixel
        self.setMinimumSize(260, 140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_style(self, pre: int, crit: int, color) -> None:
        self._pre, self._crit, self._color = pre, crit, color

    def _plot(self) -> QRectF:
        return QRectF(44, 22, self.width() - 44 - 40, self.height() - 22 - 22)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        plot = self._plot()
        n = len(self._ppm)
        peak = max(self._ppm) if n else 0
        ymax = nice_ceil(max(self._crit * 1.25, peak * 1.1, 100))
        p.drawPixmap(0, 0, self._static.get(self, (self._pre, self._crit, ymax, self._color.rgba()),
                                            lambda sp: self._paint_static(sp, ymax)))
        if n < 2:
            return
        p.setRenderHint(QPainter.Antialiasing)
        dx = plot.width() / (self._ppm.maxlen - 1)
        x0 = plot.right() - (n - 1) * dx
        p.setClipRect(plot.adjusted(0, -2, 2, 2))

        volts = QPolygonF([QPointF(x0 + i * dx, plot.bottom() - min(v, self._vmax) / self._vmax * plot.height())
                           for i, v in enumerate(self._volts)])
        p.setPen(QPen(T.alpha(T.BLUE, 190), 1.2))
        p.drawPolyline(volts)

        line = QPolygonF([QPointF(x0 + i * dx, plot.bottom() - min(v, ymax) / ymax * plot.height())
                          for i, v in enumerate(self._ppm)])
        area = QPolygonF(line)
        area.append(QPointF(plot.right(), plot.bottom()))
        area.append(QPointF(x0, plot.bottom()))
        grad = QLinearGradient(0, plot.top(), 0, plot.bottom())
        grad.setColorAt(0, T.alpha(self._color, 110))
        grad.setColorAt(1, T.alpha(self._color, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        # The fill's only sloped edge lies under the antialiased line, so it needs no antialiasing
        # (the biggest single saving in this widget on the Pi's software rasterizer).
        p.setRenderHint(QPainter.Antialiasing, False)
        p.drawPolygon(area)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(self._color, 1.8))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(line)
        p.setClipping(False)
        draw_lamp(p, line.last(), 3.5, self._color, True)

    def _paint_static(self, p: QPainter, ymax: float) -> None:
        """Threshold bands, grid, axes and legend: change only with size, thresholds, scale or level."""
        plot = self._plot()
        cap = self._ppm.maxlen
        dx = plot.width() / (cap - 1)

        def y_ppm(v):
            return plot.bottom() - min(v, ymax) / ymax * plot.height()

        # inset well, threshold bands + grid
        neu_inset(p, round_path(plot.adjusted(-5, -5, 5, 5), 10), 2, 3)
        y_pre, y_crit = y_ppm(self._pre), y_ppm(self._crit)
        p.fillRect(QRectF(plot.left(), plot.top(), plot.width(), y_crit - plot.top()), T.alpha(T.RED, 20))
        p.fillRect(QRectF(plot.left(), y_crit, plot.width(), y_pre - y_crit), T.alpha(T.YELLOW, 16))
        p.setFont(self._f_axis)
        for k in range(5):
            yy = plot.top() + k * plot.height() / 4
            p.setPen(QPen(T.alpha(T.LINE, 220), 1))
            p.drawLine(QPointF(plot.left(), yy), QPointF(plot.right(), yy))
            p.setPen(T.TEXT_DIM)
            p.drawText(QRectF(0, yy - 7, plot.left() - 9, 14), Qt.AlignRight | Qt.AlignVCenter,
                       f"{int(ymax * (1 - k / 4))}")
            p.drawText(QRectF(plot.right() + 9, yy - 7, 31, 14), Qt.AlignLeft | Qt.AlignVCenter,
                       f"{self._vmax * (1 - k / 4):g}")
        per_min = 60 * self._hz
        for m in range(1, int((cap - 1) / per_min) + 1):
            x = plot.right() - m * per_min * dx
            p.setPen(QPen(T.alpha(T.LINE, 220), 1, Qt.DashLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(T.TEXT_DIM)
            p.drawText(QRectF(x - 20, plot.bottom() + 7, 40, 14), Qt.AlignCenter, f"-{m}m")

        for yy, color, label in ((y_pre, T.YELLOW, self._pre), (y_crit, T.RED, self._crit)):
            p.setPen(QPen(color, 1, Qt.DashLine))
            p.drawLine(QPointF(plot.left(), yy), QPointF(plot.right(), yy))
            p.setPen(color)
            p.drawText(QRectF(plot.left() + 4, yy - 14, 60, 13), Qt.AlignLeft | Qt.AlignVCenter, f"{label}")

        # legend
        p.setFont(self._f_legend)
        p.setPen(self._color)
        p.drawText(QPointF(plot.left(), 11), "PPM")
        p.setPen(T.BLUE)
        p.drawText(QRectF(plot.right() - 60, 0, 60 + 40, 14), Qt.AlignRight | Qt.AlignVCenter, "AO  V")


class RingIndicator(QWidget):
    """Countdown / progress ring with a centre readout and caption."""

    def __init__(self, caption: str, parent=None) -> None:
        super().__init__(parent)
        self._caption = caption
        self._frac, self._text, self._color = 0.0, "--", T.TEXT_DIM
        self._f_text = T.font(15, QFont.DemiBold)
        self._f_cap = T.font(10, QFont.DemiBold, spacing=1.6)
        self.setMinimumSize(70, 84)

    def set_state(self, frac: float, text: str, color) -> None:
        if (frac, text, color) != (self._frac, self._text, self._color):
            self._frac, self._text, self._color = frac, text, color
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height() - 18) - 8
        rect = QRectF((self.width() - side) / 2, 4, side, side)
        thick = 9.0
        neu_blit(p, self, ("ring", rect.x(), rect.y(), side, side, thick), 2, 2.5, inset=True)
        inner = rect.adjusted(thick + 5, thick + 5, -thick - 5, -thick - 5)
        if inner.width() > 16:
            neu_blit(p, self, ("rect", inner.x(), inner.y(), inner.width(), inner.height(), inner.width() / 2),
                     2, 3, convex=True)
        if self._frac > 0:
            track = rect.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2)
            p.setPen(QPen(self._color, 5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(track, 90 * 16, int(-360 * 16 * min(self._frac, 1.0)))
        px = max(10, int(side * (0.24 if len(self._text) <= 3 else 0.16)))
        if px != self._f_text.pixelSize():
            self._f_text.setPixelSize(px)
        p.setFont(self._f_text)
        p.setPen(self._color)
        p.drawText(rect, Qt.AlignCenter, self._text)
        p.setFont(self._f_cap)
        p.setPen(T.TEXT_DIM)
        p.drawText(QRectF(0, rect.bottom() + 3, self.width(), 14), Qt.AlignCenter, self._caption)


class StatusTile(QWidget):
    """Compact status readout: key, value and a lamp; tinted when active."""

    def __init__(self, key: str, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self._value, self._color, self._active = "--", T.TEXT_DIM, False
        self._f_key = T.font(10, QFont.DemiBold, spacing=1.6)
        self._f_val = T.font(15, QFont.DemiBold, spacing=1)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(170, 48)

    def set_key(self, key: str) -> None:
        if key != self._key:
            self._key = key
            self.update()

    def set_state(self, value: str, color, active: bool) -> None:
        if (value, color, active) != (self._value, self._color, self._active):
            self._value, self._color, self._active = value, color, active
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        shape = ("rect", r.x(), r.y(), r.width(), r.height(), 10.0)
        if self._active:  # pressed in and tinted while the output is on
            neu_blit(p, self, shape, 2, 2.5, inset=True)
            p.fillPath(round_path(r, 10), T.alpha(self._color, 28))
        else:
            neu_blit(p, self, shape, 2, 2.5, convex=True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color if self._active else T.OFF)
        p.drawRoundedRect(QRectF(r.left() + 7, r.top() + 8, 4, r.height() - 16), 2, 2)

        p.setFont(self._f_key)
        p.setPen(T.TEXT_DIM)
        p.drawText(QRectF(r.left() + 18, r.top() + 2, r.width() - 46, 15), Qt.AlignLeft | Qt.AlignVCenter,
                   self._key)
        p.setFont(self._f_val)
        p.setPen(self._color if self._active else T.TEXT)
        p.drawText(QRectF(r.left() + 18, r.top() + 15, r.width() - 46, r.height() - 16),
                   Qt.AlignLeft | Qt.AlignVCenter, self._value)
        draw_lamp(p, QPointF(r.right() - 16, r.center().y()), 5, self._color, self._active)


class ReadoutStrip(QWidget):
    """Row of small key/value telemetry readouts."""

    def __init__(self, keys: tuple, parent=None) -> None:
        super().__init__(parent)
        self._keys = keys
        self._values = [("--", T.TEXT_DIM)] * len(keys)
        self._f_key = T.font(10, QFont.DemiBold, spacing=1.4)
        self._f_val = T.font(16, QFont.DemiBold, mono=True)
        self.setFixedHeight(40)

    def set_values(self, values: list) -> None:
        if values != self._values:
            self._values = values
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        w = self.width() / len(self._keys)
        for i, (key, (text, color)) in enumerate(zip(self._keys, self._values)):
            x = i * w
            if i:  # engraved divider
                p.setPen(T.alpha(T.SHADOW, 120))
                p.drawLine(QPointF(x, 6), QPointF(x, self.height() - 6))
                p.setPen(T.HILITE)
                p.drawLine(QPointF(x + 1, 6), QPointF(x + 1, self.height() - 6))
            p.setFont(self._f_key)
            p.setPen(T.TEXT_DIM)
            p.drawText(QRectF(x + 8, 0, w - 10, 14), Qt.AlignLeft | Qt.AlignVCenter, key)
            p.setFont(self._f_val)
            p.setPen(color)
            p.drawText(QRectF(x + 8, 15, w - 10, 24), Qt.AlignLeft | Qt.AlignVCenter, text)


class SegmentBar(QWidget):
    """Segmented concentration bar on a 0..1.25 x CRIT scale, lit by zone, with PRE/CRIT ticks."""

    SEGMENTS = 50

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ppm, self._pre, self._crit, self._dim = 0, 250, 500, True
        self.setFixedHeight(20)

    def set_level(self, ppm: int, pre: int, crit: int, dim: bool) -> None:
        if (ppm, pre, crit, dim) != (self._ppm, self._pre, self._crit, self._dim):
            self._ppm, self._pre, self._crit, self._dim = ppm, pre, crit, dim
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        neu_blit(p, self, ("rect", r.x(), r.y(), r.width(), r.height(), r.height() / 2), 1.5, 2, inset=True)
        track = r.adjusted(7, 5, -7, -5)
        span = self._crit * 1.25
        w = track.width() / self.SEGMENTS
        lit_upto = self._ppm / span * self.SEGMENTS
        for i in range(self.SEGMENTS):
            at = (i + 0.5) / self.SEGMENTS * span
            zone = T.RED if at >= self._crit else T.YELLOW if at >= self._pre else T.GREEN
            if i < lit_upto:
                color = T.alpha(zone, 120) if self._dim else zone
            else:
                color = T.alpha(zone, 30)
            p.fillRect(QRectF(track.left() + i * w + 0.75, track.top(), w - 1.5, track.height()), color)
        for thr, color in ((self._pre, T.YELLOW), (self._crit, T.RED)):
            x = track.left() + thr / span * track.width()
            p.fillRect(QRectF(x - 1, r.top() + 1, 2, r.height() - 2), color)


class EventTicker(QWidget):
    """Most-recent-first list of firmware events."""

    def __init__(self, rows: int = 8, parent=None) -> None:
        super().__init__(parent)
        self._rows = deque(maxlen=rows)
        self._f_time = T.font(11, mono=True)
        self._f_text = T.font(12, QFont.DemiBold, spacing=0.8)
        self.setMinimumHeight(60)

    def add(self, text: str, color) -> None:
        self._rows.appendleft((time.strftime("%H:%M:%S"), text, color))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        row_h = 18
        for i, (stamp, text, color) in enumerate(self._rows):
            y = i * row_h
            if y + row_h > self.height():
                break
            fade = 255 if i == 0 else max(90, 230 - i * 22)
            p.setFont(self._f_time)
            p.setPen(T.alpha(T.TEXT_DIM, fade))
            p.drawText(QRectF(0, y, 64, row_h), Qt.AlignLeft | Qt.AlignVCenter, stamp)
            p.setFont(self._f_text)
            p.setPen(T.alpha(color, fade))
            text = p.fontMetrics().elidedText(text, Qt.ElideRight, self.width() - 66)
            p.drawText(QRectF(66, y, self.width() - 66, row_h), Qt.AlignLeft | Qt.AlignVCenter, text)


class SegmentReadout(QWidget):
    """Standalone seven-segment number with a unit caption."""

    def __init__(self, unit: str, digits: int = 4, parent=None) -> None:
        super().__init__(parent)
        self._unit, self._digits = unit, digits
        self._text, self._color = "-" * digits, T.TEXT_DIM
        self._f_unit = T.font(11, QFont.DemiBold, spacing=2)
        self.setMinimumHeight(56)

    def set_value(self, value, color) -> None:
        text = "-" * self._digits if value is None else str(value).rjust(self._digits)
        if (text, color) != (self._text, self._color):
            self._text, self._color = text, color
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        slots = len(self._text)
        per_h = SevenSegment.width_for(slots, 1.0) + SevenSegment.SKEW  # slant adds width
        h = min(self.height() - 20, 64, (self.width() - 16) / per_h)
        x = (self.width() - per_h * h) / 2
        SevenSegment.draw(p, self._text, QPointF(x, 2), h, self._color)
        p.setFont(self._f_unit)
        p.setPen(T.TEXT_DIM)
        p.drawText(QRectF(0, h + 4, self.width(), 14), Qt.AlignCenter, self._unit)


class Sparkline(QWidget):
    """Minimal trend line with PRE/CRIT threshold lines; no axes."""

    def __init__(self, data: deque, parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._pre, self._crit, self._color = 250, 500, T.ACCENT
        self.setMinimumHeight(40)

    def set_style(self, pre: int, crit: int, color) -> None:
        self._pre, self._crit, self._color = pre, crit, color

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        well = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        neu_blit(p, self, ("rect", well.x(), well.y(), well.width(), well.height(), 10.0), 2, 2.5, inset=True)
        r = well.adjusted(6, 7, -6, -5)
        n = len(self._data)
        ymax = max(self._crit * 1.25, (max(self._data) * 1.1) if n else 0)

        def y(v):
            return r.bottom() - min(v, ymax) / ymax * r.height()

        for thr, color in ((self._pre, T.YELLOW), (self._crit, T.RED)):
            p.setPen(QPen(T.alpha(color, 150), 1, Qt.DashLine))
            p.drawLine(QPointF(r.left(), y(thr)), QPointF(r.right(), y(thr)))
        if n < 2 or not self.isEnabled():
            return
        dx = r.width() / (self._data.maxlen - 1)
        x0 = r.right() - (n - 1) * dx
        line = QPolygonF([QPointF(x0 + i * dx, y(v)) for i, v in enumerate(self._data)])
        area = QPolygonF(line)
        area.append(QPointF(r.right(), r.bottom()))
        area.append(QPointF(x0, r.bottom()))
        grad = QLinearGradient(0, r.top(), 0, r.bottom())
        grad.setColorAt(0, T.alpha(self._color, 90))
        grad.setColorAt(1, T.alpha(self._color, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPolygon(area)
        p.setPen(QPen(self._color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(line)


class HudButton(QAbstractButton):
    """Painted push button. `set_lit` marks the state the ESP32 reports as active (e.g. the fan
    mode); it follows telemetry, never the click itself."""

    HEIGHT = 38
    MARGIN = 5  # room around the pill for its shadows

    def __init__(self, text: str, color=T.ACCENT, px: int = 12, parent=None) -> None:
        super().__init__(parent)
        self.setText(text)
        self._color = color
        self._lit = False
        self._font = T.font(px, QFont.Bold, spacing=1.6)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)  # repaint on hover enter/leave
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(QFontMetrics(self._font).horizontalAdvance(self.text()) + 38, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(QFontMetrics(self._font).horizontalAdvance(self.text()) + 24, self.HEIGHT)

    def set_state(self, text: str, color, lit: bool) -> None:
        if (text, color, lit) != (self.text(), self._color, self._lit):
            self._color, self._lit = color, lit
            self.setText(text)
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        m = self.MARGIN
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        radius = min(r.height() / 2, 14.0)
        shape = ("rect", r.x(), r.y(), r.width(), r.height(), radius)
        c = self._color
        if not self.isEnabled():
            neu_blit(p, self, shape, 1, 1.5)
            text = T.alpha(T.TEXT_DIM, 140)
        elif self.isDown():
            neu_blit(p, self, shape, 2, 2.5, inset=True)
            if self._lit:
                p.fillPath(round_path(r, radius), T.alpha(c, 200))
            text = T.ON_COLOR if self._lit else c
        elif self._lit:
            draw_pill(p, r, radius, c)
            text = T.ON_COLOR
        else:
            neu_blit(p, self, shape, 2.5, 3, convex=True)
            if self.underMouse():
                p.fillPath(round_path(r, radius), T.alpha(c, 22))
            text = c
        p.setFont(self._font)
        p.setPen(text)
        p.drawText(r, Qt.AlignCenter, self.text())


class HudStepper(QWidget):
    """Touch-friendly [-] value [+] entry. Hold a button to repeat. `edited` fires on user steps only."""

    edited = Signal(int)

    def __init__(self, key: str, lo: int, hi: int, step: int, parent=None) -> None:
        super().__init__(parent)
        self._key, self._lo, self._hi, self._step = key, lo, hi, step
        self._value, self._color = lo, T.TEXT_DIM
        self._f_key = T.font(10, QFont.DemiBold, spacing=1.6)
        self._f_val = T.font(17, QFont.DemiBold, mono=True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._buttons = []
        for i, (glyph, sign) in enumerate((("−", -1), ("+", 1))):
            btn = HudButton(glyph, px=20)
            btn.setFixedWidth(34)
            btn.setAutoRepeat(True)
            btn.setAutoRepeatDelay(400)
            btn.setAutoRepeatInterval(60)
            btn.clicked.connect(lambda _checked=False, s=sign: self._step_by(s))
            self._buttons.append(btn)
            if i:
                row.addStretch(1)
            row.addWidget(btn)
        self.setFixedHeight(HudButton.HEIGHT)
        self.setMinimumWidth(130)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int, color) -> None:
        value = max(self._lo, min(self._hi, value))
        if (value, color) != (self._value, self._color):
            self._value, self._color = value, color
            self.update()

    def _step_by(self, sign: int) -> None:
        value = max(self._lo, min(self._hi, self._value + sign * self._step))
        if value != self._value:
            self._value = value
            self.update()
            self.edited.emit(value)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(36, 4, self.width() - 72, self.height() - 8)
        neu_blit(p, self, ("rect", r.x(), r.y(), r.width(), r.height(), 10.0), 2, 2.5, inset=True)
        p.setFont(self._f_key)
        p.setPen(T.TEXT_DIM)
        p.drawText(r.adjusted(8, 3, 0, 0), Qt.AlignLeft | Qt.AlignTop, self._key)
        p.setFont(self._f_val)
        p.setPen(self._color if self.isEnabled() else T.TEXT_DIM)
        p.drawText(r.adjusted(0, 0, -7, 0), Qt.AlignRight | Qt.AlignBottom, str(self._value))


class FanGlyph(QWidget):
    """Exhaust fan: housing ring, three blades and hub; lit while the relay is on."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._on, self._color = False, T.OFF
        self.setMinimumSize(64, 64)

    def set_state(self, on: bool, color) -> None:
        if (on, color) != (self._on, self._color):
            self._on, self._color = on, color
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        housing = min(self.width(), self.height()) / 2 - 8
        c = QPointF(self.width() / 2, self.height() / 2)
        neu_blit(p, self, ("rect", c.x() - housing, c.y() - housing, 2 * housing, 2 * housing, housing),
                 3, 4, convex=True)
        radius = housing * 0.8
        neu_blit(p, self, ("rect", c.x() - radius, c.y() - radius, 2 * radius, 2 * radius, radius), 2, 3,
                 inset=True)
        lit = self._on and self.isEnabled()
        color = self._color if lit else T.OFF
        if lit:
            glow = QRadialGradient(c, radius)
            glow.setColorAt(0, T.alpha(color, 90))
            glow.setColorAt(1, T.alpha(color, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(c, radius, radius)
            p.setPen(QPen(T.alpha(color, 200), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, radius - 1, radius - 1)
        radius *= 0.92
        p.setPen(Qt.NoPen)
        p.setBrush(T.alpha(color, 220) if lit else T.alpha(T.SHADOW, 110))
        for angle in (15, 135, 255):
            p.save()
            p.translate(c)
            p.rotate(angle)
            p.drawEllipse(QRectF(radius * 0.14, -radius * 0.2, radius * 0.7, radius * 0.4))
            p.restore()
        draw_lamp(p, c, radius * 0.15, color, lit)
