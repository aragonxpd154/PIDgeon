"""Temas claro (estilo StreamXpert/Office) e escuro."""

from PySide6.QtGui import QPalette, QColor

LIGHT = {
    "window": "#eef2f8", "base": "#ffffff", "alt": "#f5f8fc", "text": "#1d2a3a",
    "muted": "#5d6b7d", "accent": "#2b6cb0", "accent2": "#e8f0fb", "border": "#c5d0de",
    "ribbon": "#f7f9fc", "ribbon_tab": "#dde6f2", "graph_bg": "#0b1220", "graph_grid": "#25324a",
    "ok": "#2f9e44", "warn": "#e8a10c", "err": "#d9363e", "select": "#cfe0f7",
}
DARK = {
    "window": "#1b1f27", "base": "#12161d", "alt": "#181d25", "text": "#dfe6ef",
    "muted": "#8b97a8", "accent": "#5aa2ff", "accent2": "#1f2a3a", "border": "#2c3442",
    "ribbon": "#20252e", "ribbon_tab": "#262c37", "graph_bg": "#0b1018", "graph_grid": "#223047",
    "ok": "#40c057", "warn": "#f2b31b", "err": "#ff5a5f", "select": "#2b3d57",
}

CURRENT = dict(LIGHT)


def apply(app, dark=False):
    c = DARK if dark else LIGHT
    CURRENT.clear()
    CURRENT.update(c)
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.Window, QColor(c["window"]))
    p.setColor(QPalette.WindowText, QColor(c["text"]))
    p.setColor(QPalette.Base, QColor(c["base"]))
    p.setColor(QPalette.AlternateBase, QColor(c["alt"]))
    p.setColor(QPalette.Text, QColor(c["text"]))
    p.setColor(QPalette.Button, QColor(c["ribbon"]))
    p.setColor(QPalette.ButtonText, QColor(c["text"]))
    p.setColor(QPalette.Highlight, QColor(c["accent"]))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, QColor(c["base"]))
    p.setColor(QPalette.ToolTipText, QColor(c["text"]))
    p.setColor(QPalette.PlaceholderText, QColor(c["muted"]))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(c["muted"]))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(c["muted"]))
    app.setPalette(p)
    app.setStyleSheet(stylesheet(c))


def stylesheet(c):
    return """
QMainWindow, QDialog {{ background: {window}; }}
QTabWidget#ribbon::pane {{ border: 1px solid {border}; border-top: none; background: {ribbon}; }}
QTabWidget#ribbon > QTabBar::tab {{ padding: 5px 16px; margin-right: 1px; border: 1px solid transparent;
    border-bottom: none; color: {text}; background: transparent; }}
QTabWidget#ribbon > QTabBar::tab:selected {{ background: {ribbon}; border-color: {border};
    border-top-left-radius: 3px; border-top-right-radius: 3px; color: {accent}; font-weight: 600; }}
QTabWidget#ribbon > QTabBar::tab:hover:!selected {{ background: {ribbon_tab}; }}
QFrame#ribbonGroup {{ border-right: 1px solid {border}; }}
QLabel#groupTitle {{ color: {muted}; font-size: 11px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 3px; padding: 3px; }}
QToolButton:hover {{ background: {accent2}; border-color: {border}; }}
QToolButton:checked {{ background: {select}; border-color: {accent}; }}
QTreeWidget, QTableWidget, QPlainTextEdit, QListWidget {{ border: 1px solid {border}; background: {base};
    selection-background-color: {select}; selection-color: {text}; }}
QHeaderView::section {{ background: {ribbon_tab}; border: none; border-right: 1px solid {border};
    padding: 3px 6px; color: {text}; }}
QTabWidget#bottomTabs::pane, QTabWidget#centerTabs::pane, QTabWidget#leftTabs::pane {{ border: 1px solid {border}; }}
QTabWidget#bottomTabs > QTabBar::tab, QTabWidget#centerTabs > QTabBar::tab, QTabWidget#leftTabs > QTabBar::tab {{
    padding: 3px 12px; border: 1px solid {border}; border-top: none; background: {ribbon_tab}; color: {text}; }}
QTabBar::tab:selected {{ background: {base}; font-weight: 600; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {ribbon_tab}; padding: 3px 6px; }}
QStatusBar {{ background: {ribbon_tab}; border-top: 1px solid {border}; }}
QStatusBar QLabel {{ padding: 0 8px; border-left: 1px solid {border}; }}
QLabel#infoBar {{ background: {ribbon_tab}; padding: 3px 8px; border: 1px solid {border}; }}
QProgressBar {{ border: 1px solid {border}; border-radius: 2px; text-align: center; background: {base}; height: 14px; }}
QProgressBar::chunk {{ background: {accent}; }}
QGroupBox {{ border: 1px solid {border}; border-radius: 4px; margin-top: 10px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {accent}; font-weight: 600; }}
""".format(**c)
