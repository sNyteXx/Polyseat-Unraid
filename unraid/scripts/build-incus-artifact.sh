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

case "$GITHUB_WORKSPACE:$RUNNER_TEMP" in
  /*:/*) ;;
  *) die 'runner paths must be absolute' ;;
esac

for pinned in "$INCUS_COMMIT" "$RAFT_COMMIT" "$COWSQL_COMMIT" "$LXC_COMMIT"; do
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

# The upstream Git repositories are fixed; callers cannot supply URLs.
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

# shellcheck disable=SC2016  # the literal string $ORIGIN is the ELF RUNPATH token
origin='$ORIGIN'

build_lxc() {
  lxc_commit=$1
  lxc_src=$2
  lxc_prefix=$3
  lxc_build="$lxc_src.build"

  case "$lxc_commit" in
    *[!0123456789abcdef]*) die 'LXC revision is not a lowercase hex SHA' ;;
  esac
  [ "${#lxc_commit}" -eq 40 ] || die 'LXC revision must be a full 40-character SHA'
  case "$lxc_prefix" in
    /*) ;;
    *) die 'LXC prefix must be absolute' ;;
  esac

  checkout_pinned https://github.com/lxc/lxc.git "$lxc_commit" "$lxc_src"

  # Only the library is wanted: no commands/tools/examples/man/tests/init files,
  # and no optional dependency that Unraid (Slackware) would have to provide
  # (apparmor, selinux, openssl, dbus; pam and io_uring are off by default).
  # -ffile-prefix-map keeps the build directory out of the binary.
  meson setup "$lxc_build" "$lxc_src" \
    --prefix="$lxc_prefix" --libdir=lib --buildtype=release \
    -Dc_args="-ffile-prefix-map=$lxc_src=lxc -ffile-prefix-map=$lxc_build=lxc-build" \
    -Dapparmor=false -Dselinux=false -Dopenssl=false -Ddbus=false \
    -Dseccomp=true -Dcapabilities=true \
    -Dcommands=false -Dtools=false -Dtools-multicall=false \
    -Dexamples=false -Dman=false -Dapi-docs=false -Dtests=false \
    -Dpam-cgroup=false -Dio-uring-event-loop=false \
    -Dinstall-init-files=false -Dinstall-state-dirs=false -Dspecfile=false \
    -Dcoverity-build=false -Dthread-safety=true \
    -Drootfs-mount-path=/var/lib/polyseat-lxc/rootfs
  ninja -C "$lxc_build"
  meson install -C "$lxc_build" --strip

  lxc_lib="$lxc_prefix/lib/liblxc.so.1"
  [ -e "$lxc_lib" ] || die 'liblxc.so.1 was not installed'
  lxc_real=$(readlink -f "$lxc_lib")

  # RUNPATH (DT_RUNPATH) = $ORIGIN so bundled sibling libs are found.
  patchelf --set-rpath "$origin" "$lxc_real"

  readelf -W -d "$lxc_real" | grep -Fq "[$origin]" ||
    die "liblxc RUNPATH is not $origin"
  if readelf -W -d "$lxc_real" | grep -qE '\((RPATH)\)'; then
    die 'liblxc has a legacy DT_RPATH'
  fi
  # The rootfs mount point is compiled in as an absolute path; it must be the stable plugin-owned
  # directory the controller creates, never a build-directory path that only exists on CI.
  grep -aFq '/var/lib/polyseat-lxc/rootfs' "$lxc_real" ||
    die 'liblxc does not carry the fixed rootfs mount path'
  if grep -aFq "$lxc_prefix/lib/lxc/rootfs" "$lxc_real"; then
    die 'liblxc still references a build-prefix rootfs mount path'
  fi
  lxc_needed=$(readelf -W -d "$lxc_real" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')
  for lib in $lxc_needed; do
    case "$lib" in
      libseccomp.so.2|libcap.so.2|libc.so.6|libm.so.6|libpthread.so.0|\
      libdl.so.2|librt.so.1|libgcc_s.so.1|ld-linux-x86-64.so.2) ;;
      *) die "liblxc has an unexpected dependency: $lib" ;;
    esac
  done
  lxc_glibc_max=$(readelf -W -V "$lxc_real" | grep -o 'GLIBC_[0-9][0-9.]*' |
    sed 's/GLIBC_//' | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)
  case "$lxc_glibc_max" in
    2.[0-9]|2.[12][0-9]|2.3[0-8]|2.2.5|2.3.[234]) ;;
    *) die "liblxc requires glibc $lxc_glibc_max (> 2.38)" ;;
  esac
  printf 'liblxc: NEEDED=%s GLIBC_MAX=%s\n' \
    "$(printf '%s' "$lxc_needed" | tr '\n' ' ')" "$lxc_glibc_max"
}

checkout_pinned https://github.com/lxc/incus.git "$INCUS_COMMIT" "$src"
checkout_pinned https://github.com/cowsql/raft.git "$RAFT_COMMIT" "$raft"
checkout_pinned https://github.com/cowsql/cowsql.git "$COWSQL_COMMIT" "$cowsql"
lxc="$build_root/lxc"

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

# Pinned liblxc 6.x without AppArmor/SELinux/OpenSSL/D-Bus: Unraid ships no liblxc, and
# Incus 7.5 requires LXC >= 6.0.0 (Ubuntu's packaged 5.0.x would not be representative).
build_lxc "$LXC_COMMIT" "$lxc" "$prefix"

pkg-config --modversion raft cowsql lxc
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

# ---- Relocatable runtime payload for the Unraid bundle (build proof, not an installer) ----
# Layout: bin/{incus,incusd}, lib/{liblxc.so.1,libcowsql.so.0,libraft.so.0} as real files named
# by SONAME (the bundle format forbids symlinks). incusd and the libraries get an
# origin-relative RUNPATH so no LD_LIBRARY_PATH or ld.so.conf change is needed on the host.
payload="$output/payload"
mkdir -p "$payload/bin" "$payload/lib"
cp "$GOBIN/incus" "$GOBIN/incusd" "$payload/bin/"
for soname in liblxc.so.1 libcowsql.so.0 libraft.so.0; do
  [ -e "$prefix/lib/$soname" ] || die "missing runtime library: $soname"
  cp "$(readlink -f "$prefix/lib/$soname")" "$payload/lib/$soname"
  strip --strip-unneeded "$payload/lib/$soname"
  patchelf --set-rpath "$origin" "$payload/lib/$soname"
done
patchelf --set-rpath "$origin/../lib" "$payload/bin/incusd"
chmod 0755 "$payload/bin/incus" "$payload/bin/incusd"
chmod 0644 "$payload/lib/"*

# The bundle may only be built if every library resolves on the recorded Unraid baseline.
python3 "$GITHUB_WORKSPACE/unraid/scripts/check-bundle-abi.py" \
  --payload "$payload" --baseline "$GITHUB_WORKSPACE/unraid/abi/unraid-7.3.3-x86_64" \
  --max-glibc 2.44 --json "$output/abi-report.json" > "$output/abi-report.txt" ||
  { cat "$output/abi-report.txt" >&2; die 'runtime payload does not close against the Unraid 7.3.3 baseline'; }
glibc_needed=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["glibc_max"])' "$output/abi-report.json")
mkdir -p "$output/bundle"
SOURCE_DATE_EPOCH=$(git -C "$src" log -1 --format=%ct) \
  python3 "$GITHUB_WORKSPACE/unraid/scripts/make-runtime-bundle.py" \
  --payload "$payload" --out "$output/bundle" \
  --version "${version_without_v}-polyseat.ci" --incus-version "$version_without_v" \
  --min-glibc "$glibc_needed" --abi-report "$output/abi-report.json" > "$output/bundle-paths.txt"
for f in "$output"/bundle/*.tar.gz; do
  printf 'BUNDLE_SHA256=%s\n' "$(basename "$f" .tar.gz)" > "$output/bundle.env"
done

{
  printf 'INCUS_COMMIT=%s\n' "$INCUS_COMMIT"
  printf 'INCUS_RELEASE=%s\n' "$INCUS_RELEASE"
  printf 'RAFT_COMMIT=%s\n' "$RAFT_COMMIT"
  printf 'COWSQL_COMMIT=%s\n' "$COWSQL_COMMIT"
  printf 'LXC_COMMIT=%s\n' "$LXC_COMMIT"
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
  sha256sum -- bin/* bundle/* abi-report.json bundle.env build-info.txt *-ldd.txt *-elf-dynamic.txt \
    *-elf-versions.txt incusd-go-buildinfo.txt \
    incus-cli-version.txt incusd-cli-version.txt \
    go-modules.txt SBOM-build-source-only.cdx.json NOTICE.txt > SHA256SUMS
  sha256sum --check SHA256SUMS
)

printf '%s\n' 'WIP build proof completed; NOT validated for Unraid.'
