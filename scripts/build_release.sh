#!/usr/bin/env bash
# Monta o pacote de release offline: dist/PIDgeon-<versão>-linux-x86_64.zip
#  - código do PIDgeon + instalador
#  - wheels do PySide6-Essentials e do PyAV (com FFmpeg) para Python 3.10 e 3.11+
#  - bibliotecas xcb do Qt (extraídas dos .deb do Ubuntu 22.04) para quem não as tem
#  - amostra ISDB-Tb (BTS 204) gerada pelo próprio PIDgeon
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VER="$(python3 -c 'import pidgeon; print(pidgeon.__version__)')"
NAME="PIDgeon-$VER-linux-x86_64"
OUT="$ROOT/dist/$NAME"
PYSIDE_VER="${PYSIDE_VER:-6.11.3}"
rm -rf "$OUT" "$ROOT/dist/$NAME.zip"
mkdir -p "$OUT/vendor/wheels/common" "$OUT/vendor/wheels/cp310" "$OUT/vendor/wheels/cp311" "$OUT/vendor/xcb-libs"

echo "==> Wheels"
PLAT=(--platform manylinux_2_34_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64)
pip download -q --only-binary=:all: "${PLAT[@]}" --python-version 3.10 --implementation cp \
    -d "$OUT/vendor/wheels/common" "PySide6-Essentials==$PYSIDE_VER" pip
pip download -q --only-binary=:all: --no-deps "${PLAT[@]}" --python-version 3.10 --implementation cp \
    -d "$OUT/vendor/wheels/cp310" av
pip download -q --only-binary=:all: --no-deps "${PLAT[@]}" --python-version 3.11 --implementation cp \
    -d "$OUT/vendor/wheels/cp311" av

echo "==> Bibliotecas xcb do Qt (Ubuntu 22.04 / jammy, glibc >= 2.14)"
TMP="$(mktemp -d)"
B=http://archive.ubuntu.com/ubuntu/pool
for p in universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb \
         main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1build2_amd64.deb \
         main/x/xcb-util-image/libxcb-image0_0.4.0-2_amd64.deb \
         main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build3_amd64.deb \
         main/x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1build3_amd64.deb \
         main/x/xcb-util/libxcb-util1_0.4.0-1build2_amd64.deb \
         main/libx/libxkbcommon/libxkbcommon-x11-0_1.4.0-1_amd64.deb; do
  curl -sfo "$TMP/$(basename "$p")" "$B/$p"
  dpkg-deb -x "$TMP/$(basename "$p")" "$TMP/x"
done
cp -a "$TMP"/x/usr/lib/x86_64-linux-gnu/*.so.* "$OUT/vendor/xcb-libs/"
rm -rf "$TMP"

echo "==> Código"
cp -r pidgeon install.sh uninstall.sh README.md LICENSE CHANGELOG.md requirements.txt packaging "$OUT/"
find "$OUT/pidgeon" -name "__pycache__" -type d -prune -exec rm -rf {} +
chmod +x "$OUT/install.sh" "$OUT/uninstall.sh"

echo "==> Amostra ISDB-Tb"
mkdir -p "$OUT/amostras"
python3 -m pidgeon.tools.testgen "$OUT/amostras/teste_isdbt.bts" --segundos 12

cat > "$OUT/LEIA-ME.txt" <<EOF
PIDgeon $VER — instalação rápida (offline)

1. Extraia este .zip (botão direito > Extrair aqui)
2. Abra um terminal dentro da pasta extraída e rode:

     ./install.sh

3. Abra pelo menu de aplicativos (PIDgeon) ou digite: pidgeon

Para remover: ./uninstall.sh
Requisitos: Linux x86_64 com Python 3.10+ e glibc 2.34+ (Zorin OS 17/18, Ubuntu 22.04/24.04, Debian 12, Mint 21/22).
EOF

echo "==> Compactando"
(cd "$ROOT/dist" && zip -qr -9 "$NAME.zip" "$NAME")
ls -la "$ROOT/dist/$NAME.zip"
sha256sum "$ROOT/dist/$NAME.zip" | tee "$ROOT/dist/$NAME.zip.sha256"
