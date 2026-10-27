#!/usr/bin/env bash
# Remove o PIDgeon instalado pelo install.sh. Use --tudo para apagar também as configurações.
set -eu
PREFIX="${PIDGEON_PREFIX:-$HOME/.local/share/pidgeon}"
BIN="${PIDGEON_BIN:-$HOME/.local/bin}"
rm -rf "$PREFIX"
rm -f "$BIN/pidgeon" "$BIN/pidgeon-cli" "$BIN/pidgeon-gerar-teste" "$BIN/pidgeon-enviar"
rm -f "$HOME/.local/share/applications/pidgeon.desktop"
for d in 48x48 128x128 256x256; do rm -f "$HOME/.local/share/icons/hicolor/$d/apps/pidgeon.png"; done
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/pidgeon.svg"
if [ "${1:-}" = "--tudo" ]; then rm -rf "$HOME/.config/PIDgeon"; fi
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "PIDgeon removido."
