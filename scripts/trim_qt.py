"""Remove do PySide6 empacotado tudo o que o PIDgeon não usa (QML, Quick, Designer…).

Mantém os bindings QtCore/QtGui/QtWidgets, os plugins de plataforma (X11, Wayland, offscreen…),
temas, formatos de imagem e o fechamento transitivo das bibliotecas Qt de que eles dependem.

Uso:  python3 scripts/trim_qt.py <diretório que contém PySide6/>
"""

import os
import re
import shutil
import subprocess
import sys

KEEP_BINDINGS = {"QtCore", "QtGui", "QtWidgets"}
KEEP_PLUGINS = {"platforms", "platformthemes", "platforminputcontexts", "xcbglintegrations", "imageformats",
                "iconengines", "egldeviceintegrations", "generic", "tls", "wayland-decoration-client",
                "wayland-graphics-integration-client", "wayland-shell-integration", "styles"}
DROP_DIRS = ("Qt/qml", "Qt/metatypes", "include", "typesystems", "glue", "doc", "examples")


def needed(path):
    try:
        out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return set(re.findall(r"\(NEEDED\).*\[(.+?)\]", out))


def main(base):
    ps = os.path.join(base, "PySide6")
    qlib = os.path.join(ps, "Qt", "lib")
    before = du(ps)
    for d in DROP_DIRS:
        shutil.rmtree(os.path.join(ps, d), ignore_errors=True)
    # bindings Python que não usamos
    for f in os.listdir(ps):
        m = re.match(r"(Qt\w+)\.(abi3\.so|pyi)$", f)
        if m and m.group(1) not in KEEP_BINDINGS:
            os.remove(os.path.join(ps, f))
    # plugins
    pdir = os.path.join(ps, "Qt", "plugins")
    for d in os.listdir(pdir):
        if d not in KEEP_PLUGINS:
            shutil.rmtree(os.path.join(pdir, d), ignore_errors=True)
    for f in ("libqvnc.so", "libqvkkhrdisplay.so", "libqlinuxfb.so"):
        p = os.path.join(pdir, "platforms", f)
        if os.path.exists(p):
            os.remove(p)
    # traduções: só português
    tdir = os.path.join(ps, "Qt", "translations")
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            if not re.match(r"qt(base)?_pt(_BR)?\.qm$", f):
                p = os.path.join(tdir, f)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    # fechamento das dependências
    roots = [os.path.join(ps, f) for f in os.listdir(ps) if f.endswith(".so") or ".so." in f]
    for dp, _, files in os.walk(pdir):
        roots += [os.path.join(dp, f) for f in files if f.endswith(".so")]
    libs = set(os.listdir(qlib))
    keep = set()
    todo = list(roots)
    while todo:
        for n in needed(todo.pop()):
            if n in libs and n not in keep:
                keep.add(n)
                todo.append(os.path.join(qlib, n))
    # pyside6 / shiboken ficam ao lado dos bindings; mantém todos os .so que não são do Qt/lib
    removed = 0
    for f in libs:
        if f not in keep and (f.startswith("libQt6") or f.startswith("libicu")):
            p = os.path.join(qlib, f)
            if os.path.isfile(p) or os.path.islink(p):
                os.remove(p)
                removed += 1
    # plugins que dependem de módulos ausentes no PySide6-Essentials (Pdf, VirtualKeyboard, EglFS-KMS)
    present = set(os.listdir(qlib))
    for dp, _, files in os.walk(pdir):
        for f in files:
            p = os.path.join(dp, f)
            if f.endswith(".so") and any(n.startswith("libQt6") and n not in present for n in needed(p)):
                os.remove(p)
                removed += 1
    after = du(ps)
    print("PySide6: %.0f MB -> %.0f MB (%d bibliotecas/plugins Qt removidos, %d mantidos)" % (
        before / 1e6, after / 1e6, removed, len(keep)))


def du(path):
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


if __name__ == "__main__":
    main(sys.argv[1])
