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
: "${INCUS_RELEASE:?Release label required}"

case "$GITHUB_WORKSPACE:$RUNNER_TEMP" in
  /*:/*) ;;
  *) die 'runner paths must be absolute' ;;
esac

for pinned in "$INCUS_COMMIT" "$RAFT_COMMIT" "$COWSQL_COMMIT"; do
  case "$pinned" in
    *[!0123456789abcdef]*) die 'a source revision is not a lowercase hex SHA' ;;
  esac
  [ "${#pinned}" -eq 40 ] || die 'source revisions must be full 40-character SHAs'
done

build_root=$(mktemp -d "$RUNNER_TEMP/polyseat-incus-proof.XXXXXXXX") ||
  die 'could not create isolated build directory'
if [ -n "${GITHUB_ENV:-}" ]; then
  printf 'INCUS_PROOF_STAGE=%s\n' "$build_root" >> "$GITHUB_ENV"
fi
prefix="$build_root/prefix"
src="$build_root/incus"
raft="$build_root/raft"
cowsql="$build_root/cowsql"
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

# The three upstream Git repositories are fixed; callers cannot supply URLs.
# Fetching a full SHA, then checking HEAD, prevents branch/tag drift.
checkout_pinned() {
  remote_url=$1
  commit=$2
  destination=$3
  git init -q "$destination"
  git -C "$destination" remote add origin "$remote_url"
  git -C "$destination" fetch -q --depth 1 origin "$commit"
  git -C "$destination" checkout -q --detach FETCH_HEAD
  actual=$(git -C "$destination" rev-parse HEAD)
  [ "$actual" = "$commit" ] || die 'downloaded source does not match pinned SHA'
}

checkout_pinned https://github.com/lxc/incus.git "$INCUS_COMMIT" "$src"
checkout_pinned https://github.com/cowsql/raft.git "$RAFT_COMMIT" "$raft"
checkout_pinned https://github.com/cowsql/cowsql.git "$COWSQL_COMMIT" "$cowsql"

(
  cd "$raft"
  autoreconf -fi
  ./configure --prefix="$prefix"
  make -j2
  make install
)

(
  cd "$cowsql"
  autoreconf -fi
  ./configure --prefix="$prefix"
  make -j2
  make install
)

pkg-config --modversion raft cowsql
make -C "$src" -j2

for binary in incus incusd incus-agent incus-migrate; do
  [ -x "$GOBIN/$binary" ] || die "missing expected built binary: $binary"
  cp "$GOBIN/$binary" "$output/bin/$binary"
done

# A proof of compilation is not a relocatable runtime. Record each binary's
# Ubuntu ELF ABI requirements and resolved libraries, not just incusd's.
# Report the host linkage; do not bundle Ubuntu's libraries for Unraid.
for binary in incus incusd incus-agent incus-migrate; do
  readelf -W -d "$GOBIN/$binary" > "$output/$binary-elf-dynamic.txt"
  readelf -W --version-info "$GOBIN/$binary" > "$output/$binary-elf-versions.txt"
  if readelf -W -l "$GOBIN/$binary" | grep -q 'INTERP'; then
    ldd "$GOBIN/$binary" > "$output/$binary-ldd.txt"
    if grep -q 'not found' "$output/$binary-ldd.txt"; then
      die "built $binary has unresolved dependencies on Ubuntu runner"
    fi
  else
    printf '%s\n' 'No ELF interpreter: static or otherwise non-dynamic binary.' \
      > "$output/$binary-ldd.txt"
  fi
done

# No-GPU, no-daemon execution smoke on the disposable CI runner. Neither
# command initializes Incus, creates a bridge, opens a service, or touches VMs.
"$GOBIN/incus" --version > "$output/incus-cli-version.txt"
"$GOBIN/incusd" --version > "$output/incusd-cli-version.txt"
version_without_v=${INCUS_RELEASE#v}
grep -Fq "$version_without_v" "$output/incus-cli-version.txt" ||
  die 'Incus CLI did not report the selected source version'
grep -Fq "$version_without_v" "$output/incusd-cli-version.txt" ||
  die 'Incus daemon binary did not report the selected source version'

# Source inventory ONLY: it is not a complete runtime SBOM or vulnerability audit.
(
  cd "$src"
  go list -m all > "$output/go-modules.txt"
)
go version -m "$GOBIN/incusd" > "$output/incusd-go-buildinfo.txt"
python3 "$GITHUB_WORKSPACE/unraid/scripts/make_proof_sbom.py" \
  --modules "$output/go-modules.txt" \
  --output "$output/SBOM-build-source-only.cdx.json"

{
  printf 'INCUS_COMMIT=%s\n' "$INCUS_COMMIT"
  printf 'INCUS_RELEASE=%s\n' "$INCUS_RELEASE"
  printf 'RAFT_COMMIT=%s\n' "$RAFT_COMMIT"
  printf 'COWSQL_COMMIT=%s\n' "$COWSQL_COMMIT"
  printf 'GO_VERSION=%s\n' "$(go version)"
  printf 'BUILD_HOST=%s\n' "$(uname -sm)"
} > "$output/build-info.txt"

printf '%s\n' \
  'WIP — DO NOT USE ON UNRAID.' \
  'This artifact proves only compilation on an Ubuntu GitHub Actions runner.' \
  'Its SBOM inventories build inputs, not all target runtime dependencies.' \
  'It is not signed, vulnerability-cleared, a complete runtime, or an installer.' \
  'Do not execute it on Unraid, and do not modify Docker, libvirt, networking or GOW.' \
  > "$output/NOTICE.txt"

(
  cd "$output"
  sha256sum bin/* build-info.txt *-ldd.txt *-elf-dynamic.txt \
    *-elf-versions.txt incusd-go-buildinfo.txt \
    incus-cli-version.txt incusd-cli-version.txt \
    go-modules.txt SBOM-build-source-only.cdx.json NOTICE.txt > SHA256SUMS
  sha256sum --check SHA256SUMS
)

printf '%s\n' 'WIP build proof completed; NOT validated for Unraid.'
