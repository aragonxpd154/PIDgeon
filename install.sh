#!/usr/bin/env bash
# PIDgeon — instalador rápido para Linux (Zorin OS, Ubuntu, Debian, Mint, Pop!_OS…)
#
# Instala no usuário atual, sem sudo:
#   programa   ~/.local/share/pidgeon
#   comandos   ~/.local/bin/pidgeon, pidgeon-cli, pidgeon-gerar-teste, pidgeon-enviar
#   atalho     menu de aplicativos ("PIDgeon")
#
# Se a pasta vendor/ existir (pacote de release), instala 100% offline.
# Sem vendor/ (clone do git), baixa as dependências com o pip.
#
# Opções:  --python /caminho/python3   --prefix DIR   --bin DIR   --online   --sem-atalho
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PIDGEON_PREFIX:-$HOME/.local/share/pidgeon}"
BIN="${PIDGEON_BIN:-$HOME/.local/bin}"
PY="${PYTHON:-python3}"
[ -n "${HOME:-}" ] || { echo "HOME não definido"; exit 1; }
ONLINE=0
SHORTCUT=1

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PY="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --bin) BIN="$2"; shift 2 ;;
    --online) ONLINE=1; shift ;;
    --sem-atalho) SHORTCUT=0; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Opção desconhecida: $1"; exit 2 ;;
  esac
done

PREFIX="$(realpath -m "$PREFIX")"
BIN="$(realpath -m "$BIN")"

c_ok="\033[1;32m"; c_info="\033[1;34m"; c_warn="\033[1;33m"; c_err="\033[1;31m"; c_0="\033[0m"
say()  { printf "${c_info}==>${c_0} %s\n" "$*"; }
ok()   { printf "${c_ok} ✔${c_0} %s\n" "$*"; }
warn() { printf "${c_warn} !${c_0} %s\n" "$*"; }
die()  { printf "${c_err} ✘ %s${c_0}\n" "$*"; exit 1; }

echo
printf "${c_info}  PIDgeon — analisador de Transport Stream / ISDB-Tb${c_0}\n"
echo "  -----------------------------------------------------"

# ------------------------------------------------------------------ verificações
[ "$(uname -m)" = "x86_64" ] || die "Arquitetura $(uname -m) não suportada pelo pacote offline (use x86_64 ou --online)."
command -v "$PY" >/dev/null 2>&1 || die "python3 não encontrado. Instale com: sudo apt install python3"
PY="$(command -v "$PY")"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PYTAG="$("$PY" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python $PYVER é antigo — o PIDgeon precisa do Python 3.10 ou mais novo."
GLIBC="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0)"
ok "Python $PYVER ($PY), glibc $GLIBC"
if [ -r /etc/os-release ]; then . /etc/os-release; ok "Sistema: ${PRETTY_NAME:-Linux}"; fi

VENDOR="$HERE/vendor"
if [ -d "$VENDOR/wheels" ] && [ "$ONLINE" = 0 ]; then
  MODE=offline
  if [ "$PYTAG" = "310" ]; then AVDIR="$VENDOR/wheels/cp310"; else AVDIR="$VENDOR/wheels/cp311"; fi
  PIPWHL="$(ls "$VENDOR"/wheels/common/pip-*.whl 2>/dev/null | head -1)"
  [ -n "$PIPWHL" ] || die "pip não encontrado em vendor/wheels/common"
  say "Instalação OFFLINE a partir do pacote (vendor/)"
else
  MODE=online
  say "Instalação ONLINE (dependências baixadas do PyPI)"
fi

# ------------------------------------------------------------------ dependências Python
say "Instalando dependências (PySide6-Essentials + PyAV/FFmpeg) em $PREFIX/lib …"
rm -rf "$PREFIX/lib.new"
mkdir -p "$PREFIX"
PIPARGS=(install --target "$PREFIX/lib.new" --disable-pip-version-check --no-warn-script-location --quiet)
[ "$(id -u)" = 0 ] && PIPARGS+=(--root-user-action=ignore)
if [ "$MODE" = offline ]; then
  "$PY" "$PIPWHL/pip" "${PIPARGS[@]}" --no-index \
      --find-links "$VENDOR/wheels/common" --find-links "$AVDIR" PySide6-Essentials av \
    || die "Falha ao instalar as dependências offline (Python $PYVER)."
else
  if "$PY" -m pip --version >/dev/null 2>&1; then
    "$PY" -m pip "${PIPARGS[@]}" -r "$HERE/requirements.txt" || die "Falha no pip."
  else
    die "pip não disponível. Instale com: sudo apt install python3-pip   (ou use o pacote de release com vendor/)"
  fi
fi
rm -rf "$PREFIX/lib"
mv "$PREFIX/lib.new" "$PREFIX/lib"
ok "Dependências instaladas"

# ------------------------------------------------------------------ aplicativo
say "Copiando o PIDgeon …"
rm -rf "$PREFIX/app"
mkdir -p "$PREFIX/app"
cp -r "$HERE/pidgeon" "$PREFIX/app/"
find "$PREFIX/app" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
"$PY" -m compileall -q "$PREFIX/app" >/dev/null 2>&1 || true
[ -d "$HERE/amostras" ] && { rm -rf "$PREFIX/amostras"; cp -r "$HERE/amostras" "$PREFIX/"; }
ok "Aplicativo em $PREFIX/app"

# ------------------------------------------------------------------ bibliotecas do Qt (xcb)
rm -rf "$PREFIX/syslib"
missing=()
for so in libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-image.so.0 libxcb-keysyms.so.1 \
          libxcb-render-util.so.0 libxcb-util.so.1 libxkbcommon-x11.so.0; do
  if ! /sbin/ldconfig -p 2>/dev/null | grep -q "$so"; then missing+=("$so"); fi
done
if [ ${#missing[@]} -gt 0 ]; then
  if [ -d "$VENDOR/xcb-libs" ]; then
    mkdir -p "$PREFIX/syslib"
    for so in "${missing[@]}"; do
      cp -a "$VENDOR/xcb-libs/$so"* "$PREFIX/syslib/" 2>/dev/null || warn "$so não está no pacote"
    done
    ok "Bibliotecas do Qt incorporadas: ${missing[*]}"
  else
    warn "Faltam bibliotecas do Qt: ${missing[*]}"
    warn "Instale com: sudo apt install libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxkbcommon-x11-0"
  fi
else
  ok "Bibliotecas do Qt (xcb) já presentes no sistema"
fi

# ------------------------------------------------------------------ comandos
mkdir -p "$BIN"
mk_launcher() {
  local name="$1" module="$2"
  cat > "$BIN/$name" <<EOF
#!/bin/sh
# Gerado pelo instalador do PIDgeon
P="$PREFIX"
export PYTHONPATH="\$P/app:\$P/lib\${PYTHONPATH:+:\$PYTHONPATH}"
[ -d "\$P/syslib" ] && export LD_LIBRARY_PATH="\$P/syslib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$PY" -m $module "\$@"
EOF
  chmod +x "$BIN/$name"
}
mk_launcher pidgeon "pidgeon"
mk_launcher pidgeon-cli "pidgeon cli"
mk_launcher pidgeon-gerar-teste "pidgeon.tools.testgen"
mk_launcher pidgeon-enviar "pidgeon.tools.sender"
ok "Comandos em $BIN: pidgeon, pidgeon-cli, pidgeon-gerar-teste, pidgeon-enviar"

# ------------------------------------------------------------------ atalho no menu
if [ "$SHORTCUT" = 1 ]; then
  ICON_DIR="$HOME/.local/share/icons/hicolor"
  for sz in 48 128 256; do
    if [ -f "$HERE/packaging/pidgeon-$sz.png" ]; then
      mkdir -p "$ICON_DIR/${sz}x${sz}/apps"
      cp "$HERE/packaging/pidgeon-$sz.png" "$ICON_DIR/${sz}x${sz}/apps/pidgeon.png"
    fi
  done
  if [ -f "$HERE/packaging/pidgeon.svg" ]; then
    mkdir -p "$ICON_DIR/scalable/apps"
    cp "$HERE/packaging/pidgeon.svg" "$ICON_DIR/scalable/apps/pidgeon.svg"
  fi
  mkdir -p "$HOME/.local/share/applications"
  sed "s#@BIN@#$BIN#g" "$HERE/packaging/pidgeon.desktop" > "$HOME/.local/share/applications/pidgeon.desktop"
  command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "$ICON_DIR" >/dev/null 2>&1 || true
  ok "Atalho criado no menu de aplicativos (PIDgeon)"
fi

# ------------------------------------------------------------------ autoteste
say "Verificando a instalação …"
export PYTHONPATH="$PREFIX/app:$PREFIX/lib"
[ -d "$PREFIX/syslib" ] && export LD_LIBRARY_PATH="$PREFIX/syslib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$PY" - <<'EOF' || die "Autoteste falhou."
import av, PySide6
from PySide6 import QtWidgets  # noqa
import pidgeon
from pidgeon.core.analyzer import Analyzer
print("   PIDgeon %s | PySide6 %s | PyAV %s | FFmpeg libavcodec %s" % (
    pidgeon.__version__, PySide6.__version__, av.__version__,
    ".".join(map(str, av.library_versions["libavcodec"]))))
EOF
QXCB="$(ls -d "$PREFIX"/lib/PySide6/Qt/plugins/platforms/libqxcb.so 2>/dev/null || true)"
if [ -n "$QXCB" ] && ldd "$QXCB" | grep -q "not found"; then
  warn "Ainda faltam bibliotecas para o Qt (X11):"
  ldd "$QXCB" | grep "not found" | sed 's/^/      /'
fi
ok "Instalação concluída ($MODE)"

echo
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN não está no PATH desta sessão. Abra pelo menu, saia e entre de novo na sessão, ou rode: $BIN/pidgeon" ;;
esac
cat <<EOF

  Como usar:
    pidgeon                                   abre a interface gráfica
    pidgeon arquivo.bts                       abre um arquivo direto
    pidgeon udp://239.1.1.1:1234              multicast (RTP detectado sozinho)
    pidgeon "rtp://10.0.0.5@232.1.1.1:5000?iface=enp3s0"   SSM numa interface
    pidgeon-cli arquivo.ts -j relatorio.json  análise sem interface
    pidgeon-gerar-teste ~/teste.bts           gera um BTS ISDB-Tb de teste
EOF
[ -d "$PREFIX/amostras" ] && echo "    Amostra pronta:  pidgeon $PREFIX/amostras/teste_isdbt.bts"
echo
