"""Ícones desenhados em código (sem arquivos externos)."""

import math

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QPolygonF, QPainterPath, QFont

_CACHE = {}

COL = {
    "table": "#3b6db3", "video": "#c2410c", "audio": "#7c3aed", "clock": "#0f766e",
    "data": "#64748b", "null": "#a3a3a3", "isdb": "#0891b2", "unknown": "#94a3b8",
}


def _pm(size=32):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    return pm


def icon(name, size=32, color=None):
    key = (name, size, color)
    if key in _CACHE:
        return _CACHE[key]
    pm = _pm(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    c = QColor(color or COL.get(name, "#2b6cb0"))
    pen = QPen(c, max(1.5, s / 14))
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = s * 0.12
    r = QRectF(m, m, s - 2 * m, s - 2 * m)
    if name == "table":
        p.drawRoundedRect(r, 2, 2)
        p.drawLine(QPointF(r.left(), r.top() + r.height() / 3), QPointF(r.right(), r.top() + r.height() / 3))
        p.drawLine(QPointF(r.left() + r.width() / 3, r.top() + r.height() / 3), QPointF(r.left() + r.width() / 3, r.bottom()))
    elif name == "video":
        p.setBrush(QBrush(c.lighter(160)))
        p.drawRoundedRect(QRectF(m, s * 0.28, s * 0.58, s * 0.44), 2, 2)
        p.drawPolygon(QPolygonF([QPointF(s * 0.7, s * 0.5), QPointF(s - m, s * 0.3), QPointF(s - m, s * 0.7)]))
    elif name == "audio":
        p.drawLine(QPointF(s * 0.38, s * 0.72), QPointF(s * 0.38, s * 0.2))
        p.drawLine(QPointF(s * 0.38, s * 0.2), QPointF(s * 0.78, s * 0.14))
        p.drawLine(QPointF(s * 0.78, s * 0.14), QPointF(s * 0.78, s * 0.64))
        p.setBrush(c)
        p.drawEllipse(QPointF(s * 0.3, s * 0.74), s * 0.1, s * 0.08)
        p.drawEllipse(QPointF(s * 0.7, s * 0.66), s * 0.1, s * 0.08)
    elif name == "clock":
        p.drawEllipse(r)
        p.drawLine(QPointF(s / 2, s / 2), QPointF(s / 2, s * 0.25))
        p.drawLine(QPointF(s / 2, s / 2), QPointF(s * 0.7, s * 0.6))
    elif name == "data":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s * 0.25))
        p.drawLine(QPointF(m, m + s * 0.12), QPointF(m, s - m - s * 0.12))
        p.drawLine(QPointF(s - m, m + s * 0.12), QPointF(s - m, s - m - s * 0.12))
        p.drawArc(QRectF(m, s - m - s * 0.25, s - 2 * m, s * 0.25), 180 * 16, 180 * 16)
    elif name == "null":
        p.drawEllipse(r)
        p.drawLine(QPointF(r.left() + 3, r.bottom() - 3), QPointF(r.right() - 3, r.top() + 3))
    elif name == "isdb":
        p.drawLine(QPointF(s / 2, s - m), QPointF(s / 2, s * 0.45))
        for k in (0.18, 0.3):
            p.drawArc(QRectF(s / 2 - s * k, s * 0.45 - s * k, 2 * s * k, 2 * s * k), 30 * 16, 120 * 16)
    elif name in ("play", "stop", "pause", "prev", "next"):
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        if name == "play":
            p.drawPolygon(QPolygonF([QPointF(s * 0.28, s * 0.18), QPointF(s * 0.82, s * 0.5), QPointF(s * 0.28, s * 0.82)]))
        elif name == "stop":
            p.drawRoundedRect(QRectF(s * 0.22, s * 0.22, s * 0.56, s * 0.56), 3, 3)
        elif name == "pause":
            p.drawRect(QRectF(s * 0.24, s * 0.2, s * 0.18, s * 0.6))
            p.drawRect(QRectF(s * 0.58, s * 0.2, s * 0.18, s * 0.6))
        elif name == "prev":
            p.drawRect(QRectF(s * 0.18, s * 0.2, s * 0.1, s * 0.6))
            p.drawPolygon(QPolygonF([QPointF(s * 0.8, s * 0.2), QPointF(s * 0.32, s * 0.5), QPointF(s * 0.8, s * 0.8)]))
        else:
            p.drawRect(QRectF(s * 0.72, s * 0.2, s * 0.1, s * 0.6))
            p.drawPolygon(QPolygonF([QPointF(s * 0.2, s * 0.2), QPointF(s * 0.68, s * 0.5), QPointF(s * 0.2, s * 0.8)]))
    elif name == "record":
        p.setBrush(QColor("#d9363e"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(s * 0.2, s * 0.2, s * 0.6, s * 0.6))
    elif name == "file":
        path = QPainterPath()
        path.moveTo(s * 0.25, m)
        path.lineTo(s * 0.62, m)
        path.lineTo(s * 0.78, s * 0.3)
        path.lineTo(s * 0.78, s - m)
        path.lineTo(s * 0.25, s - m)
        path.closeSubpath()
        p.setBrush(QColor("#ffffff"))
        p.drawPath(path)
        p.drawText(QRectF(0, s * 0.35, s, s * 0.5), Qt.AlignCenter, "TS")
    elif name == "network":
        p.drawEllipse(r)
        p.drawEllipse(QRectF(s * 0.36, m, s * 0.28, s - 2 * m))
        p.drawLine(QPointF(m, s / 2), QPointF(s - m, s / 2))
    elif name == "fullscreen":
        for (x, y, dx, dy) in ((m, m, 1, 1), (s - m, m, -1, 1), (m, s - m, 1, -1), (s - m, s - m, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * s * 0.25, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * s * 0.25))
    elif name == "save":
        p.drawRoundedRect(r, 2, 2)
        p.drawRect(QRectF(s * 0.32, m, s * 0.36, s * 0.26))
        p.drawRect(QRectF(s * 0.26, s * 0.55, s * 0.48, s * 0.3))
    elif name == "reset":
        p.drawArc(r, 60 * 16, 290 * 16)
        p.drawLine(QPointF(s * 0.68, s * 0.12), QPointF(s * 0.72, s * 0.32))
        p.drawLine(QPointF(s * 0.72, s * 0.32), QPointF(s * 0.52, s * 0.3))
    elif name == "chart":
        p.drawLine(QPointF(m, s - m), QPointF(s - m, s - m))
        p.drawLine(QPointF(m, s - m), QPointF(m, m))
        p.drawPolyline(QPolygonF([QPointF(m + 2, s * 0.7), QPointF(s * 0.4, s * 0.45), QPointF(s * 0.6, s * 0.6), QPointF(s - m, s * 0.25)]))
    elif name == "speaker":
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(m, s * 0.38), QPointF(s * 0.3, s * 0.38), QPointF(s * 0.52, s * 0.2),
                                 QPointF(s * 0.52, s * 0.8), QPointF(s * 0.3, s * 0.62), QPointF(m, s * 0.62)]))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(s * 0.45, s * 0.3, s * 0.3, s * 0.4), -60 * 16, 120 * 16)
    elif name == "theme":
        p.drawEllipse(r)
        p.setBrush(c)
        p.drawPie(r, 90 * 16, 180 * 16)
    elif name == "logo":
        _logo(p, s)
    else:
        p.drawEllipse(r)
    p.end()
    ic = QIcon(pm)
    _CACHE[key] = ic
    return ic


def _logo(p, s):
    """Pombo estilizado com ondas — logotipo do PIDgeon."""
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#2b6cb0"))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
    p.setBrush(QColor("#ffffff"))
    body = QPainterPath()
    body.moveTo(s * 0.18, s * 0.62)
    body.cubicTo(s * 0.25, s * 0.35, s * 0.55, s * 0.3, s * 0.68, s * 0.36)
    body.cubicTo(s * 0.74, s * 0.26, s * 0.84, s * 0.26, s * 0.86, s * 0.34)
    body.lineTo(s * 0.94, s * 0.38)
    body.lineTo(s * 0.85, s * 0.41)
    body.cubicTo(s * 0.86, s * 0.6, s * 0.65, s * 0.74, s * 0.42, s * 0.72)
    body.lineTo(s * 0.18, s * 0.62)
    p.drawPath(body)
    p.setBrush(QColor("#2b6cb0"))
    p.drawEllipse(QPointF(s * 0.8, s * 0.33), s * 0.025, s * 0.025)
    pen = QPen(QColor("#9cd1ff"), s * 0.045)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    for k in (0.08, 0.15):
        p.drawArc(QRectF(s * 0.30 - s * k, s * 0.34 - s * k, 2 * s * k, 2 * s * k), 95 * 16, 80 * 16)
    wing = QPen(QColor("#2b6cb0"), s * 0.03)
    wing.setCapStyle(Qt.RoundCap)
    p.setPen(wing)
    p.drawArc(QRectF(s * 0.36, s * 0.42, s * 0.3, s * 0.2), 200 * 16, 120 * 16)


def lamp_pixmap(color, size=12):
    pm = _pm(size)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    p.setBrush(c)
    p.setPen(QPen(c.darker(140), 1))
    p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    p.end()
    return pm


def app_icon():
    ic = QIcon()
    for sz in (16, 32, 48, 64, 128, 256):
        ic.addPixmap(icon("logo", sz).pixmap(sz, sz))
    return ic


_ = (math, QFont)
