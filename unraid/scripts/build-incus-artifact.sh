#!/bin/sh
# WIP / DO NOT USE. Builds a provenance-labelled userspace proof on disposable CI.
# No Unraid install, service start, network changes, or host configuration changes.
set -eu

die() {
  printf 'BLOCKED: %s\n' "$1" >&2
  exit 2
}

if [ "${CI:-}" != true ] || [ "${GITHUB_ACTIONS:-}" != true ] ||
   [ "${RUNNER_OS:-}" != Linux ] ||
   [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]; then
  die 'disposable GitHub-hosted Linux Actions runner required'
fi
[ ! -e /etc/unraid-version ] ||
  die 'never run this build script on Unraid'

: "${GITHUB_WORKSPACE:?GitHub Actions workspace required}"
: "${RUNNER_TEMP:?GitHub Actions temporary directory required}"
: "${INCUS_COMMIT:?Incus SHA required}"
: "${RAFT_COMMIT:?raft SHA required}"
: "${COWSQL_COMMIT:?cowsql SHA required}"
: "${LXC_COMMIT:?LXC SHA required}"
: "${INCUS_RELEASE:?Release label required}"
: "${SQUASHFS_TOOLS_COMMIT:?squashfs-tools SHA required}"
: "${SQUASHFS_TOOLS_RELEASE:?squashfs-tools release required}"
: "${SQUASHFS_TOOLS_TAG_OBJECT:?squashfs-tools tag object required}"

case "$GITHUB_WORKSPACE:$RUNNER_TEMP" in
  /*:/*) ;;
  *) die 'runner paths must be absolute' ;;
esac

# shellcheck source=unraid/scripts/lib/build-core.sh
. "$GITHUB_WORKSPACE/unraid/scripts/lib/build-core.sh"
core_check_pins "$INCUS_COMMIT" "$RAFT_COMMIT" "$COWSQL_COMMIT" "$LXC_COMMIT" "$SQUASHFS_TOOLS_COMMIT" "$SQUASHFS_TOOLS_TAG_OBJECT"

build_root=$(mktemp -d "$RUNNER_TEMP/polyseat-incus-proof.XXXXXXXX") ||
  die 'could not create isolated build directory'
if [ -n "${GITHUB_ENV:-}" ]; then
  printf 'INCUS_PROOF_STAGE=%s\n' "$build_root" >> "$GITHUB_ENV"
fi
prefix="$build_root/prefix"
src="$build_root/incus"
output="$GITHUB_WORKSPACE/incus-build-proof"
if [ -e "$output" ] || [ -L "$output" ]; then
  die 'proof output directory already exists; refusing to overwrite'
fi
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
export CGO_CFLAGS="-I$prefix/include"
export CGO_LDFLAGS="-L$prefix/lib"
export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CGO_LDFLAGS_ALLOW='(-Wl,-wrap,pthread_create)|(-Wl,-z,now)'
export GOBIN="$build_root/go-bin"

mkdir -p "$prefix" "$GOBIN" "$output/bin"

export CORE_REPO="$GITHUB_WORKSPACE"
export CORE_JOBS=2
export CORE_LABEL=ci
export CORE_MODE=ci

checkout_pinned https://github.com/lxc/incus.git "$INCUS_COMMIT" "$src"
core_build_deps "$prefix" "$build_root"
core_build_incus "$src"
core_package "$src" "$prefix" "$output"

printf '%s\n' \
  'WIP — DO NOT USE ON UNRAID.' \
  'This artifact proves only compilation on an Ubuntu GitHub Actions runner.' \
  'Its SBOM inventories build inputs, not all target runtime dependencies.' \
  'It is not signed, vulnerability-cleared, a complete runtime, or an installer.' \
  'Do not execute it on Unraid, and do not modify Docker, libvirt, networking or GOW.' \
  > "$output/NOTICE.txt"

core_checksums "$output"

printf '%s\n' 'WIP build proof completed; NOT validated for Unraid.'
