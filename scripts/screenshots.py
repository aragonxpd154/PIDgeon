"""Gera capturas da interface (modo offscreen) para a documentação."""
import os, sys, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QEventLoop
src, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)
app = QApplication(sys.argv[:1])
from pidgeon.gui.main_window import MainWindow
w = MainWindow()
w.resize(1600, 960)
w.show()
def wait(sec):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents(QEventLoop.AllEvents, 50)
        time.sleep(0.02)
w.loop_chk.setChecked(True)
w.open_input(src)
wait(3.0)
w.start_player()
wait(5.0)
for it in list(w.pid_items.values())[:0]:
    pass
w.grab().save(os.path.join(outdir, "tv.png"))
for name in ["PID", "TS", "Grid", "ISDB-T / BTS", "PCR", "TR 101 290", "EPG"]:
    if name == "TR 101 290":
        w.reset_counters()
    for i in range(w.center.count()):
        if w.center.tabText(i) == name:
            w.center.setCurrentIndex(i)
    if name == "PID":
        w.sel_pid = 0x1011
    wait(1.2)
    w.grab().save(os.path.join(outdir, name.split()[0].replace("/", "").lower() + ".png"))
for i in range(w.center.count()):
    if w.center.tabText(i) == "Mosaico":
        w.center.setCurrentIndex(i)
w.mosaic_chk.setChecked(True); wait(7.0)
w.grab().save(os.path.join(outdir, "mosaico.png"))
w.left.setCurrentIndex(2); wait(1.0)
w.grab().save(os.path.join(outdir, "tabelas.png"))
w._toggle_theme(True); w.center.setCurrentIndex(0); w.left.setCurrentIndex(0); wait(1.5)
w.grab().save(os.path.join(outdir, "tv_escuro.png"))
print("frames", w.player[0].frames if w.player else None, "info", w.player_info)
w.close()
