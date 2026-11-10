"""Seletor de adaptador de rede que se atualiza sozinho e janela de diagnóstico de rede."""

import shlex
import subprocess
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QToolButton, QLabel, QDialog,
                               QFormLayout, QLineEdit, QSpinBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QHeaderView, QListWidget, QListWidgetItem, QCheckBox, QMessageBox, QGroupBox)

from ..core import netif
from .icons import icon

AUTO_LABEL = "Automático (acompanha troca de placa, cabo e IP)"


class InterfaceCombo(QWidget):
    """Combo de adaptadores: relê o sistema a cada 2 s e guarda a escolha pelo NOME do adaptador.

    value(): "" (automático) ou o nome (enp3s0, enx00e04c…, wlp2s0, enp3s0.100).
    """

    changed = Signal()

    def __init__(self, parent=None, group_getter=None, show_status=True):
        super().__init__(parent)
        self.group_getter = group_getter or (lambda: None)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(320)
        h.addWidget(self.combo, 1)
        b = QToolButton()
        b.setIcon(icon("reset", 16))
        b.setToolTip("Reler os adaptadores agora")
        b.clicked.connect(lambda: self.refresh(force=True))
        h.addWidget(b)
        lay.addLayout(h)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: palette(mid);")
        self.status.setVisible(show_status)
        lay.addWidget(self.status)
        self._wanted = ""
        self._snap = None
        self.combo.currentIndexChanged.connect(self._picked)
        self.refresh(force=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)

    def _picked(self, _i):
        if self.combo.signalsBlocked():
            return
        self._wanted = self.combo.currentData() or ""
        self._update_status()
        self.changed.emit()

    def value(self):
        return self._wanted

    def set_value(self, name_or_ip):
        """Aceita nome ou, por compatibilidade, o IP salvo em versões anteriores."""
        v = name_or_ip or ""
        if v and netif.is_ipv4(v):
            i = netif.find(v)
            v = i.name if i else ""
        self._wanted = v
        self.refresh(force=True)
        self.changed.emit()

    def refresh(self, force=False):
        snap = netif.snapshot()
        if snap == self._snap and not force:
            self._update_status()
            return
        self._snap = snap
        ifs = [i for i in netif.interfaces(include_loopback=True) if not i.virtual or i.ipv4]
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(icon("network", 16), AUTO_LABEL, "")
        names = []
        for i in ifs:
            self.combo.addItem(i.label(), i.name)
            names.append(i.name)
            if not i.usable and not i.loopback:
                self.combo.setItemData(self.combo.count() - 1, QColor("#9ca3af"), Qt.ForegroundRole)
        if self._wanted and self._wanted not in names:
            self.combo.addItem("%s — não encontrado nesta máquina (aguardando)" % self._wanted, self._wanted)
            self.combo.setItemData(self.combo.count() - 1, QColor("#dc2626"), Qt.ForegroundRole)
        idx = self.combo.findData(self._wanted)
        self.combo.setCurrentIndex(max(0, idx))
        self.combo.blockSignals(False)
        self._update_status()

    def _update_status(self):
        group = self.group_getter()
        try:
            mc = bool(group) and 224 <= int(group.split(".")[0]) <= 239
        except ValueError:
            mc = False
        if not self._wanted:
            sel = netif.auto_join_set(group if mc else None)
            if sel:
                txt = "→ " + ", ".join("%s (%s)" % (i.name, i.ipv4) for i in sel)
                if len(sel) > 1:
                    txt += " — o PIDgeon trava no adaptador por onde o fluxo chegar"
            else:
                txt = "⚠ nenhum adaptador conectado com IPv4 no momento"
        else:
            r = netif.resolve(self._wanted, group if mc else None)
            txt = ("⚠ " if r.missing else "→ ") + r.note
        self.status.setText(txt)


# ====================================================================== diagnóstico
class _Bridge(QObject):
    done = Signal(list)


class NetDiagDialog(QDialog):
    LEVEL = {"ok": ("✔", "#16a34a"), "info": ("ℹ", None), "warn": ("⚠", "#d97706"), "erro": ("✘", "#dc2626")}

    def __init__(self, parent=None, group="239.1.1.1", port=1234, iface=""):
        super().__init__(parent)
        self.setWindowTitle("Diagnóstico de rede")
        self.setWindowIcon(icon("network"))
        self.resize(900, 640)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.group = QLineEdit(group or "")
        self.group.setPlaceholderText("grupo multicast ou vazio")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(port or 1234)
        self.iface = InterfaceCombo(self, group_getter=lambda: self.group.text().strip())
        self.iface.set_value(iface)
        f.addRow("Grupo / endereço", self.group)
        f.addRow("Porta", self.port)
        f.addRow("Adaptador", self.iface)
        lay.addLayout(f)
        gb = QGroupBox("Adaptadores (atualiza a cada 2 s)")
        v = QVBoxLayout(gb)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Adaptador", "IPv4", "Tipo", "Estado", "Velocidade", "Recebendo",
                                              "rp_filter", "Grupos IGMP"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.table)
        lay.addWidget(gb, 1)
        gb = QGroupBox("Verificação")
        v = QVBoxLayout(gb)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        v.addWidget(self.results)
        h = QHBoxLayout()
        self.run_btn = QPushButton(icon("play", 16), "Verificar")
        self.run_btn.clicked.connect(self.run)
        h.addWidget(self.run_btn)
        self.fix_btn = QPushButton("Corrigir rp_filter (pede a senha)")
        self.fix_btn.setToolTip("Desliga o filtro de caminho reverso no adaptador escolhido, para aceitar multicast "
                                "de fontes em outra sub-rede")
        self.fix_btn.clicked.connect(self.fix_rp)
        h.addWidget(self.fix_btn)
        self.persist = QCheckBox("manter após reiniciar")
        h.addWidget(self.persist)
        h.addStretch(1)
        b = QPushButton("Copiar relatório")
        b.clicked.connect(self.copy)
        h.addWidget(b)
        b = QPushButton("Fechar")
        b.clicked.connect(self.close)
        h.addWidget(b)
        v.addLayout(h)
        lay.addWidget(gb, 1)
        self.bridge = _Bridge()
        self.bridge.done.connect(self._show)
        self._rx = netif.rx_bytes()
        self._rx_t = __import__("time").monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._table)
        self.timer.start(2000)
        self._table()
        QTimer.singleShot(100, self.run)

    def _table(self):
        import time
        now = time.monotonic()
        rx = netif.rx_bytes()
        dt = max(0.1, now - self._rx_t)
        mem = netif.igmp_memberships()
        ifs = [i for i in netif.interfaces() if not i.virtual or i.ipv4]
        self.table.setRowCount(len(ifs))
        for r, i in enumerate(ifs):
            rate = (rx.get(i.name, 0) - self._rx.get(i.name, 0)) * 8 / dt / 1e6 if i.name in self._rx else 0
            state = "conectado" if i.usable else ("sem cabo / desconectado" if i.up else "desligado")
            if i.up and i.carrier and not i.ipv4:
                state = "conectado, sem IPv4"
            rp = netif.rp_filter(i.name)
            groups = [g for g in mem.get(i.name, []) if g not in ("224.0.0.1", "224.0.0.251", "224.0.0.252")]
            vals = [i.name, i.ipv4 and "%s/%s" % (i.ipv4, i.prefix) or "-", i.kind, state,
                    ("%d Mb/s" % i.speed) if i.speed and i.speed > 0 else "-", "%.2f Mbps" % rate,
                    {0: "0 (desligado)", 1: "1 (estrito)", 2: "2 (loose)"}.get(rp, "-"), ", ".join(groups) or "-"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 3:
                    it.setForeground(QColor("#16a34a" if i.usable else "#dc2626"))
                if c == 6 and rp == 1:
                    it.setForeground(QColor("#d97706"))
                self.table.setItem(r, c, it)
        self._rx, self._rx_t = rx, now

    def run(self):
        self.run_btn.setEnabled(False)
        self.results.clear()
        self.results.addItem("Verificando…")
        group = self.group.text().strip() or None
        port = self.port.value()
        iface = self.iface.value()

        def work():
            try:
                res = netif.diagnose(group, port, iface, sample=1.0)
            except Exception as exc:          # nunca derruba a janela
                res = [("erro", "Falha no diagnóstico: %s" % exc)]
            self.bridge.done.emit(res)
        threading.Thread(target=work, daemon=True).start()

    def _show(self, res):
        self.results.clear()
        for lvl, text in res:
            sym, col = self.LEVEL.get(lvl, ("•", None))
            it = QListWidgetItem("%s  %s" % (sym, text))
            if col:
                it.setForeground(QColor(col))
            self.results.addItem(it)
        self.run_btn.setEnabled(True)

    def fix_rp(self):
        name = self.iface.value()
        targets = ["all", "default"]
        if name:
            targets.append(name)
        else:
            targets += [i.name for i in netif.auto_join_set()]
        cmds = ["sysctl -w net.ipv4.conf.%s.rp_filter=0" % t for t in targets]
        if self.persist.isChecked():
            conf = "\\n".join("net.ipv4.conf.%s.rp_filter = 0" % t for t in targets)
            cmds.append("printf '# PIDgeon: aceitar multicast de outras sub-redes\\n%s\\n' > "
                        "/etc/sysctl.d/60-pidgeon-multicast.conf" % conf)
        script = " && ".join(cmds)
        try:
            r = subprocess.run(["pkexec", "sh", "-c", script], capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            QMessageBox.information(self, "Corrigir rp_filter", "pkexec não encontrado. Rode no terminal:\n\nsudo sh -c "
                                    + shlex.quote(script))
            return
        except subprocess.TimeoutExpired:
            return
        if r.returncode == 0:
            QMessageBox.information(self, "Corrigir rp_filter", "rp_filter desligado em: %s%s" % (
                ", ".join(targets), "\n(gravado em /etc/sysctl.d/60-pidgeon-multicast.conf)"
                if self.persist.isChecked() else "\n(vale até reiniciar)"))
        elif r.returncode not in (126, 127):
            QMessageBox.warning(self, "Corrigir rp_filter", r.stderr.strip() or "Falhou (código %d)" % r.returncode)
        self._table()
        self.run()

    def copy(self):
        lines = ["Diagnóstico de rede — PIDgeon"]
        for r in range(self.table.rowCount()):
            lines.append("  " + " | ".join(self.table.item(r, c).text() for c in range(self.table.columnCount())))
        for i in range(self.results.count()):
            lines.append(self.results.item(i).text())
        QGuiApplication.clipboard().setText("\n".join(lines))
