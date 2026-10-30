#!/bin/sh
# WIP / BUILD ONLY. Fetch one hash-pinned Ubuntu binary input, never install it.
# Usage: prepare-canary-image.sh build-pins.env IMAGE.tar.gz PROVENANCE.json
# Both CI and local-dev call this before make-runtime-bundle.py. No host/Incus actions.
set -eu

fail() { printf 'CANARY INPUT BLOCKED: %s\n' "$1" >&2; exit 2; }
[ "$#" -eq 3 ] || fail 'expected pin file, new image path and new provenance path'
[ ! -e /etc/unraid-version ] || fail 'never prepare build inputs on Unraid'
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=unraid/scripts/lib/dev-guards.sh
. "$here/lib/dev-guards.sh"
dev_load_pins "$1"
image=$2
provenance=$3
for tool in curl sha256sum dpkg-deb tar python3; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing build tool: $tool"
done
if [ -e "$image" ] || [ -L "$image" ]; then fail 'image destination already exists'; fi
if [ -e "$provenance" ] || [ -L "$provenance" ]; then fail 'provenance destination already exists'; fi
[ "$image" != "$provenance" ] || fail 'image and provenance must differ'
# The fixed URL/pins are not commands. The strict reader rejects substitutions.
case "$BUSYBOX_DEB_URL" in
  https://security.ubuntu.com/ubuntu/pool/main/b/busybox/busybox-static_*_amd64.deb) ;;
  *) fail 'BusyBox source must be the pinned Ubuntu security archive' ;;
esac
umask 077
input_dir=$(mktemp -d "${TMPDIR:-/tmp}/polyseat-busybox-input.XXXXXXXX")
cleanup() {
  # Only files created by this invocation; never recursively remove a directory.
  rm -f -- "$input_dir/source.deb" "$input_dir/payload.tar" "$input_dir/busybox"
  rmdir -- "$input_dir" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --max-time 120 --retry 2 --max-filesize 2097152 \
  "$BUSYBOX_DEB_URL" -o "$input_dir/source.deb"
printf '%s  %s\n' "$BUSYBOX_DEB_SHA256" "$input_dir/source.deb" | sha256sum --check --status ||
  fail 'Ubuntu BusyBox package SHA-256 mismatch'
[ "$(dpkg-deb -f "$input_dir/source.deb" Package)" = busybox-static ] || fail 'unexpected package'
[ "$(dpkg-deb -f "$input_dir/source.deb" Version)" = "$BUSYBOX_DEB_VERSION" ] || fail 'package version mismatch'
[ "$(dpkg-deb -f "$input_dir/source.deb" Architecture)" = amd64 ] || fail 'package architecture mismatch'
# No dpkg installation and no archive extraction to host paths. Only one member
# is copied from the already authenticated package into the private tempdir.
dpkg-deb --fsys-tarfile "$input_dir/source.deb" > "$input_dir/payload.tar"
tar -xOf "$input_dir/payload.tar" ./usr/bin/busybox > "$input_dir/busybox"
printf '%s  %s\n' "$BUSYBOX_BINARY_SHA256" "$input_dir/busybox" | sha256sum --check --status ||
  fail 'BusyBox binary SHA-256 mismatch'
mkdir -p -- "$(dirname -- "$image")" "$(dirname -- "$provenance")"
python3 "$here/make-canary-image.py" \
  --binary "$input_dir/busybox" --binary-sha256 "$BUSYBOX_BINARY_SHA256" \
  --version "$BUSYBOX_VERSION" \
  --source-id "ubuntu:busybox-static=$BUSYBOX_DEB_VERSION" \
  --source-uri "$BUSYBOX_DEB_URL" --source-sha256 "$BUSYBOX_DEB_SHA256" \
  --out "$image" --provenance "$provenance"
