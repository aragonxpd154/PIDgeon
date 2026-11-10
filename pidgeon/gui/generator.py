"""Janela "Gerar / Transmitir": BTS 204, BTS 188, TS 188 e TS 204 a partir de uma gravação,
para arquivo e/ou rede (UDP puro, RTP, RTP + FEC SMPTE 2022-1 — unicast ou multicast)."""

import os
import threading

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QSettings
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QPushButton, QComboBox, QSpinBox, QCheckBox, QFileDialog, QProgressBar,
                               QPlainTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QWidget,
                               QMessageBox, QDoubleSpinBox)

from ..core.convert import Generator, GenConfig, FORMATS, SOURCE_KINDS, probe, needs_remux, LAYER_LETTER
from ..core.netout import NetConfig, PROTOCOLS
from ..core.remux import IsdbParams, Layer
from ..core.names import ISDB_GUARD, ISDB_MODULATION, ISDB_CODERATE, ISDB_TIME_INTERLEAVE
from ..core.sources import is_multicast
from .netwidgets import InterfaceCombo
from .icons import icon

EXT = {"bts204": ".bts", "bts188": ".bts188.ts", "ts188": ".ts", "ts204": ".ts204", "original": ".ts"}


class _Bridge(QObject):
    log = Signal(str)
    done = Signal(str)


class LayerRow:
    def __init__(self, grid, row, name, on_change):
        self.name = name
        self.active = QCheckBox("Camada %s" % name)
        self.mod = QComboBox()
        for k in (0, 1, 2, 3):
            self.mod.addItem(ISDB_MODULATION[k], k)
        self.cr = QComboBox()
        for k in (0, 1, 2, 3, 4):
            self.cr.addItem(ISDB_CODERATE[k], k)
        self.segs = QSpinBox()
        self.segs.setRange(1, 13)
        self.ti = QComboBox()
        for k in (0, 1, 2, 3):
            self.ti.addItem("", k)
        self.cap = QLabel()
        self.cap.setMinimumWidth(170)
        for c, w in enumerate((self.active, self.mod, self.cr, self.segs, self.ti, self.cap)):
            grid.addWidget(w, row, c)
        self.active.toggled.connect(on_change)
        for w in (self.mod, self.cr, self.ti):
            w.currentIndexChanged.connect(on_change)
        self.segs.valueChanged.connect(on_change)

    def set_mode(self, mode):
        for i in range(4):
            self.ti.setItemText(i, "I = %d" % ISDB_TIME_INTERLEAVE[i][mode])

    def set(self, layer):
        self.active.setChecked(layer is not None)
        if layer:
            self.mod.setCurrentIndex(self.mod.findData(layer.mod))
            self.cr.setCurrentIndex(self.cr.findData(layer.cr))
            self.segs.setValue(layer.segs)
            self.ti.setCurrentIndex(self.ti.findData(layer.ti))

    def get(self):
        if not self.active.isChecked():
            return None
        return Layer(self.mod.currentData(), self.cr.currentData(), self.ti.currentData(), self.segs.value())

    def enable(self, on):
        self.active.setEnabled(on)
        act = on and self.active.isChecked()
        for w in (self.mod, self.cr, self.segs, self.ti):
            w.setEnabled(act)


class GeneratorDialog(QDialog):
    def __init__(self, parent=None, source=None, open_cb=None):
        super().__init__(parent)
        self.setWindowTitle("Gerar / transmitir BTS e TS a partir de uma gravação")
        self.setWindowIcon(icon("network"))
        self.setMinimumWidth(820)
        self.resize(880, 760)
        self.settings = QSettings("PIDgeon", "PIDgeon")
        self.open_cb = open_cb
        self.pr = None
        self.gen = None
        self.thread = None
        self.bridge = _Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.done.connect(self._done)
        lay = QVBoxLayout(self)

        # ------------------------------------------------ gravação
        gb = QGroupBox("Gravação de origem")
        v = QVBoxLayout(gb)
        h = QHBoxLayout()
        self.src = QLineEdit()
        self.src.setPlaceholderText("arquivo .ts / .bts / .trp / .m2ts gravado (pelo PIDgeon ou outro equipamento)")
        h.addWidget(self.src, 1)
        b = QPushButton("Escolher…")
        b.clicked.connect(self._choose_src)
        h.addWidget(b)
        b = QPushButton("Analisar")
        b.clicked.connect(self._probe)
        h.addWidget(b)
        v.addLayout(h)
        self.src_info = QLabel("—")
        self.src_info.setWordWrap(True)
        self.src_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.src_info)
        lay.addWidget(gb)

        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # ------------------------------------------------ aba Formato
        w = QWidget()
        v = QVBoxLayout(w)
        f = QFormLayout()
        self.fmt = QComboBox()
        for k, label in FORMATS.items():
            self.fmt.addItem(label, k)
        self.fmt.currentIndexChanged.connect(self._update)
        f.addRow("Formato de saída", self.fmt)
        self.remove_nulls = QCheckBox("Remover pacotes nulos (saída VBR — o envio segue o PCR)")
        f.addRow("TS 188 / 204", self.remove_nulls)
        hl = QHBoxLayout()
        self.keep = {}
        for k in "ABC":
            c = QCheckBox(k)
            c.setChecked(True)
            self.keep[k] = c
            hl.addWidget(c)
        hl.addWidget(QLabel("(as camadas desmarcadas viram nulo — ex.: só A = TS do 1-seg)"))
        hl.addStretch(1)
        self.keep_box = QWidget()
        self.keep_box.setLayout(hl)
        hl.setContentsMargins(0, 0, 0, 0)
        f.addRow("Camadas do BTS no TS", self.keep_box)
        self.rs_parity = QCheckBox("Paridade RS(204,196) nos 8 bytes finais (padrão: zeros)")
        f.addRow("BTS 204", self.rs_parity)
        self.force_remux = QCheckBox("Remultiplexar mesmo que a gravação já seja BTS 204 (aplica os parâmetros da aba ISDB-T)")
        f.addRow("", self.force_remux)
        v.addLayout(f)
        self.fmt_hint = QLabel()
        self.fmt_hint.setWordWrap(True)
        self.fmt_hint.setStyleSheet("color: palette(mid);")
        v.addWidget(self.fmt_hint)
        v.addStretch(1)
        tabs.addTab(w, "Formato")

        # ------------------------------------------------ aba ISDB-T
        w = QWidget()
        v = QVBoxLayout(w)
        self.manual = QCheckBox("Definir os parâmetros manualmente (desmarcado: usa a IIP da gravação ou o padrão "
                                "1-seg + HD 64-QAM 3/4)")
        self.manual.toggled.connect(self._update)
        v.addWidget(self.manual)
        h = QHBoxLayout()
        self.mode = QComboBox()
        for m in (1, 2, 3):
            self.mode.addItem("Modo %d (%dk)" % (m, 1 << m), m)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.gi = QComboBox()
        for k in (0, 1, 2, 3):
            self.gi.addItem("GI %s" % ISDB_GUARD[k], k)
        self.gi.currentIndexChanged.connect(self._update)
        self.partial = QCheckBox("Recepção parcial (1-seg na camada A)")
        self.partial.toggled.connect(self._update)
        for x in (self.mode, self.gi, self.partial):
            h.addWidget(x)
        h.addStretch(1)
        v.addLayout(h)
        grid = QGridLayout()
        for c, t in enumerate(("", "Modulação", "Taxa de código", "Segmentos", "Entrelaçamento", "Capacidade (TS 188)")):
            grid.addWidget(QLabel("<b>%s</b>" % t), 0, c)
        self.rows = [LayerRow(grid, i + 1, n, self._update) for i, n in enumerate("ABC")]
        v.addLayout(grid)
        self.isdb_info = QLabel()
        self.isdb_info.setWordWrap(True)
        v.addWidget(self.isdb_info)
        lb = QLabel("Camada de cada serviço na remultiplexação (Auto: 1-seg na A, demais na camada de maior "
                    "capacidade; PAT/NIT/SDT/TOT acompanham o 1-seg):")
        lb.setWordWrap(True)
        v.addWidget(lb)
        self.svc_table = QTableWidget(0, 4)
        self.svc_table.setHorizontalHeaderLabels(["Serviço", "Nome", "Hoje", "Camada no BTS gerado"])
        hdr = self.svc_table.horizontalHeader()
        for c in (0, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self.svc_table.verticalHeader().setVisible(False)
        self.svc_table.setMinimumHeight(110)
        v.addWidget(self.svc_table)
        tabs.addTab(w, "ISDB-T (remux)")
        self._isdb_tab = w

        # ------------------------------------------------ aba Destino
        w = QWidget()
        v = QVBoxLayout(w)
        h = QHBoxLayout()
        self.to_file = QCheckBox("Gravar arquivo")
        self.to_file.toggled.connect(self._update)
        h.addWidget(self.to_file)
        self.out = QLineEdit()
        h.addWidget(self.out, 1)
        b = QPushButton("Salvar como…")
        b.clicked.connect(self._choose_out)
        h.addWidget(b)
        v.addLayout(h)
        self.to_net = QCheckBox("Transmitir na rede (no ritmo real do stream)")
        self.to_net.toggled.connect(self._update)
        v.addWidget(self.to_net)
        g = QGridLayout()
        self.proto = QComboBox()
        for k, label in PROTOCOLS.items():
            self.proto.addItem(label, k)
        self.proto.currentIndexChanged.connect(self._update)
        self.host = QLineEdit("239.1.1.1")
        self.host.setPlaceholderText("grupo multicast (224–239.x.x.x) ou IP unicast do receptor")
        self.host.textChanged.connect(self._update)
        self.cast = QLabel()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(1234)
        self.port.valueChanged.connect(self._update)
        self.iface = InterfaceCombo(self, group_getter=lambda: self.host.text().strip())
        self.iface.changed.connect(self._update)
        self.ttl = QSpinBox()
        self.ttl.setRange(1, 255)
        self.ttl.setValue(16)
        self.ttl.valueChanged.connect(self._update)
        self.dscp = QSpinBox()
        self.dscp.setRange(0, 63)
        self.dscp.setToolTip("QoS: 0 = melhor esforço, 34 = AF41 (vídeo), 46 = EF")
        self.dscp.valueChanged.connect(self._update)
        self.pkts = QSpinBox()
        self.pkts.setRange(1, 7)
        self.pkts.setValue(7)
        self.pkts.valueChanged.connect(self._update)
        self.loopback = QCheckBox("Multicast também nesta máquina (loopback)")
        self.loopback.setChecked(True)
        self.fec_l = QSpinBox()
        self.fec_l.setRange(1, 20)
        self.fec_l.setValue(10)
        self.fec_l.setPrefix("L = ")
        self.fec_d = QSpinBox()
        self.fec_d.setRange(4, 20)
        self.fec_d.setValue(10)
        self.fec_d.setPrefix("D = ")
        self.fec_2d = QCheckBox("2D (também linhas)")
        for x in (self.fec_l, self.fec_d):
            x.valueChanged.connect(self._update)
        self.fec_2d.toggled.connect(self._update)
        g.addWidget(QLabel("Protocolo"), 0, 0)
        g.addWidget(self.proto, 0, 1)
        g.addWidget(QLabel("Destino"), 0, 2)
        g.addWidget(self.host, 0, 3)
        g.addWidget(self.cast, 0, 4)
        g.addWidget(QLabel("Porta"), 1, 0)
        g.addWidget(self.port, 1, 1)
        g.addWidget(QLabel("Interface de saída"), 1, 2)
        g.addWidget(self.iface, 1, 3, 1, 2)
        g.addWidget(QLabel("TTL"), 2, 0)
        g.addWidget(self.ttl, 2, 1)
        g.addWidget(QLabel("DSCP"), 2, 2)
        hh = QHBoxLayout()
        hh.addWidget(self.dscp)
        hh.addWidget(QLabel("Pacotes TS por datagrama"))
        hh.addWidget(self.pkts)
        hh.addStretch(1)
        g.addLayout(hh, 2, 3, 1, 2)
        g.addWidget(QLabel("FEC 2022-1"), 3, 0)
        hh = QHBoxLayout()
        for x in (self.fec_l, self.fec_d, self.fec_2d):
            hh.addWidget(x)
        hh.addStretch(1)
        g.addLayout(hh, 3, 1, 1, 3)
        g.addWidget(self.loopback, 4, 1, 1, 3)
        self.net_box = QWidget()
        self.net_box.setLayout(g)
        v.addWidget(self.net_box)
        self.url = QLabel()
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.url.setStyleSheet("font-family: monospace; padding: 4px;")
        self.url.setWordWrap(True)
        v.addWidget(self.url)
        h = QHBoxLayout()
        self.loop = QCheckBox("Repetir a gravação (loop)")
        h.addWidget(self.loop)
        h.addWidget(QLabel("Velocidade"))
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.1, 10.0)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(1.0)
        self.speed.setSuffix("×")
        h.addWidget(self.speed)
        h.addStretch(1)
        v.addLayout(h)
        v.addStretch(1)
        tabs.addTab(w, "Destino (arquivo / rede)")
        self.tabs = tabs

        # ------------------------------------------------ execução
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        lay.addWidget(self.progress)
        self.status = QLabel("Parado")
        lay.addWidget(self.status)
        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumHeight(110)
        lay.addWidget(self.logbox)
        h = QHBoxLayout()
        self.start_btn = QPushButton(icon("play", 16), "Iniciar")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton(icon("stop", 16), "Parar")
        self.stop_btn.clicked.connect(self.stop)
        self.open_btn = QPushButton("Abrir saída no analisador")
        self.open_btn.clicked.connect(self._open_output)
        self.listen_btn = QPushButton("Analisar o que está sendo transmitido")
        self.listen_btn.clicked.connect(self._open_stream)
        close = QPushButton("Fechar")
        close.clicked.connect(self.close)
        for x in (self.start_btn, self.stop_btn, self.open_btn, self.listen_btn):
            h.addWidget(x)
        h.addStretch(1)
        h.addWidget(close)
        lay.addLayout(h)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        self._load_settings()
        self.rows[0].set_mode(3)
        for r in self.rows:
            r.set_mode(self.mode.currentData())
        if source:
            self.src.setText(source)
        if self.src.text().strip() and os.path.isfile(self.src.text().strip()):
            QTimer.singleShot(50, self._probe)
        else:
            self._fill_isdb(IsdbParams())
        self._update()

    # ============================================================ configurações
    def _load_settings(self):
        s = self.settings
        s.beginGroup("gerador")
        self.src.setText(s.value("src", ""))
        i = self.fmt.findData(s.value("fmt", "bts204"))
        self.fmt.setCurrentIndex(max(0, i))
        self.to_file.setChecked(s.value("to_file", True, type=bool))
        self.to_net.setChecked(s.value("to_net", False, type=bool))
        self.proto.setCurrentIndex(max(0, self.proto.findData(s.value("proto", "udp"))))
        self.host.setText(s.value("host", "239.1.1.1"))
        self.port.setValue(int(s.value("port", 1234)))
        self.iface.set_value(s.value("iface", ""))
        self.ttl.setValue(int(s.value("ttl", 16)))
        self.dscp.setValue(int(s.value("dscp", 0)))
        self.pkts.setValue(int(s.value("pkts", 7)))
        self.fec_l.setValue(int(s.value("fec_l", 10)))
        self.fec_d.setValue(int(s.value("fec_d", 10)))
        self.fec_2d.setChecked(s.value("fec_2d", False, type=bool))
        self.loop.setChecked(s.value("loop", False, type=bool))
        self.out_dir = s.value("out_dir", os.path.expanduser("~/Vídeos/PIDgeon"))
        s.endGroup()

    def _save_settings(self):
        s = self.settings
        s.beginGroup("gerador")
        for k, v in (("src", self.src.text().strip()), ("fmt", self.fmt.currentData()),
                     ("to_file", self.to_file.isChecked()), ("to_net", self.to_net.isChecked()),
                     ("proto", self.proto.currentData()), ("host", self.host.text().strip()),
                     ("port", self.port.value()), ("iface", self.iface.value()), ("ttl", self.ttl.value()),
                     ("dscp", self.dscp.value()), ("pkts", self.pkts.value()), ("fec_l", self.fec_l.value()),
                     ("fec_d", self.fec_d.value()), ("fec_2d", self.fec_2d.isChecked()),
                     ("loop", self.loop.isChecked()), ("out_dir", self.out_dir)):
            s.setValue(k, v)
        s.endGroup()

    # ============================================================ gravação
    def _choose_src(self):
        path, _ = QFileDialog.getOpenFileName(self, "Gravação de origem", os.path.dirname(self.src.text()) or
                                              os.path.expanduser("~"),
                                              "Transport Stream (*.ts *.bts *.trp *.m2ts *.mts *.tp *.ts204 *.bts188);;"
                                              "Todos os arquivos (*)")
        if path:
            self.src.setText(path)
            self._probe()

    def _probe(self):
        path = self.src.text().strip()
        if not path or not os.path.isfile(path):
            self.src_info.setText("Arquivo não encontrado.")
            return
        try:
            self.pr = probe(path)
        except Exception as exc:
            self.pr = None
            self.src_info.setText("Falha ao analisar: %s" % exc)
            return
        self.src_info.setText(self.pr.summary())
        self._fill_isdb(self.pr.iip_params or IsdbParams())
        self.svc_table.setRowCount(0)
        for sid, name, one, ly, _pids in self.pr.services():
            r = self.svc_table.rowCount()
            self.svc_table.insertRow(r)
            it = QTableWidgetItem("0x%04X" % sid)
            it.setData(Qt.UserRole, sid)
            self.svc_table.setItem(r, 0, it)
            self.svc_table.setItem(r, 1, QTableWidgetItem(name + ("  [1-seg]" if one else "")))
            self.svc_table.setItem(r, 2, QTableWidgetItem(LAYER_LETTER.get(ly, "—")))
            cb = QComboBox()
            cb.addItems(["Auto", "A", "B", "C"])
            cb.currentIndexChanged.connect(self._update)
            self.svc_table.setCellWidget(r, 3, cb)
        if not self.out.text().strip():
            self._suggest_out()
        self._update()

    def _fill_isdb(self, p):
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(self.mode.findData(p.mode))
        self.mode.blockSignals(False)
        for r in self.rows:
            r.set_mode(p.mode)
        self.gi.setCurrentIndex(self.gi.findData(p.gi))
        self.partial.setChecked(p.partial)
        for r, l in zip(self.rows, p.layers):
            r.set(l)

    def _mode_changed(self):
        for r in self.rows:
            r.set_mode(self.mode.currentData())
        self._update()

    def _isdb_params(self):
        return IsdbParams(mode=self.mode.currentData(), gi=self.gi.currentData(), partial=self.partial.isChecked(),
                          layers=[r.get() for r in self.rows])

    def _service_layers(self):
        out = {}
        for r in range(self.svc_table.rowCount()):
            cb = self.svc_table.cellWidget(r, 3)
            if cb and cb.currentText() != "Auto":
                out[self.svc_table.item(r, 0).data(Qt.UserRole)] = cb.currentText()
        return out

    # ============================================================ destino
    def _suggest_out(self):
        base = os.path.splitext(os.path.basename(self.src.text().strip() or "saida"))[0]
        fmt = self.fmt.currentData()
        d = self.out_dir if os.path.isdir(self.out_dir) else os.path.dirname(self.src.text().strip()) or os.path.expanduser("~")
        self.out.setText(os.path.join(d, "%s_%s%s" % (base, fmt, EXT.get(fmt, ".ts"))))

    def _choose_out(self):
        fmt = self.fmt.currentData()
        path, _ = QFileDialog.getSaveFileName(self, "Arquivo de saída", self.out.text() or self.out_dir,
                                              "%s (*%s);;Todos (*)" % (FORMATS[fmt], EXT.get(fmt, ".ts")))
        if path:
            self.out.setText(path)
            self.out_dir = os.path.dirname(path)
            self.to_file.setChecked(True)

    def _net_config(self):
        return NetConfig(host=self.host.text().strip(), port=self.port.value(), proto=self.proto.currentData(),
                         iface=self.iface.value() or None, ttl=self.ttl.value(), dscp=self.dscp.value(),
                         pkts_per_dgram=self.pkts.value(), fec_cols=self.fec_l.value(), fec_rows=self.fec_d.value(),
                         fec_2d=self.fec_2d.isChecked(), loopback=self.loopback.isChecked())

    def _config(self):
        fmt = self.fmt.currentData()
        keep = tuple(i + 1 for i, k in enumerate("ABC") if self.keep[k].isChecked())
        return GenConfig(
            source=self.src.text().strip(), fmt=fmt,
            out_file=self.out.text().strip() if self.to_file.isChecked() else None,
            net=self._net_config().validate() if self.to_net.isChecked() else None,
            isdb=self._isdb_params().validate() if self.manual.isChecked() else None,
            force_remux=self.force_remux.isChecked(), service_layers=self._service_layers(),
            remove_nulls=self.remove_nulls.isChecked(),
            keep_layers=keep if len(keep) < 3 else None, bts_rs_parity=self.rs_parity.isChecked(),
            loop=self.loop.isChecked(), speed=self.speed.value())

    # ============================================================ estado da tela
    def _update(self, *_):
        fmt = self.fmt.currentData()
        pr = self.pr
        src_bts204 = pr is not None and pr.kind == "bts204"
        ts_out = fmt in ("ts188", "ts204")
        self.remove_nulls.setEnabled(ts_out)
        self.keep_box.setEnabled(ts_out and src_bts204)
        self.rs_parity.setEnabled(fmt == "bts204")
        self.force_remux.setEnabled(fmt in ("bts204", "bts188") and pr is not None and pr.is_bts)
        hints = {
            "bts204": "BTS ISDB-Tb de 204 bytes (188 + 8 bytes de informação ISDB-T + 8 de paridade), 32,507936 Mbps "
                      "— o que entra no modulador. Se a gravação for TS, o PIDgeon remultiplexa: quadro, camadas, "
                      "nulos, IIP e PCR.",
            "bts188": "BTS sem os 16 bytes finais: mesmos pacotes, nulos e IIP, em 188 bytes (29,958 Mbps). "
                      "Útil para equipamentos/IP que não aceitam 204.",
            "ts188": "TS MPEG-2 comum. Vindo de BTS, a IIP vira nulo (taxa constante, PCR exato). "
                     "Marque 'remover nulos' para obter só o conteúdo útil.",
            "ts204": "TS 188 + 16 bytes de paridade Reed-Solomon RS(204,188) por pacote (formato DVB/ASI 204).",
            "original": "Repassa os pacotes exatamente como estão na gravação (só transmite/copia).",
        }
        txt = hints.get(fmt, "")
        if pr is not None:
            remux = needs_remux(GenConfig(source=pr.path, fmt=fmt, force_remux=self.force_remux.isChecked()), pr)
            txt += "\n\nOrigem: %s → %s%s" % (SOURCE_KINDS.get(pr.kind), FORMATS[fmt],
                                              " (remultiplexação ISDB-T)" if remux else "")
            self._isdb_tab.setEnabled(remux)
        self.fmt_hint.setText(txt)
        # ISDB-T
        manual = self.manual.isChecked()
        for x in (self.mode, self.gi, self.partial):
            x.setEnabled(manual)
        p = self._isdb_params()
        for i, r in enumerate(self.rows):
            r.enable(manual)
            l = p.layers[i]
            r.cap.setText("%.3f Mbps (%d TSP/quadro)" % (p.capacity_bps(i) / 1e6, p.layer_tsps(i)) if l else "—")
        if manual:
            try:
                p.validate()
                self.isdb_info.setText("✔ %s — %d TSPs por quadro" % (p.describe(), p.frame_tsps()))
            except ValueError as exc:
                self.isdb_info.setText("✘ %s" % exc)
        else:
            src = "IIP da gravação" if (pr and pr.iip_params) else "padrão (1-seg QPSK 2/3 + HD 64-QAM 3/4, modo 3, GI 1/16)"
            self.isdb_info.setText("Automático: %s" % src)
        # rede
        net = self.to_net.isChecked()
        self.net_box.setEnabled(net)
        fec = self.proto.currentData() == "rtp-fec"
        for x in (self.fec_l, self.fec_d, self.fec_2d):
            x.setEnabled(net and fec)
        host = self.host.text().strip()
        mc = is_multicast(host)
        self.cast.setText("<b>multicast</b>" if mc else "<b>unicast</b>")
        self.ttl.setToolTip("TTL do multicast" if mc else "TTL dos pacotes unicast")
        self.loopback.setEnabled(net and mc)
        self.out.setEnabled(self.to_file.isChecked())
        try:
            nc = self._net_config().validate()
            self.url.setText("%s\n%s" % (nc.describe(), nc.url()) if net else "")
        except ValueError as exc:
            self.url.setText("✘ %s" % exc if net else "")
        running = self.thread is not None and self.thread.is_alive()
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    # ============================================================ execução
    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        if self.pr is None or self.pr.path != self.src.text().strip():
            self._probe()
            if self.pr is None:
                return
        try:
            cfg = self._config()
            if not cfg.out_file and not cfg.net:
                raise ValueError("Marque 'Gravar arquivo' e/ou 'Transmitir na rede'")
            if cfg.out_file and os.path.abspath(cfg.out_file) == os.path.abspath(cfg.source):
                raise ValueError("O arquivo de saída não pode ser a própria gravação")
            self.gen = Generator(cfg, self.pr, log=self.bridge.log.emit)
        except ValueError as exc:
            QMessageBox.warning(self, "Gerar / transmitir", str(exc))
            return
        if cfg.out_file:
            self.out_dir = os.path.dirname(os.path.abspath(cfg.out_file))
        self._save_settings()
        warn = self.gen.capacity_warnings()
        if warn and QMessageBox.question(self, "Capacidade das camadas", "\n".join(warn) + "\n\nContinuar assim mesmo?") \
                != QMessageBox.Yes:
            return
        self.logbox.clear()
        gen = self.gen

        def work():
            try:
                gen.run()
                self.bridge.done.emit("")
            except Exception as exc:
                self.bridge.done.emit(str(exc))
        self.thread = threading.Thread(target=work, daemon=True, name="pidgeon-gerador")
        self.thread.start()
        self.timer.start(300)
        self._update()

    def stop(self):
        if self.gen is not None:
            self.gen.stop()

    def _tick(self):
        g = self.gen
        if g is None:
            return
        st = g.stats
        self.progress.setValue(int(1000 * (st.progress % 1.0 if st.loops else st.progress)))
        n = st.net or {}
        parts = ["%.1f s de mídia" % st.media_t, "%.3f Mbps" % (st.rate_bps() / 1e6),
                 "%d pacotes" % st.packets_out]
        if n:
            parts.append("%d datagramas" % n.get("datagrams", 0))
            if n.get("fec_col") or n.get("fec_row"):
                parts.append("FEC %d col / %d lin" % (n.get("fec_col", 0), n.get("fec_row", 0)))
            if n.get("errors"):
                parts.append("%d erros de envio" % n["errors"])
        if st.loops:
            parts.append("loop %d" % st.loops)
        self.status.setText(" · ".join(parts))

    def _log(self, text):
        self.logbox.appendPlainText(text)

    def _done(self, err):
        self.timer.stop()
        self._tick()
        if not err:
            self.progress.setValue(1000)
        self.status.setText(("Erro: %s" % err) if err else ("Concluído — " + self.status.text()))
        self._update()

    def _open_output(self):
        path = self.out.text().strip()
        if self.open_cb and path and os.path.isfile(path):
            self.open_cb(path)
        else:
            QMessageBox.information(self, "Abrir saída", "Gere o arquivo primeiro.")

    def _open_stream(self):
        if not self.open_cb:
            return
        try:
            nc = self._net_config().validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Analisar transmissão", str(exc))
            return
        host = nc.host if nc.multicast else "0.0.0.0"
        url = "%s://%s:%d" % ("udp" if nc.proto == "udp" else "rtp", host, nc.port)
        q = []
        if nc.proto == "rtp-fec":
            q.append("fec=on")
        if nc.iface and nc.multicast:
            q.append("iface=%s" % nc.iface)
        if q:
            url += "?" + "&".join(q)
        self.open_cb(url)

    def closeEvent(self, ev):
        if self.thread is not None and self.thread.is_alive():
            if QMessageBox.question(self, "Gerar / transmitir", "A geração está em andamento. Parar e fechar?") \
                    != QMessageBox.Yes:
                ev.ignore()
                return
            self.stop()
            self.thread.join(2)
        self._save_settings()
        super().closeEvent(ev)
