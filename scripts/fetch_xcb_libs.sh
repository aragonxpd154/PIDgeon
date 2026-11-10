#!/usr/bin/env bash
# Baixa (no momento do BUILD, não na instalação) as bibliotecas xcb-util que o Qt 6 usa e que
# muitas instalações novas do Ubuntu/Zorin/Mint não trazem. Pacotes do Ubuntu 22.04 (jammy),
# glibc >= 2.14, conferidos por SHA-256.
#   uso: scripts/fetch_xcb_libs.sh <diretório de destino>
set -euo pipefail
DEST="$1"
mkdir -p "$DEST"
B=http://archive.ubuntu.com/ubuntu/pool
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
while read -r sum path; do
  [ -z "$sum" ] && continue
  f="$TMP/$(basename "$path")"
  curl -sSfo "$f" "$B/$path"
  echo "$sum  $f" | sha256sum -c --quiet -
  dpkg-deb -x "$f" "$TMP/x"
done <<'LIST'
c9b5d1ad4af57397b1bd77e0a92750e34419def134c0282a0836ae9efc07cf64 universe/x/xcb-util-cursor/libxcb-cursor0_0.1.1-4ubuntu1_amd64.deb
4dfe14a0ce681b823b740dabb3de84b3ce0958c9a404af61823b11f64047b0ef main/x/xcb-util-wm/libxcb-icccm4_0.4.1-1.1build2_amd64.deb
b52babc3b4f2aa5241cac3a7888ce3b37d25a7f18867cae180e8cd11ec945cb9 main/x/xcb-util-image/libxcb-image0_0.4.0-2_amd64.deb
1d62f96a793cc1aa0df860de3f7d83edea2fa461d97e2b3d4d7f7e7a10e55f42 main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build3_amd64.deb
f87659a2369430ed2acb1cdae54a06fa9d60727a6545594e213173ddb4377727 main/x/xcb-util-renderutil/libxcb-render-util0_0.3.9-1build3_amd64.deb
237d51a378b93c1669ff193084ebeb6422fe1e2ea96c2e9a0099eac40992a345 main/x/xcb-util/libxcb-util1_0.4.0-1build2_amd64.deb
e29c40fab6e0f055218aee4c7e60cd61ea82283229d7ac7ac02e226e92a5b0d5 main/libx/libxkbcommon/libxkbcommon-x11-0_1.4.0-1_amd64.deb
LIST
cp -a "$TMP"/x/usr/lib/x86_64-linux-gnu/*.so.* "$DEST/"
ls "$DEST"
