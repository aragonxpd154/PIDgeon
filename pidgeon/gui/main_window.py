"""Janela principal do PIDgeon (layout inspirado no StreamXpert / TS Reader)."""

import csv
import datetime
import json
import os
import threading
import time

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QSettings, QSize
from PySide6.QtGui import QAction, QFont, QKeySequence, QColor, QBrush, QShortcut
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QToolButton, QLabel,
    QComboBox, QFrame, QSplitter, QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QDockWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QMessageBox, QMenu, QCheckBox,
    QSpinBox, QSlider, QProgressBar, QApplication, QScrollArea, QSizePolicy, QAbstractItemView,
    QPushButton,
)

from .. import __version__
from ..core.analyzer import Analyzer
from ..core.sources import open_source, SourceSpec
from ..core.recorder import ServiceFilter, BlockingPipe, Recorder
from ..core.names import (LAYER_NAMES, stream_type_name, component_tag_kind, VIDEO_STREAM_TYPES,
                          AUDIO_STREAM_TYPES, NULL_PID, ISDB_MODES, ISDB_GUARD, TMCC_IDENTIFIER)
from ..core.isdbt import BTSInfo
from ..core.tables import describe_service_type
from ..core.text import hexdump
from ..player.decoder import Decoder, decode_thumbnail, HAVE_AV
from . import theme
from .icons import icon, lamp_pixmap, app_icon
from .widgets import TraceGraph, VideoWidget, LevelMeter, fmt_bps, fmt_int
from .dialogs import NetworkDialog, AboutDialog

LAYER_COLORS = {"A": "#16a34a", "B": "#2563eb", "C": "#9333ea", "Nulo": "#9ca3af", "IIP": "#0891b2"}


class Bridge(QObject):
    frame = Signal(object, int, int, int)
    info = Signal(dict)
    levels = Signal(float, float)
    error = Signal(str)
    thumb = Signal(int, object)
    gen_done = Signal(str, str)


class MosaicWorker(threading.Thread):
    """Captura alguns segundos de cada serviço com vídeo e decodifica uma miniatura."""

    def __init__(self, an, bridge):
        super().__init__(daemon=True, name="pidgeon-mosaic")
        self.an = an
        self.bridge = bridge
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.current = None
        self.buf = bytearray()
        self.filter = None

    def tap(self, pkts, size, t):
        f = self.filter
        if f is not None:
            f(pkts, size, t)

    def _sink(self, pkts):
        with self.lock:
            if len(self.buf) < 6 * 1024 * 1024:
                self.buf += b"".join(pkts)

    def run(self):
        while not self.stop_ev.is_set():
            with self.an.lock:
                sids = [s.sid for s in self.an.services.values()
                        if s.video_streams() and self.an.pat.get(s.sid) == s.pmt_pid]
            if not sids:
                self.stop_ev.wait(1.0)
                continue
            for sid in sids:
                if self.stop_ev.is_set():
                    break
                with self.an.lock:
                    s = self.an.services.get(sid)
                    if s is None or not s.video_streams():
                        continue
                    vpid = s.video_streams()[0]["pid"]
                with self.lock:
                    self.buf = bytearray()
                self.filter = ServiceFilter(self.an, sid, self._sink, only_pids=[vpid], include_si=False)
                t0 = time.monotonic()
                while time.monotonic() - t0 < 2.5 and not self.stop_ev.is_set():
                    self.stop_ev.wait(0.1)
                    with self.lock:
                        if len(self.buf) > 3 * 1024 * 1024:
                            break
                self.filter = None
                with self.lock:
                    data = bytes(self.buf)
                if len(data) > 188 * 50:
                    r = decode_thumbnail(data, 320)
                    if r and not self.stop_ev.is_set():
                        try:
                            self.bridge.thumb.emit(sid, r)
                        except RuntimeError:  # janela já fechada
                            return
            self.stop_ev.wait(3.0)

    def stop(self):
        self.stop_ev.set()


def ribbon_group(title, widgets_layout):
    fr = QFrame()
    fr.setObjectName("ribbonGroup")
    v = QVBoxLayout(fr)
    v.setContentsMargins(6, 2, 8, 2)
    v.setSpacing(2)
    v.addLayout(widgets_layout, 1)
    lb = QLabel(title)
    lb.setObjectName("groupTitle")
    lb.setAlignment(Qt.AlignCenter)
    v.addWidget(lb)
    return fr


def big_button(text, ic, slot=None, checkable=False, tip=None):
    b = QToolButton()
    b.setText(text)
    b.setIcon(icon(ic, 32))
    b.setIconSize(QSize(28, 28))
    b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
    b.setCheckable(checkable)
    b.setMinimumWidth(64)
    if tip:
        b.setToolTip(tip)
    if slot:
        if checkable:
            b.toggled.connect(slot)
        else:
            b.clicked.connect(slot)
    return b


def kv_tree(headers=("Campo", "Valor")):
    t = QTreeWidget()
    t.setHeaderLabels(list(headers))
    t.setAlternatingRowColors(True)
    t.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    t.setUniformRowHeights(True)
    return t


def node_to_item(node, parent):
    it = QTreeWidgetItem(parent, [str(node.label), "" if node.value is None else str(node.value)])
    for c in node.children:
        node_to_item(c, it)
    return it


class MainWindow(QMainWindow):
    def __init__(self, initial=None):
        super().__init__()
        self.settings = QSettings("PIDgeon", "PIDgeon")
        self.dark = self.settings.value("dark", False, type=bool)
        theme.apply(QApplication.instance(), self.dark)
        self.setWindowTitle("PIDgeon %s — nenhuma entrada" % __version__)
        self.setWindowIcon(app_icon())
        self.resize(1500, 920)
        self.an = None
        self.src = None
        self.recorder = None
        self.player = None
        self.mosaic = None
        self.bridge = Bridge()
        self.bridge.frame.connect(self._on_frame)
        self.bridge.info.connect(self._on_player_info)
        self.bridge.levels.connect(lambda l, r: self.meter.set_levels(l, r))
        self.bridge.error.connect(lambda m: self._log_local(m, "warn"))
        self.bridge.thumb.connect(self._on_thumb)
        self.bridge.gen_done.connect(self._on_gen_done)
        self.player_info = {}
        self.msg_seen = 0
        self.sel_pid = None
        self.svc_sig = None
        self.tables_sig = None
        self.epg_sig = None
        self.pid_items = {}
        self.thumbs = {}
        self._build_ribbon()
        self._build_center()
        self._build_docks()
        self._build_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.decay = QTimer(self)
        self.decay.timeout.connect(self.meter.decay)
        self.decay.start(120)
        QShortcut(QKeySequence("Ctrl+O"), self, self.open_file)
        QShortcut(QKeySequence("Ctrl+N"), self, self.open_network)
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("Space"), self, self.toggle_play)
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if initial:
            QTimer.singleShot(200, lambda: self.open_input(initial))

    # ================================================================ UI
    def _build_ribbon(self):
        self.ribbon = QTabWidget()
        self.ribbon.setObjectName("ribbon")
        self.ribbon.setFixedHeight(122)
        self.ribbon.setDocumentMode(False)

        # ---------------- Início
        home = QWidget()
        h = QHBoxLayout(home)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(0)
        g = QHBoxLayout()
        g.addWidget(big_button("Arquivo", "file", self.open_file, tip="Abrir arquivo .ts/.bts/.trp/.m2ts (Ctrl+O)"))
        g.addWidget(big_button("Rede", "network", self.open_network, tip="UDP/RTP multicast/unicast, HTTP (Ctrl+N)"))
        self.recent_btn = big_button("Recentes", "reset")
        self.recent_menu = QMenu(self)
        self.recent_btn.setMenu(self.recent_menu)
        self.recent_btn.setPopupMode(QToolButton.InstantPopup)
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        g.addWidget(self.recent_btn)
        h.addWidget(ribbon_group("Entrada", g))
        g = QHBoxLayout()
        self.stop_btn = big_button("Parar", "stop", self.stop_input)
        g.addWidget(self.stop_btn)
        self.pause_btn = big_button("Pausar", "pause", self.toggle_pause, checkable=True, tip="Pausa a leitura do arquivo")
        g.addWidget(self.pause_btn)
        h.addWidget(ribbon_group("Controle", g))
        g = QGridLayout()
        self.realtime_chk = QCheckBox("Tempo real (taxa do mux)")
        self.realtime_chk.setChecked(True)
        self.realtime_chk.setToolTip("Desmarcado: análise rápida do arquivo (sem player)")
        self.loop_chk = QCheckBox("Repetir arquivo (loop)")
        g.addWidget(self.realtime_chk, 0, 0)
        g.addWidget(self.loop_chk, 1, 0)
        h.addWidget(ribbon_group("Arquivo", g))
        g = QHBoxLayout()
        g.addWidget(big_button("Zerar", "reset", self.reset_counters, tip="Zera contadores de erro e bitrate"))
        g.addWidget(big_button("Relatório", "save", self.export_report, tip="Exportar relatório JSON"))
        h.addWidget(ribbon_group("Análise", g))
        g = QHBoxLayout()
        g.addWidget(big_button("Gerar teste", "isdb", self.generate_test,
                               tip="Gera um BTS ISDB-Tb sintético (HD camada B + 1-seg camada A)"))
        g.addWidget(big_button("Sobre", "logo", lambda: AboutDialog(self, __version__).exec()))
        h.addWidget(ribbon_group("Ferramentas", g))
        h.addStretch(1)
        self.ribbon.addTab(home, "Início")

        # ---------------- Decodificação
        dec = QWidget()
        h = QHBoxLayout(dec)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(0)
        g = QHBoxLayout()
        self.play_btn = big_button("Reproduzir", "play", self.start_player)
        g.addWidget(self.play_btn)
        grid = QGridLayout()
        grid.setVerticalSpacing(2)
        self.prog_cb = QComboBox()
        self.prog_cb.setMinimumWidth(260)
        self.prog_cb.currentIndexChanged.connect(self._fill_components)
        self.video_cb = QComboBox()
        self.audio_cb = QComboBox()
        for r, (lbl, w) in enumerate((("Programa", self.prog_cb), ("Vídeo", self.video_cb), ("Áudio", self.audio_cb))):
            grid.addWidget(QLabel(lbl), r, 0)
            grid.addWidget(w, r, 1)
        g.addLayout(grid)
        g.addWidget(big_button("Anterior", "prev", lambda: self.step_service(-1)))
        g.addWidget(big_button("Próximo", "next", lambda: self.step_service(1)))
        g.addWidget(big_button("Parar", "stop", self.stop_player))
        h.addWidget(ribbon_group("Seleção de serviço", g))
        g = QHBoxLayout()
        g.addWidget(big_button("Tela cheia", "fullscreen", self.toggle_fullscreen, tip="F11 ou duplo clique no vídeo"))
        v = QVBoxLayout()
        self.deint_chk = QCheckBox("Desentrelaçar (yadif)")
        self.deint_chk.setChecked(True)
        v.addWidget(self.deint_chk)
        self.maxw_cb = QComboBox()
        for lbl, val in (("Resolução nativa (até 1920)", 1920), ("Até 1280", 1280), ("Até 960", 960), ("Até 640", 640)):
            self.maxw_cb.addItem(lbl, val)
        v.addWidget(self.maxw_cb)
        g.addLayout(v)
        h.addWidget(ribbon_group("Exibição", g))
        g = QHBoxLayout()
        self.mute_btn = big_button("Mudo", "speaker", self._set_mute, checkable=True)
        g.addWidget(self.mute_btn)
        self.vol = QSlider(Qt.Vertical)
        self.vol.setRange(0, 150)
        self.vol.setValue(100)
        self.vol.valueChanged.connect(self._set_volume)
        self.vol.setToolTip("Volume")
        g.addWidget(self.vol)
        h.addWidget(ribbon_group("Áudio", g))
        h.addStretch(1)
        self.ribbon.addTab(dec, "Decodificação")

        # ---------------- TR 101 290
        etr = QWidget()
        h = QHBoxLayout(etr)
        h.setContentsMargins(4, 2, 4, 2)
        g = QHBoxLayout()
        g.addWidget(big_button("Zerar", "reset", self.reset_counters))
        g.addWidget(big_button("Exportar CSV", "save", self.export_etr_csv))
        g.addWidget(big_button("Ver tabela", "table", lambda: self.center.setCurrentWidget(self.etr_page)))
        h.addWidget(ribbon_group("TR 101 290 (ETSI) — prioridades 1, 2 e 3 + ISDB-T", g))
        h.addStretch(1)
        self.ribbon.addTab(etr, "TR 101 290")

        # ---------------- Gravação
        rec = QWidget()
        h = QHBoxLayout(rec)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(0)
        g = QHBoxLayout()
        self.rec_btn = big_button("Gravar", "record", self.start_record)
        g.addWidget(self.rec_btn)
        g.addWidget(big_button("Parar", "stop", self.stop_record))
        v = QVBoxLayout()
        self.rec_mode = QComboBox()
        self.rec_mode.addItem("Mux completo — formato original (BTS 204 / TS 188)", "full_raw")
        self.rec_mode.addItem("Mux completo — converter para TS 188", "full_188")
        self.rec_mode.addItem("Serviço selecionado com tabelas (PAT/PMT/SI/PCR)", "service")
        self.rec_mode.addItem("PID selecionado na lista", "pids")
        v.addWidget(self.rec_mode)
        hh = QHBoxLayout()
        self.split_min = QSpinBox()
        self.split_min.setRange(0, 1440)
        self.split_min.setSuffix(" min")
        self.split_min.setSpecialValueText("sem divisão por tempo")
        self.split_mb = QSpinBox()
        self.split_mb.setRange(0, 100000)
        self.split_mb.setSuffix(" MB")
        self.split_mb.setSpecialValueText("sem divisão por tamanho")
        hh.addWidget(self.split_min)
        hh.addWidget(self.split_mb)
        v.addLayout(hh)
        g.addLayout(v)
        h.addWidget(ribbon_group("Gravação", g))
        g = QVBoxLayout()
        self.rec_dir = self.settings.value("rec_dir", os.path.expanduser("~/Vídeos/PIDgeon"))
        self.rec_dir_lbl = QLabel(self.rec_dir)
        self.rec_dir_lbl.setMinimumWidth(240)
        g.addWidget(self.rec_dir_lbl)
        b = QPushButton("Escolher pasta…")
        b.clicked.connect(self._choose_rec_dir)
        g.addWidget(b)
        self.rec_status = QLabel("Parado")
        g.addWidget(self.rec_status)
        h.addWidget(ribbon_group("Destino", g))
        h.addStretch(1)
        self.ribbon.addTab(rec, "Gravação")

        # ---------------- Exibir
        view = QWidget()
        h = QHBoxLayout(view)
        h.setContentsMargins(4, 2, 4, 2)
        g = QHBoxLayout()
        self.msg_toggle = big_button("Mensagens", "table", None, checkable=True)
        self.trace_toggle = big_button("Trace bar", "chart", None, checkable=True)
        g.addWidget(self.msg_toggle)
        g.addWidget(self.trace_toggle)
        h.addWidget(ribbon_group("Painéis", g))
        g = QHBoxLayout()
        self.dark_btn = big_button("Tema escuro", "theme", self._toggle_theme, checkable=True)
        self.dark_btn.setChecked(self.dark)
        g.addWidget(self.dark_btn)
        h.addWidget(ribbon_group("Aparência", g))
        h.addStretch(1)
        self.ribbon.addTab(view, "Exibir")
        self.ribbon.setCurrentIndex(1)

    def _build_center(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(4)
        lay.addWidget(self.ribbon)
        split = QSplitter(Qt.Horizontal)
        # ---------------- lado esquerdo
        self.left = QTabWidget()
        self.left.setObjectName("leftTabs")
        self.left.setTabPosition(QTabWidget.South)
        self.pid_tree = QTreeWidget()
        self.pid_tree.setHeaderHidden(True)
        self.pid_tree.setUniformRowHeights(True)
        self.pid_tree.itemSelectionChanged.connect(self._pid_selected)
        self.pid_tree.itemExpanded.connect(lambda it: self.refresh())
        self.pid_root = QTreeWidgetItem(self.pid_tree, ["PID Info (0)"])
        self.pid_root.setIcon(0, icon("table", 16))
        f = self.pid_root.font(0)
        f.setBold(True)
        self.pid_root.setFont(0, f)
        self.pid_root.setExpanded(True)
        self.left.addTab(self.pid_tree, "PID")
        self.svc_tree = QTreeWidget()
        self.svc_tree.setHeaderLabels(["Serviço / componente", "Info"])
        self.svc_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.svc_tree.itemDoubleClicked.connect(self._svc_double)
        self.left.addTab(self.svc_tree, "Serviços")
        self.tbl_tree = kv_tree(("Tabela / campo", "Valor"))
        self.left.addTab(self.tbl_tree, "Tabelas PSI/SI")
        split.addWidget(self.left)
        # ---------------- centro
        self.center = QTabWidget()
        self.center.setObjectName("centerTabs")
        self.center.setTabPosition(QTabWidget.South)
        self.center.currentChanged.connect(lambda i: self.refresh())
        # TV
        tv = QWidget()
        tl = QVBoxLayout(tv)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        self.tv_info = QLabel()
        self.tv_info.setObjectName("infoBar")
        self.tv_info.setTextFormat(Qt.RichText)
        tl.addWidget(self.tv_info)
        hv = QHBoxLayout()
        hv.setSpacing(0)
        self.video = VideoWidget()
        self.video.doubleClicked.connect(self.toggle_fullscreen)
        self.video_holder = hv
        hv.addWidget(self.video, 1)
        self.meter = LevelMeter()
        hv.addWidget(self.meter)
        tl.addLayout(hv, 1)
        self.tv_page = tv
        # PID
        pp = QSplitter(Qt.Vertical)
        self.pid_info = kv_tree()
        pp.addWidget(self.pid_info)
        self.pid_graph = TraceGraph(unit="bps")
        pp.addWidget(self.pid_graph)
        self.pid_hex = QPlainTextEdit()
        self.pid_hex.setReadOnly(True)
        self.pid_hex.setFont(QFont("monospace", 9))
        pp.addWidget(self.pid_hex)
        pp.setSizes([260, 160, 260])
        self.pid_page = pp
        # TS
        self.ts_tree = kv_tree()
        # Grid
        self.grid = QTableWidget()
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.setAlternatingRowColors(True)
        self.grid.cellDoubleClicked.connect(self._grid_double)
        # ISDB-T
        self.isdb_page = self._build_isdb_page()
        # PCR
        self.pcr_page = self._build_pcr_page()
        # TR 101 290
        self.etr_page = QTableWidget()
        self.etr_page.setColumnCount(7)
        self.etr_page.setHorizontalHeaderLabels(["", "Item", "Pr.", "Indicador", "Contagem", "Última ocorrência", "Detalhe"])
        self.etr_page.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.etr_page.verticalHeader().setVisible(False)
        self.etr_page.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.etr_page.setAlternatingRowColors(True)
        # EPG
        self.epg_tree = QTreeWidget()
        self.epg_tree.setHeaderLabels(["Serviço / evento", "Início", "Duração", "Gênero", "Classificação", "Descrição"])
        self.epg_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        # Mosaico
        self.mosaic_page = self._build_mosaic_page()
        for w, name in ((tv, "TV"), (pp, "PID"), (self.ts_tree, "TS"), (self.grid, "Grid"),
                        (self.isdb_page, "ISDB-T / BTS"), (self.pcr_page, "PCR"),
                        (self.etr_page, "TR 101 290"), (self.epg_tree, "EPG"), (self.mosaic_page, "Mosaico")):
            self.center.addTab(w, name)
        split.addWidget(self.center)
        split.setSizes([460, 1040])
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)
        self.setCentralWidget(root)

    def _build_isdb_page(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.layer_tbl = QTableWidget(5, 8)
        self.layer_tbl.setHorizontalHeaderLabels(["Camada", "Modulação", "CR", "Seg.", "I", "Capacidade", "Taxa medida", "Ocupação"])
        self.layer_tbl.verticalHeader().setVisible(False)
        self.layer_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layer_tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.layer_tbl.setMaximumHeight(190)
        self.layer_bars = []
        for r, name in enumerate(("A", "B", "C", "Nulo", "IIP")):
            it = QTableWidgetItem("  " + name)
            it.setForeground(QBrush(QColor(LAYER_COLORS[name])))
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            self.layer_tbl.setItem(r, 0, it)
            pb = QProgressBar()
            pb.setRange(0, 1000)
            pb.setFormat("%p%")
            self.layer_tbl.setCellWidget(r, 7, pb)
            self.layer_bars.append(pb)
        left.addWidget(self.layer_tbl)
        self.isdb_summary = QLabel()
        self.isdb_summary.setTextFormat(Qt.RichText)
        self.isdb_summary.setWordWrap(True)
        left.addWidget(self.isdb_summary)
        self.layer_graph = TraceGraph(unit="bps")
        left.addWidget(self.layer_graph, 1)
        lay.addLayout(left, 3)
        right = QVBoxLayout()
        self.iip_tree = kv_tree()
        right.addWidget(QLabel("<b>IIP — parâmetros TMCC e sincronismo SFN</b>"))
        right.addWidget(self.iip_tree, 3)
        right.addWidget(QLabel("<b>ISDB-T information do último pacote BTS</b>"))
        self.bts_tree = kv_tree()
        right.addWidget(self.bts_tree, 2)
        lay.addLayout(right, 2)
        return w

    def _build_pcr_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("PID do PCR:"))
        self.pcr_cb = QComboBox()
        self.pcr_cb.setMinimumWidth(200)
        top.addWidget(self.pcr_cb)
        self.pcr_stats = QLabel()
        top.addWidget(self.pcr_stats, 1)
        lay.addLayout(top)
        self.pcr_int = TraceGraph(unit="ms")
        self.pcr_int.title = "Intervalo entre PCRs (ms) — limite 40 ms"
        self.pcr_acc = TraceGraph(unit="ns")
        self.pcr_acc.title = "Precisão do PCR (ns) — limite ±500 ns"
        self.pcr_acc.symmetric = True
        self.pcr_jit = TraceGraph(unit="ms")
        self.pcr_jit.title = "Jitter/deriva PCR × chegada (ms) — entrada de rede"
        self.pcr_jit.symmetric = True
        for g in (self.pcr_int, self.pcr_acc, self.pcr_jit):
            lay.addWidget(g, 1)
        return w

    def _build_mosaic_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.mosaic_chk = QCheckBox("Ativar mosaico (decodifica uma miniatura de cada serviço em ciclo)")
        self.mosaic_chk.toggled.connect(self._toggle_mosaic)
        top.addWidget(self.mosaic_chk)
        top.addStretch(1)
        lay.addLayout(top)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        self.mosaic_host = QWidget()
        self.mosaic_grid = QGridLayout(self.mosaic_host)
        self.mosaic_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        sc.setWidget(self.mosaic_host)
        lay.addWidget(sc, 1)
        self.mosaic_cells = {}
        return w

    def _build_docks(self):
        self.msg_dock = QDockWidget("Mensagens", self)
        self.msg_dock.setObjectName("dock_msgs")
        self.msgs = QPlainTextEdit()
        self.msgs.setReadOnly(True)
        self.msgs.setMaximumBlockCount(5000)
        self.msgs.setFont(QFont("monospace", 9))
        self.msg_dock.setWidget(self.msgs)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.msg_dock)
        self.trace_dock = QDockWidget("Trace bar", self)
        self.trace_dock.setObjectName("dock_trace")
        self.trace_tabs = QTabWidget()
        self.trace_tabs.setObjectName("bottomTabs")
        self.trace_tabs.setTabPosition(QTabWidget.South)
        self.trace_rate = TraceGraph(unit="bps")
        self.trace_err = TraceGraph(unit="", bars=True)
        self.trace_err.title = "erros TR 101 290 por segundo"
        self.trace_tabs.addTab(self.trace_rate, "Bit-rate")
        self.trace_tabs.addTab(self.trace_err, "TR 101 290")
        self.trace_dock.setWidget(self.trace_tabs)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.trace_dock)
        self.splitDockWidget(self.msg_dock, self.trace_dock, Qt.Horizontal)
        self.resizeDocks([self.msg_dock, self.trace_dock], [600, 900], Qt.Horizontal)
        self.resizeDocks([self.msg_dock], [170], Qt.Vertical)
        self.msg_toggle.setChecked(True)
        self.trace_toggle.setChecked(True)
        self.msg_toggle.toggled.connect(self.msg_dock.setVisible)
        self.trace_toggle.toggled.connect(self.trace_dock.setVisible)
        self.msg_dock.visibilityChanged.connect(self.msg_toggle.setChecked)
        self.trace_dock.visibilityChanged.connect(self.trace_toggle.setChecked)

    def _build_status(self):
        sb = self.statusBar()
        self.st_text = QLabel("Pronto")
        sb.addWidget(self.st_text, 1)
        self.st_lamp = QLabel()
        self.st_lamp.setPixmap(lamp_pixmap("#9ca3af"))
        sb.addPermanentWidget(self.st_lamp)
        self.st_size = QLabel("-")
        self.st_rate = QLabel("-")
        self.st_fmt = QLabel("-")
        self.st_input = QLabel("-")
        self.st_rec = QLabel("")
        self.st_tot = QLabel("")
        for w in (self.st_size, self.st_rate, self.st_fmt, self.st_input, self.st_rec, self.st_tot):
            sb.addPermanentWidget(w)

    # ================================================================ entrada
    def _fill_recent(self):
        self.recent_menu.clear()
        rec = self.settings.value("recent", []) or []
        if isinstance(rec, str):
            rec = [rec]
        if not rec:
            a = self.recent_menu.addAction("(vazio)")
            a.setEnabled(False)
        for r in rec:
            a = self.recent_menu.addAction(r)
            a.triggered.connect(lambda _=False, x=r: self.open_input(x))

    def _add_recent(self, text):
        rec = self.settings.value("recent", []) or []
        if isinstance(rec, str):
            rec = [rec]
        rec = [text] + [r for r in rec if r != text]
        self.settings.setValue("recent", rec[:15])

    def open_file(self):
        d = self.settings.value("last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Abrir Transport Stream", d,
                                              "Transport Stream (*.ts *.trp *.bts *.tp *.m2ts *.mts *.mpg *.bin);;Todos (*)")
        if path:
            self.settings.setValue("last_dir", os.path.dirname(path))
            self.open_input(path)

    def open_network(self):
        rec = self.settings.value("recent", []) or []
        if isinstance(rec, str):
            rec = [rec]
        dlg = NetworkDialog(self, rec)
        if dlg.exec():
            self.open_input(dlg.value())

    def open_input(self, text):
        self.stop_input()
        try:
            spec = SourceSpec(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Entrada inválida", str(exc))
            return
        fast = spec.kind == "file" and not self.realtime_chk.isChecked()
        # arquivos usam o relógio do próprio stream (PCR) para taxas exatas; rede usa o relógio de chegada
        self.an = Analyzer(clock="stream" if spec.kind == "file" else "wall")
        try:
            self.src = open_source(text, self.an, realtime=not fast, loop=self.loop_chk.isChecked())
        except Exception as exc:
            QMessageBox.critical(self, "Erro ao abrir", str(exc))
            self.an = None
            return
        self.input_label = spec.label()
        self.an.log("PIDgeon %s — entrada: %s" % (__version__, self.input_label))
        self.src.start()
        self._add_recent(text)
        self.setWindowTitle("PIDgeon %s — %s" % (__version__, self.input_label))
        self.st_input.setText(self.input_label)
        self._reset_views()
        self.play_btn.setEnabled(not fast and HAVE_AV)

    def stop_input(self):
        self.stop_player()
        self.stop_record()
        self._toggle_mosaic(False)
        if self.src is not None:
            self.src.stop()
            self.src = None

    def toggle_pause(self, on):
        if self.src is not None and hasattr(self.src, "paused"):
            if on:
                self.src.paused.set()
            else:
                self.src.paused.clear()

    def reset_counters(self):
        if self.an is None:
            return
        with self.an.lock:
            for e in self.an.etr.values():
                e.count = 0
                e.last_msg = ""
                e.last_t = None
            for st in self.an.pids.values():
                st.cc_errors = st.tei = st.dup = 0
                st.pcr_int_max = 0.0
                st.pcr_acc_max = 0.0
                st.rate_min = None
                st.rate_max = 0.0
                st.pcr_hist.clear()
                st.unref_reported = False
            self.an.bts_tsp_errors = 0
            self.an.err_hist.clear()
        self.an.log("Contadores zerados pelo usuário")

    def _reset_views(self):
        self.pid_tree.clear()
        self.pid_root = QTreeWidgetItem(self.pid_tree, ["PID Info (0)"])
        self.pid_root.setIcon(0, icon("table", 16))
        f = self.pid_root.font(0)
        f.setBold(True)
        self.pid_root.setFont(0, f)
        self.pid_root.setExpanded(True)
        self.pid_items = {}
        self.svc_tree.clear()
        self.tbl_tree.clear()
        self.epg_tree.clear()
        self.msgs.clear()
        self.msg_seen = 0
        self.svc_sig = self.tables_sig = self.epg_sig = None
        self.prog_cb.clear()
        self.video.clear("Aguardando serviços…")
        self.thumbs = {}
        for i in reversed(range(self.mosaic_grid.count())):
            w = self.mosaic_grid.itemAt(i).widget()
            if w:
                w.deleteLater()
        self.mosaic_cells = {}

    # ================================================================ player
    def _fill_components(self):
        self.video_cb.clear()
        self.audio_cb.clear()
        sid = self.prog_cb.currentData()
        if self.an is None or sid is None:
            return
        with self.an.lock:
            s = self.an.services.get(sid)
            if s is None:
                return
            for es in s.streams:
                st = es["stream_type"]
                lang = es["info"].get("lang") or ""
                txt = "%d (%s%s)" % (es["pid"], stream_type_name(st), (" " + lang) if lang else "")
                if st in VIDEO_STREAM_TYPES:
                    self.video_cb.addItem(txt, es["pid"])
                elif st in AUDIO_STREAM_TYPES:
                    self.audio_cb.addItem(txt, es["pid"])
        self.audio_cb.addItem("(sem áudio)", None)

    def step_service(self, d):
        n = self.prog_cb.count()
        if n:
            self.prog_cb.setCurrentIndex((self.prog_cb.currentIndex() + d) % n)
            if self.player is not None:
                self.start_player()

    def toggle_play(self):
        if self.player is None:
            self.start_player()
        else:
            self.stop_player()

    def start_player(self):
        if self.an is None:
            return
        if not HAVE_AV:
            QMessageBox.warning(self, "Vídeo", "PyAV não instalado — reinstale o PIDgeon com o instalador.")
            return
        self.stop_player()
        sid = self.prog_cb.currentData()
        vpid = self.video_cb.currentData()
        apid = self.audio_cb.currentData()
        if sid is None or (vpid is None and apid is None):
            self.video.clear("Nenhum serviço com vídeo/áudio selecionado")
            return
        pipe = BlockingPipe(16 * 1024 * 1024)
        only = [p for p in (vpid, apid) if p is not None]
        filt = ServiceFilter(self.an, sid, pipe.write_packets, only_pids=only, include_si=False)
        dec = Decoder(pipe, _safe(self.bridge.frame.emit), _safe(self.bridge.info.emit),
                      _safe(self.bridge.levels.emit), _safe(self.bridge.error.emit), audio=apid is not None, deinterlace=self.deint_chk.isChecked(),
                      max_width=self.maxw_cb.currentData())
        dec.audio and setattr(dec.audio, "volume", self.vol.value() / 100.0)
        dec.audio and setattr(dec.audio, "muted", self.mute_btn.isChecked())
        with self.an.lock:
            self.an.taps.append(filt)
        self.player = (dec, pipe, filt, sid, vpid, apid)
        self.player_info = {}
        dec.start()
        self.video.clear("Sincronizando %s…" % self.prog_cb.currentText())
        self.center.setCurrentWidget(self.tv_page)
        self.an.log("Decodificando serviço %s (vídeo %s, áudio %s)" % (self.prog_cb.currentText(), vpid, apid))

    def stop_player(self):
        if self.player is None:
            return
        dec, pipe, filt, *_ = self.player
        if self.an is not None:
            with self.an.lock:
                if filt in self.an.taps:
                    self.an.taps.remove(filt)
        dec.stop()
        pipe.close()
        self.player = None
        self.video.clear("Parado")

    def _on_frame(self, data, w, h, stride):
        if self.player is not None:
            self.video.set_frame(data, w, h, stride)

    def _on_player_info(self, info):
        self.player_info.update(info)

    def _set_volume(self, v):
        if self.player and self.player[0].audio:
            self.player[0].audio.volume = v / 100.0

    def _set_mute(self, on):
        if self.player and self.player[0].audio:
            self.player[0].audio.muted = on

    def toggle_fullscreen(self):
        if getattr(self, "_fs", None) is None:
            fs = QWidget()
            fs.setWindowTitle("PIDgeon — vídeo")
            fs.setStyleSheet("background: black;")
            l = QVBoxLayout(fs)
            l.setContentsMargins(0, 0, 0, 0)
            self.video_holder.removeWidget(self.video)
            l.addWidget(self.video)
            QShortcut(QKeySequence("Escape"), fs, self.toggle_fullscreen)
            QShortcut(QKeySequence("F11"), fs, self.toggle_fullscreen)
            fs.showFullScreen()
            self._fs = fs
        else:
            fs = self._fs
            self._fs = None
            fs.layout().removeWidget(self.video)
            self.video_holder.insertWidget(0, self.video, 1)
            self.video.show()
            fs.close()
            fs.deleteLater()

    # ================================================================ mosaico
    def _toggle_mosaic(self, on):
        if not on or self.an is None:
            if self.mosaic is not None:
                self.mosaic.stop()
                if self.an is not None:
                    with self.an.lock:
                        if self.mosaic.tap in self.an.taps:
                            self.an.taps.remove(self.mosaic.tap)
                self.mosaic = None
            if not on and self.mosaic_chk.isChecked():
                self.mosaic_chk.blockSignals(True)
                self.mosaic_chk.setChecked(False)
                self.mosaic_chk.blockSignals(False)
            return
        if not HAVE_AV:
            return
        self.mosaic = MosaicWorker(self.an, self.bridge)
        with self.an.lock:
            self.an.taps.append(self.mosaic.tap)
        self.mosaic.start()

    def _on_thumb(self, sid, r):
        from PySide6.QtGui import QImage, QPixmap
        data, w, h, stride, native = r
        img = QImage(data, w, h, stride, QImage.Format_RGB888).copy()
        cell = self.mosaic_cells.get(sid)
        if cell is None:
            box = QFrame()
            box.setFrameShape(QFrame.StyledPanel)
            v = QVBoxLayout(box)
            pic = QLabel()
            pic.setFixedSize(320, 180)
            pic.setAlignment(Qt.AlignCenter)
            pic.setStyleSheet("background:black;")
            cap = QLabel()
            cap.setAlignment(Qt.AlignCenter)
            v.addWidget(pic)
            v.addWidget(cap)
            n = len(self.mosaic_cells)
            self.mosaic_grid.addWidget(box, n // 3, n % 3)
            cell = self.mosaic_cells[sid] = (pic, cap)
        pic, cap = cell
        pic.setPixmap(QPixmap.fromImage(img).scaled(320, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        name = ""
        if self.an is not None:
            s = self.an.services.get(sid)
            name = s.label if s else ""
        cap.setText("<b>%s</b><br>0x%04X — %dx%d — %s" % (name, sid, native[0], native[1],
                                                          datetime.datetime.now().strftime("%H:%M:%S")))

    # ================================================================ gravação
    def _choose_rec_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Pasta de gravação", self.rec_dir)
        if d:
            self.rec_dir = d
            self.rec_dir_lbl.setText(d)
            self.settings.setValue("rec_dir", d)

    def start_record(self):
        if self.an is None:
            QMessageBox.information(self, "Gravação", "Abra uma entrada primeiro.")
            return
        self.stop_record()
        mode = self.rec_mode.currentData()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(self.rec_dir, exist_ok=True)
        kw = dict(split_seconds=self.split_min.value() * 60, split_mb=self.split_mb.value())
        if mode in ("full_raw", "full_188"):
            ext = ".bts" if (mode == "full_raw" and self.an.packet_size == 204) else ".ts"
            path = os.path.join(self.rec_dir, "mux_%s%s" % (ts, ext))
            rec = Recorder(self.an, path, "full", keep_raw=(mode == "full_raw"), **kw)
        elif mode == "service":
            sid = self.prog_cb.currentData()
            if sid is None:
                QMessageBox.information(self, "Gravação", "Selecione um programa na aba Decodificação.")
                return
            path = os.path.join(self.rec_dir, "servico_0x%04X_%s.ts" % (sid, ts))
            rec = Recorder(self.an, path, "service", sid=sid, **kw)
        else:
            if self.sel_pid is None:
                QMessageBox.information(self, "Gravação", "Selecione um PID na lista à esquerda.")
                return
            path = os.path.join(self.rec_dir, "pid_%d_%s.ts" % (self.sel_pid, ts))
            rec = Recorder(self.an, path, "pids", pids=[self.sel_pid], **kw)
        with self.an.lock:
            self.an.taps.append(rec)
        self.recorder = rec

    def stop_record(self):
        if self.recorder is None:
            return
        if self.an is not None:
            with self.an.lock:
                if self.recorder in self.an.taps:
                    self.an.taps.remove(self.recorder)
        self.recorder.close()
        self.rec_status.setText("Último: %s (%.1f MB)" % (os.path.basename(self.recorder.files[-1]),
                                                          self.recorder.bytes / 1e6))
        self.recorder = None
        self.st_rec.setText("")

    # ================================================================ exportações
    def export_report(self):
        if self.an is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Salvar relatório",
                                              os.path.expanduser("~/pidgeon_relatorio.json"), "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.an.to_report(), f, ensure_ascii=False, indent=2, default=str)
            self.an.log("Relatório salvo em %s" % path)

    def export_etr_csv(self):
        if self.an is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Exportar TR 101 290",
                                              os.path.expanduser("~/pidgeon_tr101290.csv"), "CSV (*.csv)")
        if not path:
            return
        with self.an.lock, open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["item", "prioridade", "indicador", "contagem", "ultima_ocorrencia", "detalhe"])
            for e in self.an.etr.values():
                w.writerow([e.code, e.priority, e.name, e.count,
                            e.last_wall.strftime("%Y-%m-%d %H:%M:%S") if e.last_wall else "", e.last_msg])

    def generate_test(self):
        path, _ = QFileDialog.getSaveFileName(self, "Gerar stream de teste ISDB-Tb",
                                              os.path.expanduser("~/pidgeon_teste.bts"),
                                              "BTS 204 (*.bts);;TS 188 (*.ts)")
        if not path:
            return
        self._log_local("Gerando stream de teste em %s …" % path)

        def work():
            try:
                from ..tools.testgen import generate
                generate(path, 15, ts188=path.lower().endswith(".ts"), log=lambda m: None)
                self.bridge.gen_done.emit(path, "")
            except Exception as exc:
                self.bridge.gen_done.emit(path, str(exc))
        threading.Thread(target=work, daemon=True).start()

    def _on_gen_done(self, path, err):
        if err:
            QMessageBox.warning(self, "Gerar teste", "Falha: %s" % err)
        else:
            self.open_input(path)

    def _toggle_theme(self, on):
        self.dark = on
        self.settings.setValue("dark", on)
        theme.apply(QApplication.instance(), on)
        self.update()

    def _log_local(self, text, level="info"):
        if self.an is not None:
            self.an.log(text, level)
        else:
            self.msgs.appendPlainText("%s %s" % (datetime.datetime.now().strftime("%d/%m/%Y - %H:%M:%S"), text))

    # ================================================================ seleção
    def _pid_selected(self):
        items = self.pid_tree.selectedItems()
        if not items:
            return
        it = items[0]
        while it is not None and it.data(0, Qt.UserRole) is None:
            it = it.parent()
        if it is None:
            return
        self.sel_pid = it.data(0, Qt.UserRole)
        self.refresh()

    def _svc_double(self, item, col):
        sid = item.data(0, Qt.UserRole)
        if sid is None:
            return
        i = self.prog_cb.findData(sid)
        if i >= 0:
            self.prog_cb.setCurrentIndex(i)
            self.start_player()

    def _grid_double(self, row, col):
        it = self.grid.item(row, 0)
        if it is not None:
            self._svc_double(it, 0)

    # ================================================================ atualização
    def refresh(self):
        an = self.an
        if an is None:
            return
        with an.lock:
            self._refresh_status(an)
            self._refresh_messages(an)
            self._refresh_pids(an)
            self._refresh_services(an)
            self._refresh_trace(an)
            cur = self.center.currentWidget()
            if cur is self.tv_page:
                self._refresh_tv(an)
            elif cur is self.pid_page:
                self._refresh_pid_page(an)
            elif cur is self.ts_tree:
                self._refresh_ts(an)
            elif cur is self.grid:
                self._refresh_grid(an)
            elif cur is self.isdb_page:
                self._refresh_isdb(an)
            elif cur is self.pcr_page:
                self._refresh_pcr(an)
            elif cur is self.etr_page:
                self._refresh_etr(an)
            elif cur is self.epg_tree:
                self._refresh_epg(an)
            if self.left.currentWidget() is self.tbl_tree:
                self._refresh_tables(an)
        if self.recorder is not None:
            r = self.recorder
            self.st_rec.setText("● REC %.1f MB" % (r.bytes / 1e6))
            self.st_rec.setStyleSheet("color: %s; font-weight: 600;" % theme.CURRENT["err"])
            self.rec_status.setText("Gravando %s — %.1f MB, %d arquivo(s)" % (
                os.path.basename(r.files[-1]), r.bytes / 1e6, len(r.files)))

    def _refresh_status(self, an):
        ok = an.sync_locked and (self.src is None or self.src.signal is not False)
        recent_err = any(e.count and e.last_t is not None and an.t - e.last_t < 3 and e.priority == 1
                         for e in an.etr.values())
        col = theme.CURRENT["err"] if (not ok or recent_err) else theme.CURRENT["ok"]
        if an.total == 0:
            col = "#9ca3af"
        self.st_lamp.setPixmap(lamp_pixmap(col))
        self.st_size.setText("%s Byte" % (an.packet_size or "-"))
        occ = (100.0 * an.nonnull_rate / an.total_rate) if an.total_rate else 0
        self.st_rate.setText("%s bps / %.1f%%" % (fmt_int(an.total_rate), occ))
        self.st_rate.setToolTip("Taxa TS (188) e ocupação útil (sem pacotes nulos)")
        self.st_fmt.setText("BTS ISDB-T" if an.is_bts else ("M2TS" if an.packet_size == 192 else "MPEG-TS"))
        tot = an.tot_now()
        self.st_tot.setText(("TOT %s" % tot.strftime("%d/%m/%Y %H:%M:%S")) if tot else "")
        if self.src is not None and self.src.finished and not self.src.error:
            self.st_text.setText("Fim da entrada — %s pacotes" % fmt_int(an.total))
        elif self.src is not None and self.src.error:
            self.st_text.setText("Erro: %s" % self.src.error)
        else:
            self.st_text.setText("Analisando — %s pacotes, %s" % (fmt_int(an.total),
                                 str(datetime.timedelta(seconds=int(an.elapsed())))))

    def _refresh_messages(self, an):
        new = an.msg_serial - self.msg_seen
        if new <= 0:
            return
        msgs = list(an.messages)[-min(new, len(an.messages)):]
        self.msg_seen = an.msg_serial
        colors = {"error": theme.CURRENT["err"], "warn": theme.CURRENT["warn"]}
        for ts, lvl, txt in msgs:
            line = "%s %s" % (ts.strftime("%d/%m/%Y - %H:%M:%S"), txt)
            if lvl in colors:
                self.msgs.appendHtml('<span style="color:%s">%s</span>' % (colors[lvl], _esc(line)))
            else:
                self.msgs.appendPlainText(line)

    def _pid_text(self, an, pid, st):
        kind, desc = an.describe_pid(pid)
        layer = an.layer_of(pid)
        pct = (100.0 * st.rate / an.total_rate) if an.total_rate else 0.0
        return kind, "(%s) %d  %s  (%s / %.2f%%)" % (layer or "?", pid, desc, fmt_bps(st.rate), pct)

    def _refresh_pids(self, an):
        self.pid_root.setText(0, "PID Info (%d)" % len(an.pids))
        for pid in sorted(an.pids):
            st = an.pids[pid]
            kind, text = self._pid_text(an, pid, st)
            it = self.pid_items.get(pid)
            if it is None:
                # mantém a ordem numérica
                idx = 0
                keys = sorted(self.pid_items)
                while idx < len(keys) and keys[idx] < pid:
                    idx += 1
                it = QTreeWidgetItem()
                self.pid_root.insertChild(idx, it)
                it.setData(0, Qt.UserRole, pid)
                it.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                self.pid_items[pid] = it
            if it.text(0) != text:
                it.setText(0, text)
            it.setIcon(0, icon({"psi": "table", "video": "video", "audio": "audio", "pcr": "clock",
                                "data": "data", "null": "null", "iip": "isdb"}.get(kind, "unknown"), 16))
            err = st.cc_errors or st.tei
            if err:
                it.setForeground(0, QBrush(QColor(theme.CURRENT["err"])))
            else:
                it.setData(0, Qt.ForegroundRole, None)
            if it.isExpanded():
                self._fill_pid_children(an, it, pid, st)

    def _fill_pid_children(self, an, it, pid, st):
        rows = [
            ("Pacotes", fmt_int(st.count)),
            ("Bitrate", "%s (mín %s, máx %s)" % (fmt_bps(st.rate), fmt_bps(st.rate_min or 0), fmt_bps(st.rate_max))),
            ("Erros CC", fmt_int(st.cc_errors)),
            ("TEI", fmt_int(st.tei)),
            ("Embaralhados", fmt_int(st.scrambled)),
            ("PUSI", fmt_int(st.pusi)),
            ("Descontinuidades", fmt_int(st.discont)),
        ]
        if st.pcr_count:
            rows.append(("PCR", "%s, intervalo máx %.1f ms, precisão máx %.0f ns" % (
                fmt_int(st.pcr_count), st.pcr_int_max * 1000, st.pcr_acc_max)))
        if an.is_bts:
            dist = ", ".join("%s: %s" % (LAYER_NAMES.get(i, "D%d" % i), fmt_int(n))
                             for i, n in enumerate(st.layers) if n)
            rows.append(("Camadas ISDB-T", dist))
        svcs = an.pid_services(pid)
        if svcs:
            rows.append(("Serviços", ", ".join("%s (0x%04X)" % (s.label, s.sid) for s in svcs)))
        s, es = an.stream_of(pid)
        if es:
            rows.append(("stream_type", "0x%02X %s" % (es["stream_type"], stream_type_name(es["stream_type"]))))
            tag = es["info"].get("component_tag")
            if tag is not None:
                rows.append(("component_tag", "0x%02X %s" % (tag, component_tag_kind(tag) or "")))
        while it.childCount() < len(rows):
            QTreeWidgetItem(it)
        for i, (k, v) in enumerate(rows):
            it.child(i).setText(0, "%s: %s" % (k, v))
        while it.childCount() > len(rows):
            it.removeChild(it.child(it.childCount() - 1))

    def _refresh_services(self, an):
        sig = tuple((s.sid, s.name, s.pmt_version, len(s.streams), an.pat.get(s.sid)) for s in
                    sorted(an.services.values(), key=lambda x: x.sid))
        if sig == self.svc_sig:
            return
        self.svc_sig = sig
        cur = self.prog_cb.currentData()
        self.prog_cb.blockSignals(True)
        self.prog_cb.clear()
        self.svc_tree.clear()
        for s in sorted(an.services.values(), key=lambda x: x.sid):
            if s.sid not in an.pat:
                continue
            one = an.is_one_seg(s)
            label = "%s (ID: %d)%s" % (s.label, s.sid, " [1-seg]" if one else "")
            self.prog_cb.addItem(label, s.sid)
            top = QTreeWidgetItem(self.svc_tree, [label, describe_service_type(s.type)])
            top.setData(0, Qt.UserRole, s.sid)
            top.setIcon(0, icon("video" if s.video_streams() else "audio", 16))
            QTreeWidgetItem(top, ["Provedor", s.provider])
            QTreeWidgetItem(top, ["PMT PID", "%s" % s.pmt_pid])
            QTreeWidgetItem(top, ["PCR PID", "%s" % s.pcr_pid])
            for es in s.streams:
                st = es["stream_type"]
                tag = es["info"].get("component_tag")
                c = QTreeWidgetItem(top, ["PID %d (0x%04X)" % (es["pid"], es["pid"]),
                                          "0x%02X %s%s%s" % (st, stream_type_name(st),
                                                             (" — tag 0x%02X %s" % (tag, component_tag_kind(tag) or "")) if tag is not None else "",
                                                             (" — " + es["info"]["lang"]) if es["info"].get("lang") else "")])
                c.setIcon(0, icon("video" if st in VIDEO_STREAM_TYPES else "audio" if st in AUDIO_STREAM_TYPES else "data", 16))
            top.setExpanded(True)
        i = self.prog_cb.findData(cur)
        self.prog_cb.setCurrentIndex(i if i >= 0 else 0)
        self.prog_cb.blockSignals(False)
        self._fill_components()
        if self.player is None and self.prog_cb.count() and self.video.img is None:
            self.video.clear("Selecione um programa e clique em ▶ Reproduzir (aba Decodificação)")

    def _refresh_trace(self, an):
        if not self.trace_dock.isVisible():
            return
        series = [("Total", "#22c55e", an.total_hist), ("Útil", "#f59e0b", an.nonnull_hist)]
        if self.sel_pid is not None and self.sel_pid in an.pids:
            series.append(("PID %d" % self.sel_pid, "#ef4444", an.pids[self.sel_pid].history))
        self.trace_rate.set_series(series)
        self.trace_err.set_series([("", "#ef4444", an.err_hist)])

    def _refresh_tv(self, an):
        sid = self.prog_cb.currentData()
        s = an.services.get(sid) if sid is not None else None
        pi = self.player_info
        rate = ""
        if self.player is not None:
            vpid = self.player[4]
            if vpid in an.pids:
                rate = fmt_bps(an.pids[vpid].rate)
        res = ("%sx%s%s" % (pi.get("width"), pi.get("height"), "i" if pi.get("interlaced") else "p")) if pi.get("width") else "-"
        codec = (pi.get("vcodec") or "-").upper()
        if pi.get("vprofile"):
            codec += " " + str(pi["vprofile"])
        audio = "-"
        if pi.get("acodec"):
            audio = "%s %s Hz %s" % (pi["acodec"].upper(), pi.get("arate"), pi.get("alayout") or "")
        fps = pi.get("render_fps")
        self.tv_info.setText(
            "<b>Svc:</b> %s &nbsp;&nbsp; <b>Res:</b> %s &nbsp;&nbsp; <b>Rate:</b> %s &nbsp;&nbsp; <b>Codec:</b> %s"
            " &nbsp;&nbsp; <b>Áudio:</b> %s &nbsp;&nbsp; <b>FPS:</b> %s%s" % (
                _esc(s.label if s else "-"), res, rate or "-", _esc(codec), _esc(audio),
                ("%.1f" % fps) if fps else "-",
                (" &nbsp;&nbsp; <b>Agora:</b> %s" % _esc(s.present["name"])) if (s and s.present) else ""))

    def _refresh_pid_page(self, an):
        pid = self.sel_pid
        self.pid_info.clear()
        if pid is None or pid not in an.pids:
            self.pid_hex.setPlainText("Selecione um PID na lista à esquerda.")
            return
        st = an.pids[pid]
        kind, desc = an.describe_pid(pid)
        rows = [("PID", "%d (0x%04X)" % (pid, pid)), ("Tipo", desc), ("Camada ISDB-T", an.layer_of(pid) or "-"),
                ("Pacotes", fmt_int(st.count)), ("Bitrate atual", fmt_bps(st.rate)),
                ("Bitrate mín / máx", "%s / %s" % (fmt_bps(st.rate_min or 0), fmt_bps(st.rate_max))),
                ("% do mux", "%.3f%%" % ((100.0 * st.rate / an.total_rate) if an.total_rate else 0)),
                ("Erros de continuidade", fmt_int(st.cc_errors)), ("Transport errors (TEI)", fmt_int(st.tei)),
                ("Pacotes embaralhados", fmt_int(st.scrambled)), ("PCRs", fmt_int(st.pcr_count))]
        if st.pcr_count:
            rows += [("Intervalo PCR último / máx", "%.2f / %.2f ms" % ((st.pcr_int_last or 0) * 1000, st.pcr_int_max * 1000)),
                     ("Precisão PCR última / máx", "%s / %.0f ns" % (
                         ("%.0f" % st.pcr_acc_last) if st.pcr_acc_last is not None else "-", st.pcr_acc_max))]
        if st.last_pts is not None:
            rows.append(("Último PTS", "%d (%.3f s)" % (st.last_pts, st.last_pts / 90000.0)))
        for k, v in rows:
            QTreeWidgetItem(self.pid_info, [k, str(v)])
        self.pid_graph.set_series([("PID %d" % pid, "#ef4444", st.history)])
        raw = st.last_raw
        if raw is not None:
            size = len(raw)
            off = 4 if size == 192 else 0
            ts = raw[off:off + 188]
            b1, b3 = ts[1], ts[3]
            hdr = ("Cabeçalho: sync 0x%02X  TEI %d  PUSI %d  prioridade %d  PID %d  scrambling %d  "
                   "adaptation %d  CC %d" % (ts[0], b1 >> 7, (b1 >> 6) & 1, (b1 >> 5) & 1,
                                              ((b1 & 0x1F) << 8) | ts[2], b3 >> 6, (b3 >> 4) & 3, b3 & 15))
            txt = hdr + "\n\n" + hexdump(ts)
            if size == 204:
                info = BTSInfo(raw[188:196])
                txt += "\n\nISDB-T information (bytes 188–195):\n" + info.to_node().dump(1)
                txt += "\n\nParidade/dummy (bytes 196–203): " + raw[196:204].hex(" ").upper()
            elif size == 192:
                txt = "TP_extra_header: %s\n\n" % raw[:4].hex(" ").upper() + txt
            self.pid_hex.setPlainText(txt)

    def _refresh_ts(self, an):
        t = self.ts_tree
        t.clear()

        def sec(title, rows):
            top = QTreeWidgetItem(t, [title])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            for k, v in rows:
                QTreeWidgetItem(top, [k, "" if v is None else str(v)])
            top.setExpanded(True)

        src = self.src
        rows = [("Entrada", getattr(self, "input_label", "-")),
                ("Tamanho do pacote", "%s bytes" % an.packet_size),
                ("Sincronismo", "travado" if an.sync_locked else "procurando"),
                ("Bytes recebidos", fmt_int(an.bytes_in)), ("Pacotes", fmt_int(an.total)),
                ("Tempo de análise", str(datetime.timedelta(seconds=int(an.elapsed()))))]
        if src is not None and hasattr(src, "rtp_detected"):
            rows += [("Datagramas", fmt_int(src.datagrams)),
                     ("Encapsulamento", "RTP (PT %s)" % src.rtp_pt if src.rtp_detected else "UDP"),
                     ("Datagramas RTP perdidos", fmt_int(src.rtp_lost))]
        if an.framer.sync_losses or an.framer.bytes_skipped:
            rows += [("Perdas de sync", an.framer.sync_losses), ("Bytes descartados", fmt_int(an.framer.bytes_skipped))]
        sec("Entrada", rows)
        sec("Multiplex", [
            ("Bitrate TS (188)", "%s bps" % fmt_int(an.total_rate)),
            ("Bitrate bruto (%s B)" % an.packet_size, "%s bps" % fmt_int(an.raw_rate)),
            ("Bitrate útil (sem nulos)", "%s bps" % fmt_int(an.nonnull_rate)),
            ("Taxa pelo PCR", ("%s bps" % fmt_int(an.mux_rate)) if an.mux_rate else "-"),
            ("transport_stream_id", "%s" % (("%d (0x%04X)" % (an.tsid, an.tsid)) if an.tsid is not None else "-")),
            ("original_network_id", "%s" % (("%d (0x%04X)" % (an.onid, an.onid)) if an.onid is not None else "-")),
            ("Programas na PAT", len([p for p in an.pat if p])), ("PIDs", len(an.pids)),
        ])
        freqs = ", ".join("%.3f MHz" % f for f in an.frequencies) if an.frequencies else "-"
        sec("Rede (NIT)", [
            ("network_id", ("%d (0x%04X)" % (an.network_id, an.network_id)) if an.network_id is not None else "-"),
            ("Nome da rede", an.network_name or "-"), ("Nome do TS", an.ts_name or "-"),
            ("Tecla do controle (canal virtual)", an.remote_key if an.remote_key is not None else "-"),
            ("Frequência(s)", freqs),
            ("Código de área", ("0x%03X" % an.area_code) if an.area_code is not None else "-"),
            ("Recepção parcial (1-seg)", ", ".join("0x%04X" % s for s in an.partial_sids) or "-"),
        ])
        tot = an.tot_now()
        sec("Tempo", [("TOT/TDT (horário do stream)", tot.strftime("%d/%m/%Y %H:%M:%S") if tot else "-"),
                      ("Offset local (LTO)", an.lto or "-")])
        if an.broadcasters:
            sec("Emissoras (BIT)", [("broadcaster", b) for b in an.broadcasters])
        if an.ait_apps:
            sec("Aplicações interativas (AIT / Ginga)", [("org 0x%08X app 0x%04X" % (o, a), "control %d" % c)
                                                         for o, a, c in an.ait_apps])
        if an.emergency:
            sec("Alerta de emergência (EWBS)", [("serviço 0x%04X" % e[0], "ATIVO" if e[1] else "inativo")
                                                for e in an.emergency])
        t.resizeColumnToContents(0)

    def _refresh_grid(self, an):
        svcs = [s for s in sorted(an.services.values(), key=lambda x: x.sid) if s.sid in an.pat]
        cols = ["ID", "Nome", "Provedor", "Tipo", "Camada", "PMT", "PCR", "Vídeo", "Áudio", "Outros", "Bitrate", "Agora"]
        self.grid.setColumnCount(len(cols))
        self.grid.setHorizontalHeaderLabels(cols)
        self.grid.setRowCount(len(svcs))
        for r, s in enumerate(svcs):
            pids = {es["pid"] for es in s.streams}
            if s.pcr_pid is not None:
                pids.add(s.pcr_pid)
            if s.pmt_pid is not None:
                pids.add(s.pmt_pid)
            rate = sum(an.pids[p].rate for p in pids if p in an.pids)
            vids = ", ".join("%d %s" % (e["pid"], stream_type_name(e["stream_type"]).split()[0]) for e in s.video_streams())
            auds = ", ".join("%d%s" % (e["pid"], (" " + e["info"]["lang"]) if e["info"].get("lang") else "") for e in s.audio_streams())
            other = ", ".join(str(e["pid"]) for e in s.streams
                              if e["stream_type"] not in VIDEO_STREAM_TYPES and e["stream_type"] not in AUDIO_STREAM_TYPES)
            vals = ["0x%04X (%d)" % (s.sid, s.sid), s.label + (" [1-seg]" if an.is_one_seg(s) else ""), s.provider,
                    describe_service_type(s.type), an.service_layer(s) or "-", str(s.pmt_pid), str(s.pcr_pid),
                    vids, auds, other, fmt_bps(rate), s.present["name"] if s.present else ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    it.setData(Qt.UserRole, s.sid)
                self.grid.setItem(r, c, it)
        self.grid.resizeColumnsToContents()

    def _refresh_isdb(self, an):
        iip = an.iip or {}
        layers = iip.get("layers_info") or [None, None, None]
        names = ("A", "B", "C")
        for r in range(5):
            if r < 3:
                l = layers[r] if r < len(layers) else None
                meas = an.layer_rates[r + 1]
                if l:
                    vals = [l["mod"], l["cr"], str(l["segs"]), str(l["ti"]), fmt_bps(l["capacity"]), fmt_bps(meas)]
                    occ = (an.layer_rates[r + 1] and 0) or 0
                    _ = occ
                    used = 0.0
                    for st in an.pids.values():
                        if st.pid != NULL_PID and st.count:
                            used += st.rate * (st.layers[r + 1] / st.count)
                    frac = used / l["capacity"] if l["capacity"] else 0
                    self.layer_bars[r].setValue(int(min(1.0, frac) * 1000))
                    self.layer_bars[r].setFormat("%.1f%% útil" % (frac * 100))
                else:
                    vals = ["não usada" if iip else "-", "", "", "", "", fmt_bps(meas)]
                    self.layer_bars[r].setValue(0)
                    self.layer_bars[r].setFormat("-")
            else:
                key = 0 if r == 3 else 8
                vals = ["", "", "", "", "", fmt_bps(an.layer_rates[key])]
                self.layer_bars[r].setValue(int(1000 * an.layer_rates[key] / an.total_rate) if an.total_rate else 0)
                self.layer_bars[r].setFormat("%.1f%% do BTS" % ((100.0 * an.layer_rates[key] / an.total_rate) if an.total_rate else 0))
            for c, v in enumerate(vals):
                self.layer_tbl.setItem(r, c + 1, QTableWidgetItem(v))
        if not an.is_bts:
            self.isdb_summary.setText("<i>A entrada não é BTS de 204 bytes — informações de camada e IIP "
                                      "só existem no BTS (saída do multiplexador ISDB-T).</i>")
        else:
            tmcc = "-"
            if an.bts_last is not None:
                tmcc = TMCC_IDENTIFIER.get(an.bts_last[0] >> 6, "?")
            self.isdb_summary.setText(
                "<b>Modo:</b> %s &nbsp; <b>Intervalo de guarda:</b> %s &nbsp; <b>Recepção parcial:</b> %s<br>"
                "<b>Quadros:</b> %s &nbsp; <b>TSP/quadro:</b> %s &nbsp; <b>Erros do contador TSP:</b> %s &nbsp; "
                "<b>TMCC:</b> %s &nbsp; <b>EWBS:</b> %s &nbsp; <b>IIP:</b> %s pacotes%s" % (
                    ISDB_MODES.get(iip.get("mode"), "-"), ISDB_GUARD.get(iip.get("gi"), "-"),
                    ("sim" if iip.get("partial") else "não") if iip else "-",
                    fmt_int(an.bts_frames), an.bts_pkts_per_frame or "-", fmt_int(an.bts_tsp_errors), tmcc,
                    '<span style="color:%s"><b>LIGADO</b></span>' % theme.CURRENT["err"] if an.bts_emergency else "desligado",
                    fmt_int(an.iip_count), "" if iip.get("crc_ok", True) else " (CRC ERRO)"))
        self.layer_graph.set_series([(n, LAYER_COLORS[n], an.layer_hist[k]) for n, k in
                                     (("A", 1), ("B", 2), ("C", 3), ("Nulo", 0))])
        if an.iip_node is not None and self.iip_tree.property("sig") != id(an.iip_node):
            self.iip_tree.setProperty("sig", id(an.iip_node))
            self.iip_tree.clear()
            node_to_item(an.iip_node, self.iip_tree)
            self.iip_tree.expandAll()
        if an.bts_last is not None:
            self.bts_tree.clear()
            node_to_item(BTSInfo(an.bts_last).to_node(), self.bts_tree)
            self.bts_tree.expandAll()

    def _refresh_pcr(self, an):
        pcr_pids = sorted(p for p, st in an.pids.items() if st.pcr_count)
        cur = self.pcr_cb.currentData()
        if [self.pcr_cb.itemData(i) for i in range(self.pcr_cb.count())] != pcr_pids:
            self.pcr_cb.blockSignals(True)
            self.pcr_cb.clear()
            for p in pcr_pids:
                svc = an.pid_services(p)
                self.pcr_cb.addItem("%d (0x%04X)%s" % (p, p, (" — " + svc[0].label) if svc else ""), p)
            i = self.pcr_cb.findData(cur)
            self.pcr_cb.setCurrentIndex(i if i >= 0 else 0)
            self.pcr_cb.blockSignals(False)
        pid = self.pcr_cb.currentData()
        if pid is None or pid not in an.pids:
            return
        st = an.pids[pid]
        hist = list(st.pcr_hist)[-600:]
        self.pcr_int.set_series([("intervalo", "#22c55e", [h[1] for h in hist])])
        self.pcr_acc.set_series([("precisão", "#f59e0b", [h[2] for h in hist])])
        self.pcr_jit.set_series([("jitter", "#38bdf8", [h[3] for h in hist])])
        self.pcr_stats.setText("PCRs: %s &nbsp; intervalo máx: %.2f ms &nbsp; precisão máx: %.0f ns &nbsp; "
                               "jitter atual: %s &nbsp; taxa pelo PCR: %s" % (
                                   fmt_int(st.pcr_count), st.pcr_int_max * 1000, st.pcr_acc_max,
                                   ("%.3f ms" % (st.pcr_jitter * 1000)) if st.pcr_jitter is not None else "-",
                                   fmt_bps(an.mux_rate) if an.mux_rate else "-"))
        self.pcr_stats.setTextFormat(Qt.RichText)

    def _refresh_etr(self, an):
        items = list(an.etr.values())
        self.etr_page.setRowCount(len(items))
        c = theme.CURRENT
        for r, e in enumerate(items):
            if e.count == 0:
                col = c["ok"]
            elif e.last_t is not None and an.t - e.last_t < 5:
                col = c["err"]
            else:
                col = c["warn"]
            lamp = QTableWidgetItem()
            lamp.setIcon(QIconFromPix(lamp_pixmap(col, 14)))
            self.etr_page.setItem(r, 0, lamp)
            vals = [e.code, str(e.priority), e.name, fmt_int(e.count),
                    e.last_wall.strftime("%d/%m %H:%M:%S") if e.last_wall else "-",
                    e.last_msg or e.desc]
            for ci, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if ci == 3 and e.count:
                    it.setForeground(QBrush(QColor(col)))
                self.etr_page.setItem(r, ci + 1, it)
        self.etr_page.resizeColumnsToContents()
        self.etr_page.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)

    def _refresh_epg(self, an):
        sig = tuple((s.sid, len(s.events), id(s.present)) for s in an.services.values())
        if sig == self.epg_sig:
            return
        self.epg_sig = sig
        self.epg_tree.clear()
        offset = datetime.timedelta(0)  # horários exibidos como transmitidos (EIT/TOT)
        for s in sorted(an.services.values(), key=lambda x: x.sid):
            if not s.events:
                continue
            top = QTreeWidgetItem(self.epg_tree, ["%s (0x%04X)" % (s.label, s.sid)])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            evs = sorted(s.events.values(), key=lambda e: e["start"] or datetime.datetime.min)
            for ev in evs:
                st = (ev["start"] + offset).strftime("%d/%m %H:%M") if ev["start"] else "-"
                mark = "▶ " if (s.present and ev["event_id"] == s.present["event_id"]) else ""
                desc = ev["text"] or ""
                if ev.get("ext_text"):
                    desc += " " + ev["ext_text"]
                QTreeWidgetItem(top, [mark + (ev["name"] or "(sem nome)"), st, str(ev["duration"] or "-"),
                                      ev.get("genre", ""), ev.get("rating", ""), desc])
            top.setExpanded(True)

    def _refresh_tables(self, an):
        sig = tuple(sorted((k, ti.version, len(ti.sections)) for k, ti in an.tables.items()))
        if sig == self.tables_sig:
            return
        self.tables_sig = sig
        self.tbl_tree.clear()
        by_pid = {}
        for k, ti in sorted(an.tables.items()):
            by_pid.setdefault(ti.pid, []).append(ti)
        for pid, tis in sorted(by_pid.items()):
            kind, desc = an.describe_pid(pid)
            top = QTreeWidgetItem(self.tbl_tree, ["PID %d (0x%04X) %s" % (pid, pid, desc), "%d tabela(s)" % len(tis)])
            top.setIcon(0, icon("table", 16))
            for ti in tis:
                tt = QTreeWidgetItem(top, ["%s ext 0x%04X" % (ti.name, ti.ext),
                                           "versão %s, %d seção(ões), %s recebidas%s" % (
                                               ti.version, len(ti.sections), fmt_int(ti.count),
                                               (", %d CRC erro" % ti.crc_errors) if ti.crc_errors else "")])
                for num in sorted(ti.sections):
                    node_to_item(ti.sections[num], tt)
        self.tbl_tree.resizeColumnToContents(0)

    # ================================================================ fim
    def closeEvent(self, ev):
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop_input()
        super().closeEvent(ev)


def _safe(fn):
    """Evita exceções de sinais emitidos por threads depois que a janela foi fechada."""
    def call(*a):
        try:
            fn(*a)
        except RuntimeError:
            pass
    return call


def QIconFromPix(pm):
    from PySide6.QtGui import QIcon
    return QIcon(pm)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_ = (QAction, QSizePolicy, MosaicWorker)
