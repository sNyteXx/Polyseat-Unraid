#!/bin/sh
# shellcheck shell=sh
# WIP / DO NOT USE ON UNRAID. Shared build core, SOURCED (never executed) by:
#   - unraid/scripts/build-incus-artifact.sh  (GitHub-hosted runner only; keeps its own hard gate)
#   - unraid/scripts/dev-build.sh             (local-dev, NOT-FOR-UNRAID; keeps its own guards)
# The core has no gate of its own and must not be used without one. It installs nothing on the host,
# never starts a daemon and never creates a container.
#
# Caller contract (all must be set): CORE_REPO (repo root), CORE_JOBS, CORE_LABEL (ci|localdev),
# CORE_MODE (ci|local-dev),
# INCUS_*/RAFT_/COWSQL_/LXC_ pins, and PKG_CONFIG_PATH/CGO_*/GOBIN exports for the build prefix.

core_die() {
  printf 'BLOCKED: %s\n' "$1" >&2
  exit 2
}

# Callers define die(); fall back to core_die so the moved functions keep working.
if ! command -v die >/dev/null 2>&1; then die() { core_die "$1"; }; fi

core_check_pins() {
  for pinned in "$@"; do
    case "$pinned" in
      *[!0123456789abcdef]*) die 'a source revision is not a lowercase hex SHA' ;;
    esac
    [ "${#pinned}" -eq 40 ] || die 'source revisions must be full 40-character SHAs'
  done
}

# The upstream Git repositories are fixed; callers cannot supply URLs. Fetching a full SHA, then checking
# HEAD, prevents branch/tag drift. With a mirror directory the SHA is fetched into the mirror once and every
# checkout is made from that mirror (identical result, no network on a warm cache).
checkout_pinned() { # url sha destination [mirror]
  remote_url=$1
  commit=$2
  destination=$3
  mirror=${4:-}
  if [ -n "$mirror" ]; then
    [ -d "$mirror" ] || git init -q --bare "$mirror"
    if ! git -C "$mirror" cat-file -e "$commit^{commit}" 2>/dev/null; then
      git -C "$mirror" fetch -q --depth 1 "$remote_url" "$commit"
    fi
    [ "$(git -C "$mirror" cat-file -t "$commit" 2>/dev/null)" = commit ] || die 'mirror does not contain the pinned commit'
    # Keep the pinned commit reachable (survives gc) and fetchable by SHA from this local mirror only.
    git -C "$mirror" update-ref "refs/pins/$commit" "$commit"
    git -C "$mirror" config uploadpack.allowAnySHA1InWant true
    remote_url=$mirror
  fi
  git init -q "$destination"
  git -C "$destination" remote add origin "$remote_url"
  git -C "$destination" fetch -q --depth 1 origin "$commit"
  git -C "$destination" checkout -q --detach FETCH_HEAD
  actual=$(git -C "$destination" rev-parse HEAD)
  [ "$actual" = "$commit" ] || die 'downloaded source does not match pinned SHA'
}

# shellcheck disable=SC2016  # the literal string $ORIGIN is the ELF RUNPATH token
origin='$ORIGIN'

build_lxc() { # lxc_commit src prefix [mirror]
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

  checkout_pinned https://github.com/lxc/lxc.git "$lxc_commit" "$lxc_src" ${4:+"$4"}

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
  # The compiled-in rootfs mount point is an absolute path outside the prefix and meson install
  # writes a README there. Install into a staging DESTDIR so the build never writes outside its
  # own directory, then move only the prefix tree into place.
  lxc_dest="$lxc_src.dest"
  meson install -C "$lxc_build" --strip --destdir "$lxc_dest"
  mkdir -p "$lxc_prefix"
  cp -a "$lxc_dest$lxc_prefix/." "$lxc_prefix/"

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

# raft, cowsql and liblxc into $1 (prefix), sources below $2 (work dir).
core_build_deps() { # prefix work [mirror_dir]
  deps_prefix=$1
  deps_work=$2
  deps_mirror=${3:-}
  mkdir -p "$deps_work"
  checkout_pinned https://github.com/cowsql/raft.git "$RAFT_COMMIT" "$deps_work/raft" ${deps_mirror:+"$deps_mirror/raft.git"}
  checkout_pinned https://github.com/cowsql/cowsql.git "$COWSQL_COMMIT" "$deps_work/cowsql" ${deps_mirror:+"$deps_mirror/cowsql.git"}
  (
    cd "$deps_work/raft" || exit 1
    autoreconf -fi
    ./configure --prefix="$deps_prefix"
    make -j"$CORE_JOBS"
    make install
  )
  (
    cd "$deps_work/cowsql" || exit 1
    autoreconf -fi
    ./configure --prefix="$deps_prefix"
    make -j"$CORE_JOBS"
    make install
  )
  # Pinned liblxc 6.x without AppArmor/SELinux/OpenSSL/D-Bus: Unraid ships no liblxc, and
  # Incus 7.5 requires LXC >= 6.0.0 (Ubuntu's packaged 5.0.x would not be representative).
  # shellcheck disable=SC2153  # LXC_COMMIT is a caller-supplied pin, not a misspelling of lxc_commit
  build_lxc "$LXC_COMMIT" "$deps_work/lxc" "$deps_prefix" ${deps_mirror:+"$deps_mirror/lxc.git"}
}

core_build_incus() { # incus_src
  pkg-config --modversion raft cowsql lxc
  make -C "$1" -j"$CORE_JOBS"
}

# Everything after the compile: ELF records, version smoke, source SBOM, relocatable payload, ABI gate,
# reproducible bundle, provenance info and checksums. $1 src, $2 prefix, $3 output.
core_package() { # incus_src prefix output
  src=$1
  prefix=$2
  output=$3
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
        die "built $binary has unresolved dependencies on the build host"
      fi
    else
      printf '%s\n' 'No ELF interpreter: static or otherwise non-dynamic binary.' \
        > "$output/$binary-ldd.txt"
    fi
  done

  # No-GPU, no-daemon execution smoke. Neither command initializes Incus, creates a bridge,
  # opens a service, or touches VMs.
  "$GOBIN/incus" --version > "$output/incus-cli-version.txt"
  "$GOBIN/incusd" --version > "$output/incusd-cli-version.txt"
  version_without_v=${INCUS_RELEASE#v}
  grep -Fq "$version_without_v" "$output/incus-cli-version.txt" ||
    die 'Incus CLI did not report the selected source version'
  grep -Fq "$version_without_v" "$output/incusd-cli-version.txt" ||
    die 'Incus daemon binary did not report the selected source version'

  # Source inventory ONLY: it is not a complete runtime SBOM or vulnerability audit.
  (
    cd "$src" || exit 1
    go list -m all > "$output/go-modules.txt"
  )
  go version -m "$GOBIN/incusd" > "$output/incusd-go-buildinfo.txt"
  python3 "$CORE_REPO/unraid/scripts/make_proof_sbom.py" --mode "$CORE_MODE" --allowed-dir "$output" \
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
  python3 "$CORE_REPO/unraid/scripts/check-bundle-abi.py" \
    --payload "$payload" --baseline "$CORE_REPO/unraid/abi/unraid-7.3.3-x86_64" \
    --max-glibc 2.44 --json "$output/abi-report.json" > "$output/abi-report.txt" ||
    { cat "$output/abi-report.txt" >&2; die 'runtime payload does not close against the Unraid 7.3.3 baseline'; }
  glibc_needed=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["glibc_max"])' "$output/abi-report.json")
  mkdir -p "$output/bundle"
  SOURCE_DATE_EPOCH=$(git -C "$src" log -1 --format=%ct) \
    python3 "$CORE_REPO/unraid/scripts/make-runtime-bundle.py" \
    --payload "$payload" --out "$output/bundle" \
    --version "${version_without_v}-polyseat.$CORE_LABEL" --incus-version "$version_without_v" \
    --min-glibc "$glibc_needed" --abi-report "$output/abi-report.json" > "$output/bundle-paths.txt"
  for f in "$output"/bundle/*.tar.gz; do
    printf 'BUNDLE_SHA256=%s\n' "$(basename "$f" .tar.gz)" > "$output/bundle.env"
  done
  for manifest in "$output"/bundle/*.manifest.json; do
    python3 "$CORE_REPO/unraid/scripts/make-runtime-sbom.py" --manifest "$manifest" \
      --abi-report "$output/abi-report.json" --buildinfo "$output/incusd-go-buildinfo.txt" \
      --pins "$CORE_REPO/unraid/build-pins.env" --label "$CORE_LABEL" --output "$output/SBOM-runtime.cdx.json"
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
}

core_checksums() { # output
  (
    cd "$1" || exit 1
    sha256sum -- bin/* bundle/* abi-report.json bundle.env build-info.txt *-ldd.txt *-elf-dynamic.txt \
      *-elf-versions.txt incusd-go-buildinfo.txt \
      incus-cli-version.txt incusd-cli-version.txt \
      go-modules.txt SBOM-build-source-only.cdx.json SBOM-runtime.cdx.json NOTICE.txt > SHA256SUMS
    sha256sum --check SHA256SUMS
  )
}
