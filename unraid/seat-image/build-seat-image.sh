#!/bin/sh
# Builds the pinned Polyseat desktop seat image (Incus unified tarball) with Docker.
# Inputs: unraid/build-pins.env only. Output: $SEAT_IMAGE_OUT (default ./seat-image-out) with
#   polyseat-desktop.tar.xz, seat-image.json, polyseat-desktop.cdx.json, polyseat-desktop.tar.xz.sha256
# Never run on Unraid. Needs docker and python3; no root, no Incus. Optional: SOURCE_DATE_EPOCH, SEAT_IMAGE_OUT.
set -eu

fail() {
  printf 'SEAT IMAGE BUILD BLOCKED: %s\n' "$1" >&2
  exit 2
}

[ ! -e /etc/unraid-version ] || fail 'never build on Unraid'
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || fail 'Linux x86_64 required'
command -v docker >/dev/null 2>&1 || fail 'docker required'
command -v python3 >/dev/null 2>&1 || fail 'python3 required'
command -v xz >/dev/null 2>&1 || fail 'xz required'

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
pins=$repo/unraid/build-pins.env

pin() { # KEY: exactly one definition, strict charset
  value=$(sed -n "s/^$1=//p" "$pins")
  [ "$(printf '%s\n' "$value" | wc -l)" = 1 ] && [ -n "$value" ] || fail "pin $1 missing or duplicated"
  printf '%s' "$value" | grep -Eq '^[A-Za-z0-9._:@/+-]+$' || fail "pin $1 has unexpected characters"
  printf '%s' "$value"
}

SEAT_BASE_IMAGE=$(pin SEAT_BASE_IMAGE)
SEAT_ARCHIVE_DATE=$(pin SEAT_ARCHIVE_DATE)
SUNSHINE_PKG_VERSION=$(pin SUNSHINE_PKG_VERSION)
SUNSHINE_PKG_URL=$(pin SUNSHINE_PKG_URL)
SUNSHINE_PKG_SHA256=$(pin SUNSHINE_PKG_SHA256)
printf '%s' "$SEAT_BASE_IMAGE" | grep -Eq '^archlinux@sha256:[0-9a-f]{64}$' || fail 'SEAT_BASE_IMAGE must be archlinux@sha256:<digest>'
printf '%s' "$SEAT_ARCHIVE_DATE" | grep -Eq '^20[0-9]{2}/[0-9]{2}/[0-9]{2}$' || fail 'SEAT_ARCHIVE_DATE must be YYYY/MM/DD'
printf '%s' "$SUNSHINE_PKG_SHA256" | grep -Eq '^[0-9a-f]{64}$' || fail 'SUNSHINE_PKG_SHA256 must be a sha256'

# The image timestamp is the start of the archive day unless the caller says otherwise.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH=$(date -u -d "$(printf '%s' "$SEAT_ARCHIVE_DATE" | tr / -) 00:00:00" +%s)
fi
case "$SOURCE_DATE_EPOCH" in '' | *[!0-9]*) fail 'SOURCE_DATE_EPOCH must be an integer' ;; esac

out=${SEAT_IMAGE_OUT:-$PWD/seat-image-out}
mkdir -p "$out"
work=$(mktemp -d "${TMPDIR:-/tmp}/polyseat-seat-image.XXXXXXXX")
tag=polyseat-seat-image-build:$$
container=
cleanup() {
  [ -z "$container" ] || docker rm -f "$container" >/dev/null 2>&1 || true
  docker rmi -f "$tag" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

build_commit=${GITHUB_SHA:-$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)}

printf 'Building rootfs from %s with archive snapshot %s\n' "$SEAT_BASE_IMAGE" "$SEAT_ARCHIVE_DATE" >&2
docker build --platform linux/amd64 --tag "$tag" \
  --build-arg "SEAT_BASE_IMAGE=docker.io/library/$SEAT_BASE_IMAGE" \
  --build-arg "SEAT_ARCHIVE_DATE=$SEAT_ARCHIVE_DATE" \
  --build-arg "SUNSHINE_PKG_VERSION=$SUNSHINE_PKG_VERSION" \
  --build-arg "SUNSHINE_PKG_URL=$SUNSHINE_PKG_URL" \
  --build-arg "SUNSHINE_PKG_SHA256=$SUNSHINE_PKG_SHA256" \
  "$here"

# `pacman -Q` from the finished image; the container is only ever used for this and the export.
docker run --rm --network none --entrypoint pacman "$tag" -Q > "$work/pacman-q.txt"
container=$(docker create --network none "$tag" /bin/true)
# Streamed: the rootfs (several GB with Steam's 32 bit userspace) never lands on disk, only the final .tar.xz does.
{ docker export "$container" || : > "$work/export-failed"; } |
  python3 -I "$here/pack-image.py" --export - --pacman-q "$work/pacman-q.txt" --out-dir "$out" \
    --epoch "$SOURCE_DATE_EPOCH" --archive-date "$SEAT_ARCHIVE_DATE" --base-image "$SEAT_BASE_IMAGE" \
    --sunshine-version "$SUNSHINE_PKG_VERSION" --sunshine-url "$SUNSHINE_PKG_URL" \
    --sunshine-sha256 "$SUNSHINE_PKG_SHA256" --build-commit "$build_commit"
[ ! -e "$work/export-failed" ] || fail 'docker export failed'
docker rm -f "$container" >/dev/null
container=
