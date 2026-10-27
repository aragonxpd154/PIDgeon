"""Widgets próprios: gráfico de tendência, área de vídeo e medidor de áudio."""

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QImage, QFont, QPolygonF, QLinearGradient
from PySide6.QtWidgets import QWidget, QSizePolicy

from . import theme


def fmt_bps(v):
    if v is None:
        return "-"
    if v >= 1e6:
        return "%.2f Mbps" % (v / 1e6)
    if v >= 1e3:
        return "%.1f kbps" % (v / 1e3)
    return "%.0f bps" % v


def fmt_int(v):
    return "{:,}".format(int(v)).replace(",", ".")


class TraceGraph(QWidget):
    """Gráfico de linhas com fundo escuro (estilo 'Trace bar').

    series: lista de (nome, cor, sequência de valores)
    """

    def __init__(self, parent=None, unit="bps", bars=False, min_h=90):
        super().__init__(parent)
        self.series = []
        self.unit = unit
        self.bars = bars
        self.fixed_max = None
        self.symmetric = False
        self.title = ""
        self.setMinimumHeight(min_h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_series(self, series, title=None):
        self.series = series
        if title is not None:
            self.title = title
        self.update()

    def _fmt(self, v):
        if self.unit == "bps":
            if abs(v) >= 1e6:
                return "%.1fMbps" % (v / 1e6)
            if abs(v) >= 1e3:
                return "%.0fkbps" % (v / 1e3)
            return "%.0fbps" % v
        if self.unit == "":
            return "%g" % v
        return "%.3g %s" % (v, self.unit)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.CURRENT
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(c["graph_bg"]))
        left, top, right, bottom = 74, 8, 8, 16
        gw, gh = w - left - right, h - top - bottom
        if gw < 10 or gh < 10:
            return
        vals = [v for _, _, s in self.series for v in s if v is not None]
        vmax = max(vals) if vals else 1.0
        vmin = min(vals) if vals else 0.0
        if self.fixed_max is not None:
            vmax = max(self.fixed_max, vmax)
        if self.symmetric:
            m = max(abs(vmax), abs(vmin), 1e-9)
            vmax, vmin = m * 1.15, -m * 1.15
        else:
            vmin = min(0.0, vmin)
            vmax = vmax * 1.15 if vmax > 0 else 1.0
        rng = (vmax - vmin) or 1.0
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        grid = QPen(QColor(c["graph_grid"]), 1, Qt.DotLine)
        txt = QColor("#9fb3c8")
        for i in range(5):
            val = vmin + rng * i / 4
            y = top + gh - (val - vmin) / rng * gh
            p.setPen(grid)
            p.drawLine(QPointF(left, y), QPointF(left + gw, y))
            p.setPen(txt)
            p.drawText(QRectF(0, y - 8, left - 6, 16), Qt.AlignRight | Qt.AlignVCenter, self._fmt(val))
        p.setPen(QPen(QColor("#40506a"), 1))
        p.drawLine(QPointF(left, top), QPointF(left, top + gh))
        n = max((len(s) for _, _, s in self.series), default=0)
        npts = max(n, 60)
        dx = gw / max(1, npts - 1)
        for name, col, s in self.series:
            s = list(s)
            if not s:
                continue
            x0 = left + gw - (len(s) - 1) * dx
            if self.bars:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(col))
                for i, v in enumerate(s):
                    if v:
                        x = x0 + i * dx
                        y = top + gh - (v - vmin) / rng * gh
                        p.drawRect(QRectF(x - max(1, dx * 0.4), y, max(2, dx * 0.8), top + gh - y))
                continue
            # Constrói o polígono de uma vez: QPolygonF.append() quebra o refcount do None
            # no PySide6 6.12 com Python 3.10/3.11 (crash "none_dealloc").
            poly = QPolygonF([QPointF(x0 + i * dx, top + gh - (v - vmin) / rng * gh)
                              for i, v in enumerate(s) if v is not None])
            p.setPen(QPen(QColor(col), 1.6))
            p.drawPolyline(poly)
        # legenda
        x = left + 8
        for name, col, s in self.series:
            if not name:
                continue
            last = next((v for v in reversed(list(s)) if v is not None), None)
            label = "%s %s" % (name, self._fmt(last) if last is not None else "")
            p.setPen(QColor(col))
            p.drawText(QPointF(x, top + 12), label)
            x += p.fontMetrics().horizontalAdvance(label) + 18
        if self.title:
            p.setPen(txt)
            p.drawText(QRectF(left, top, gw - 6, 14), Qt.AlignRight, self.title)


class VideoWidget(QWidget):
    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None
        self.message = "Selecione um programa e clique em ▶ Reproduzir"
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def set_frame(self, data, w, h, stride):
        self.img = QImage(data, w, h, stride, QImage.Format_RGB888).copy()
        self.update()

    def clear(self, message=None):
        self.img = None
        if message is not None:
            self.message = message
        self.update()

    def sizeHint(self):
        return QSize(960, 540)

    def mouseDoubleClickEvent(self, ev):
        self.doubleClicked.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.img is None:
            p.setPen(QColor("#8a99ad"))
            p.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        iw, ih = self.img.width(), self.img.height()
        W, H = self.width(), self.height()
        sc = min(W / iw, H / ih)
        tw, th = iw * sc, ih * sc
        p.drawImage(QRectF((W - tw) / 2, (H - th) / 2, tw, th), self.img)


class LevelMeter(QWidget):
    """Medidor de pico estéreo com retenção."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lv = [0.0, 0.0]
        self.peak = [0.0, 0.0]
        self.setFixedWidth(22)

    def set_levels(self, l, r):
        for i, v in enumerate((l, r)):
            self.lv[i] = max(v, self.lv[i] * 0.7)
            self.peak[i] = max(v, self.peak[i] * 0.97)
        self.update()

    def decay(self):
        self.lv = [v * 0.6 for v in self.lv]
        self.peak = [v * 0.95 for v in self.peak]
        self.update()

    def paintEvent(self, ev):
        import math
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0f14"))
        h = self.height() - 4
        bw = (self.width() - 6) / 2
        g = QLinearGradient(0, self.height(), 0, 0)
        g.setColorAt(0.0, QColor("#2f9e44"))
        g.setColorAt(0.7, QColor("#e8c10c"))
        g.setColorAt(1.0, QColor("#d9363e"))
        for i in range(2):
            v = self.lv[i]
            db = 20 * math.log10(v) if v > 1e-5 else -100
            frac = max(0.0, min(1.0, (db + 60) / 60))
            x = 2 + i * (bw + 2)
            p.fillRect(QRectF(x, 2 + h * (1 - frac), bw, h * frac), g)
            pk = self.peak[i]
            pdb = 20 * math.log10(pk) if pk > 1e-5 else -100
            pf = max(0.0, min(1.0, (pdb + 60) / 60))
            p.fillRect(QRectF(x, 2 + h * (1 - pf), bw, 2), QColor("#ffffff"))
