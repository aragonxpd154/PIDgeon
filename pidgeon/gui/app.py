"""Ponto de entrada da interface gráfica."""

import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pidgeon", description="PIDgeon — analisador de Transport Stream / ISDB-Tb")
    ap.add_argument("fonte", nargs="?", help="arquivo .ts/.bts ou udp://grupo:porta, rtp://…")
    ap.add_argument("--version", action="store_true")
    a, rest = ap.parse_known_args(argv)
    from .. import __version__
    if a.version:
        print("PIDgeon", __version__)
        return 0
    # Wayland/X11: deixa o Qt escolher; se o plugin xcb falhar, tenta wayland
    if "QT_QPA_PLATFORM" not in os.environ and os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "wayland"
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([sys.argv[0]] + rest)
    app.setApplicationName("PIDgeon")
    app.setOrganizationName("PIDgeon")
    app.setDesktopFileName("pidgeon")
    from .main_window import MainWindow
    w = MainWindow(a.fonte)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
