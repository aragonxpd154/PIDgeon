#!/usr/bin/env bash
# Monta o pacote Debian/Ubuntu: dist/pidgeon_<versão>_amd64.deb
#
#   /opt/pidgeon/app          código do PIDgeon
#   /opt/pidgeon/lib/common   PySide6-Essentials + shiboken6 (abi3, Python 3.10+)
#   /opt/pidgeon/lib/cp310    PyAV/FFmpeg para Python 3.10 (Zorin OS 17 / Ubuntu 22.04)
#   /opt/pidgeon/lib/abi3     PyAV/FFmpeg para Python 3.11+ (Debian 12/13, Zorin OS 18, Ubuntu 24.04)
#   /usr/bin/pidgeon, pidgeon-cli, pidgeon-gerar, pidgeon-enviar, pidgeon-gerar-teste
#
# As bibliotecas do X11/Wayland que o Qt usa vêm do apt (campo Depends), então o pacote instala
# com:  sudo apt install ./pidgeon_<versão>_amd64.deb
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VER="$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"", open("pidgeon/__init__.py").read()).group(1))')"
PYSIDE_VER="${PYSIDE_VER:-6.11.3}"
PKG=pidgeon
ARCH=amd64
STAGE="$ROOT/build/deb/${PKG}_${VER}_${ARCH}"
OUT="$ROOT/dist/${PKG}_${VER}_${ARCH}.deb"
PIP="${PIP:-python3 -m pip}"
rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/pidgeon/app" "$STAGE/opt/pidgeon/lib" "$STAGE/opt/pidgeon/bin" \
         "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/$PKG" \
         "$STAGE/usr/share/metainfo" "$ROOT/dist"

PLAT=(--platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64
      --platform manylinux2014_x86_64 --implementation cp --only-binary=:all:)
inst() {  # inst <destino> <python> <pacotes…>
  local dest="$1" py="$2"; shift 2
  $PIP install -q --disable-pip-version-check --no-compile --target "$dest" "${PLAT[@]}" \
      --python-version "$py" "$@"
}

echo "==> Dependências Python (PySide6 $PYSIDE_VER + PyAV)"
inst "$STAGE/opt/pidgeon/lib/common" 3.10 "PySide6-Essentials==$PYSIDE_VER"
inst "$STAGE/opt/pidgeon/lib/cp310" 3.10 --no-deps av
# a wheel abi3 do PyAV compilada para cp311 roda em qualquer Python >= 3.11
inst "$STAGE/opt/pidgeon/lib/abi3" 3.11 --no-deps "av<19"

find "$STAGE/opt/pidgeon/lib" -maxdepth 2 -name "bin" -type d -prune -exec rm -rf {} +
find "$STAGE/opt/pidgeon/lib" -name "__pycache__" -type d -prune -exec rm -rf {} +
# ferramentas de desenvolvimento do Qt e módulos que o PIDgeon não usa (QML, Quick, Designer…)
rm -rf "$STAGE"/opt/pidgeon/lib/common/PySide6/{Assistant,Designer,Linguist,assistant,designer,linguist,lupdate,lrelease,qmllint,qmlformat,qmlls,qmlcachegen,qmlimportscanner,qmltyperegistrar,uic,rcc,balsam,balsamui,qsb,svgtoqml,android_deploy,android_deployqt}* 2>/dev/null || true
python3 "$ROOT/scripts/trim_qt.py" "$STAGE/opt/pidgeon/lib/common"

echo "==> Código"
cp -r pidgeon "$STAGE/opt/pidgeon/app/"
find "$STAGE/opt/pidgeon/app" -name "__pycache__" -type d -prune -exec rm -rf {} +

cat > "$STAGE/opt/pidgeon/bin/pidgeon-run" <<'EOF'
#!/bin/sh
# Inicia um módulo do PIDgeon com as bibliotecas do pacote (/opt/pidgeon).
P=/opt/pidgeon
PY="${PIDGEON_PYTHON:-/usr/bin/python3}"
V="$("$PY" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
if [ "$V" = 310 ]; then AV="$P/lib/cp310"; else AV="$P/lib/abi3"; fi
export PYTHONPATH="$P/app:$P/lib/common:$AV${PYTHONPATH:+:$PYTHONPATH}"
M="$1"; shift
exec "$PY" -m $M "$@"
EOF
chmod 755 "$STAGE/opt/pidgeon/bin/pidgeon-run"
for spec in "pidgeon:pidgeon" "pidgeon-cli:pidgeon.cli" "pidgeon-gerar:pidgeon.tools.gerar" \
            "pidgeon-enviar:pidgeon.tools.sender" "pidgeon-gerar-teste:pidgeon.tools.testgen"; do
  name="${spec%%:*}"; mod="${spec#*:}"
  printf '#!/bin/sh\nexec /opt/pidgeon/bin/pidgeon-run %s "$@"\n' "$mod" > "$STAGE/usr/bin/$name"
  chmod 755 "$STAGE/usr/bin/$name"
done

echo "==> Atalho, ícones e documentação"
sed "s#@BIN@#/usr/bin#g" packaging/pidgeon.desktop > "$STAGE/usr/share/applications/pidgeon.desktop"
for sz in 48 128 256; do
  install -Dm644 "packaging/pidgeon-$sz.png" "$STAGE/usr/share/icons/hicolor/${sz}x${sz}/apps/pidgeon.png"
done
install -Dm644 packaging/pidgeon.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/pidgeon.svg"
install -Dm644 packaging/pidgeon.metainfo.xml "$STAGE/usr/share/metainfo/io.github.aragonxpd154.pidgeon.metainfo.xml"
install -m644 README.md "$STAGE/usr/share/doc/$PKG/README.md"
gzip -9n -c CHANGELOG.md > "$STAGE/usr/share/doc/$PKG/changelog.gz"
cat > "$STAGE/usr/share/doc/$PKG/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: PIDgeon
Upstream-Contact: Marcos Santos <https://github.com/aragonxpd154>
Source: https://github.com/aragonxpd154/PIDgeon

Files: *
Copyright: 2026 Marcos Santos
License: GPL-3.0-or-later
 On Debian systems, the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.

Files: opt/pidgeon/lib/common/PySide6/* opt/pidgeon/lib/common/shiboken6/*
Copyright: The Qt Company Ltd.
License: LGPL-3.0-only

Files: opt/pidgeon/lib/*/av/* opt/pidgeon/lib/*/av.libs/*
Copyright: PyAV contributors; FFmpeg developers
License: BSD-3-Clause and LGPL-2.1-or-later (FFmpeg)
EOF
cp LICENSE "$STAGE/opt/pidgeon/LICENSE"

echo "==> Controle"
SIZE_KB="$(du -sk "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VER
Section: video
Priority: optional
Architecture: $ARCH
Maintainer: Marcos Santos <aragonxpd154@users.noreply.github.com>
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.10), libegl1, libgl1, libx11-6, libx11-xcb1, libdbus-1-3, libfontconfig1, libfreetype6,
 libglib2.0-0t64 | libglib2.0-0, libwayland-client0, libwayland-cursor0, libxcb1, libxcb-cursor0,
 libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render0, libxcb-render-util0,
 libxcb-shape0, libxcb-shm0, libxcb-sync1, libxcb-util1, libxcb-xfixes0, libxcb-xkb1, libxkbcommon0,
 libxkbcommon-x11-0, zlib1g, libzstd1
Recommends: libpulse0
Suggests: ffmpeg
Homepage: https://github.com/aragonxpd154/PIDgeon
Description: Analisador e gerador de MPEG Transport Stream / ISDB-Tb (BTS)
 PIDgeon analisa TS 188, BTS 204 e M2TS em arquivo, UDP e RTP (multicast e
 unicast, com recuperação FEC SMPTE 2022-1): PIDs, PSI/SI, TR 101 290, camadas
 ISDB-T, IIP, PCR, vídeo ao vivo e gravação.
 .
 Gera BTS 204, BTS 188, TS 188 e TS 204 (Reed-Solomon) a partir de uma
 gravação, remultiplexando TS para BTS ISDB-Tb quando necessário, e grava em
 arquivo ou transmite por UDP puro, RTP ou RTP + FEC para endereço unicast ou
 grupo multicast.
EOF
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  /usr/bin/python3 -m compileall -q /opt/pidgeon/app >/dev/null 2>&1 || true
fi
exit 0
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /opt/pidgeon -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE/opt/pidgeon/app" "$STAGE/usr/share" -type f -exec chmod 644 {} +

echo "==> Empacotando"
dpkg-deb --root-owner-group -Zxz -z6 --build "$STAGE" "$OUT" >/dev/null
ls -la "$OUT"
(cd "$ROOT/dist" && sha256sum "$(basename "$OUT")" | tee "$(basename "$OUT").sha256")
