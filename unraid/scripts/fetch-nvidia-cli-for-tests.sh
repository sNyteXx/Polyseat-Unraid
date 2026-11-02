#!/bin/sh
# Test-only: NVIDIA's libnvidia-container 1.20.1 (the version on Tower), pinned by sha256 from the official
# nvidia.github.io repository index, extracted with dpkg-deb -x into OUT/x. Never installed, never shipped, never
# used on Unraid. Feeds case D of local-hook-rehearsal.sh (the real CLI without any NVIDIA driver).
set -eu
out=${1:?usage: fetch-nvidia-cli-for-tests.sh OUT_DIR}
[ ! -e /etc/unraid-version ] || { echo 'never on Unraid' >&2; exit 2; }
base=https://nvidia.github.io/libnvidia-container/stable/deb/amd64
mkdir -p "$out/x"
while read -r sha file; do
  [ -f "$out/$file" ] || curl -fsSL --max-time 120 -o "$out/$file" "$base/$file"
  printf '%s  %s\n' "$sha" "$out/$file" | sha256sum --check --strict --quiet
  dpkg-deb -x "$out/$file" "$out/x"
done <<'PINS'
9d803cd9c898cb2fa927529d5ac4fc09f3d65c8cfbde8985de1b9398e53a4656 libnvidia-container1_1.20.1-1_amd64.deb
8bd131b8977a3db667982024850ae7b50cfa02d6b2be06a718064ec814778f05 libnvidia-container-tools_1.20.1-1_amd64.deb
PINS
test -x "$out/x/usr/bin/nvidia-container-cli"
echo "$out/x"
