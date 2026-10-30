#!/bin/sh
# WIP DO NOT USE. Build only in disposable Linux CI, never on Unraid.
set -eu
if [ "${CI:-}" != "true" ] || [ "${RUNNER_OS:-}" != "Linux" ]; then
  echo 'Refusing: requires isolated Linux CI runner' >&2; exit 2
fi
if [ -f /etc/unraid-version ]; then echo 'Refusing Unraid host' >&2; exit 2; fi
: "${INCUS_VERSION:?Pin an explicit upstream release version}"
case "$INCUS_VERSION" in *[!0-9A-Za-z._-]*|'') echo 'Invalid version' >&2; exit 2;; esac
printf 'WIP artifact metadata only; build integration not validated: %s\n' "$INCUS_VERSION"
mkdir -p dist
printf 'incus_version=%s\nbuild_status=blocked_until_toolchain_and_source_pin_verified\n' "$INCUS_VERSION" > dist/incus-build-status.txt
sha256sum dist/incus-build-status.txt > dist/SHA256SUMS
