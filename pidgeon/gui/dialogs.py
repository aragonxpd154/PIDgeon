"""Diálogos: abrir fluxo de rede, gravação, sobre."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFormLayout, QComboBox, QLineEdit, QSpinBox, QDialogButtonBox,
                               QVBoxLayout, QLabel, QHBoxLayout, QWidget)

from ..core.sources import list_interfaces, SourceSpec


class NetworkDialog(QDialog):
    def __init__(self, parent=None, recent=None):
        super().__init__(parent)
        self.setWindowTitle("Abrir fluxo de rede")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.recent = QComboBox()
        self.recent.addItem("— recentes —")
        for r in recent or []:
            if r.lower().startswith(("udp://", "rtp://", "http")):
                self.recent.addItem(r)
        self.recent.currentIndexChanged.connect(self._load_recent)
        form.addRow("Recentes", self.recent)
        self.proto = QComboBox()
        self.proto.addItems(["UDP / RTP (detecção automática)", "UDP puro", "RTP", "HTTP"])
        form.addRow("Protocolo", self.proto)
        self.addr = QLineEdit("239.1.1.1")
        self.addr.setPlaceholderText("grupo multicast (239.x.x.x) ou IP local para unicast (0.0.0.0 = todas)")
        form.addRow("Endereço", self.addr)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1234)
        form.addRow("Porta", self.port)
        self.iface = QComboBox()
        self.iface.addItem("Padrão do sistema", "")
        for n, ip in list_interfaces():
            self.iface.addItem("%s — %s" % (n, ip), ip)
        form.addRow("Interface de rede", self.iface)
        self.source = QLineEdit()
        self.source.setPlaceholderText("opcional — IP da fonte para multicast SSM (IGMPv3)")
        form.addRow("Fonte (SSM)", self.source)
        self.buffer = QSpinBox()
        self.buffer.setRange(1, 256)
        self.buffer.setValue(16)
        self.buffer.setSuffix(" MB")
        form.addRow("Buffer do socket", self.buffer)
        self.url = QLineEdit()
        self.url.setPlaceholderText("http://servidor/stream.ts")
        form.addRow("URL HTTP", self.url)
        lay.addLayout(form)
        self.preview = QLabel()
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview.setStyleSheet("font-family: monospace; padding: 6px;")
        lay.addWidget(self.preview)
        hint = QLabel("O tamanho do pacote (188 / 204 BTS / 192) e o encapsulamento RTP são detectados "
                      "automaticamente. Para multicast, o PIDgeon faz o IGMP join na interface escolhida.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Abrir")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.addr, self.source, self.url):
            w.textChanged.connect(self._update)
        for w in (self.port, self.buffer):
            w.valueChanged.connect(self._update)
        for w in (self.proto, self.iface):
            w.currentIndexChanged.connect(self._update)
        self._update()

    def _load_recent(self, idx):
        if idx <= 0:
            return
        txt = self.recent.currentText()
        try:
            sp = SourceSpec(txt)
        except ValueError:
            return
        if sp.kind == "http":
            self.proto.setCurrentIndex(3)
            self.url.setText(sp.url)
            return
        self.proto.setCurrentIndex(2 if sp.rtp == "on" else (1 if sp.rtp == "off" else 0))
        self.addr.setText(sp.group)
        self.port.setValue(sp.port)
        self.source.setText(sp.source or "")
        self.buffer.setValue(sp.buffer_mb)
        if sp.iface:
            i = self.iface.findData(sp.iface)
            if i >= 0:
                self.iface.setCurrentIndex(i)

    def value(self):
        if self.proto.currentIndex() == 3:
            return self.url.text().strip()
        scheme = "rtp" if self.proto.currentIndex() == 2 else "udp"
        src = self.source.text().strip()
        host = self.addr.text().strip() or "0.0.0.0"
        url = "%s://%s%s:%d" % (scheme, (src + "@") if src else "", host, self.port.value())
        q = []
        if self.iface.currentData():
            q.append("iface=%s" % self.iface.currentData())
        if self.proto.currentIndex() == 1:
            q.append("rtp=off")
        if self.buffer.value() != 16:
            q.append("buffer=%d" % self.buffer.value())
        if q:
            url += "?" + "&".join(q)
        return url

    def _update(self):
        http = self.proto.currentIndex() == 3
        for w in (self.addr, self.port, self.iface, self.source, self.buffer):
            w.setEnabled(not http)
        self.url.setEnabled(http)
        self.preview.setText(self.value())


class AboutDialog(QDialog):
    def __init__(self, parent=None, version=""):
        super().__init__(parent)
        self.setWindowTitle("Sobre o PIDgeon")
        lay = QVBoxLayout(self)
        t = QLabel("<h2>PIDgeon %s</h2>"
                   "<p>Analisador de MPEG Transport Stream e ISDB-Tb para Linux.</p>"
                   "<p>TS 188 · BTS 204 · M2TS 192 · UDP/RTP multicast e unicast · PSI/SI · "
                   "TR 101 290 · camadas ISDB-T e IIP · vídeo ao vivo · gravação.</p>"
                   "<p>Autor: Marcos Santos — github.com/aragonxpd154<br>Licença GPL-3.0.</p>" % version)
        t.setWordWrap(True)
        t.setTextFormat(Qt.RichText)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


_ = (QHBoxLayout, QWidget)
