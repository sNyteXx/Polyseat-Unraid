#!/bin/sh
# WIP / DO NOT USE ON UNRAID. LOCAL-DEV build of the pinned Incus/LXC runtime bundle: NOT-FOR-UNRAID,
# not a release, not a test artifact. Final test artifacts come from the GitHub workflow only.
#
# What it does: compiles the pinned sources into a bundle and records provenance. What it never does: start
# incusd, create a container, install anything on the host, run as root, delete anything, or touch the CI
# gate (it does not set or fake CI variables; inside CI it refuses to run).
#
# usage: dev-build.sh [--docker | --native] [--jobs N] [--memory GB] [--dry-run]
#   --docker (default)  build inside the pinned, unprivileged, resource-limited builder container
#   --native            use the host toolchain (Go and the C tools must already be installed; nothing is installed)
#   --dry-run           validate and print the plan; create nothing
# Paths: POLYSEAT_DEV_HOME (default ~/.cache/polyseat-unraid-dev) holds cache/ and out/<run>/ (mode 0700).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
# shellcheck source=unraid/scripts/lib/dev-guards.sh
. "$here/lib/dev-guards.sh"
# shellcheck source=unraid/scripts/lib/build-core.sh
. "$here/lib/build-core.sh"

mode=docker
jobs=4
mem_gb=12
dry=0
inner=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --docker) mode=docker ;;
    --native) mode=native ;;
    --dry-run) dry=1 ;;
    --inner) inner=1 ;;
    --jobs) shift; jobs=${1:-} ;;
    --memory) shift; mem_gb=${1:-} ;;
    *) dev_die "unknown argument: $1" ;;
  esac
  shift
done
case "$jobs" in '' | *[!0-9]*) dev_die '--jobs must be a number' ;; esac
case "$mem_gb" in '' | *[!0-9]*) dev_die '--memory must be a number of GB' ;; esac
[ "$jobs" -ge 1 ] && [ "$jobs" -le 8 ] || dev_die '--jobs must be between 1 and 8'
[ "$mem_gb" -ge 4 ] && [ "$mem_gb" -le 16 ] || dev_die '--memory must be between 4 and 16 GB'

# ---- guards (run again inside the container) ------------------------------------------------------------
dev_guard_host /etc/unraid-version "$(id -u)" "$(uname -s)" "$(uname -m)"
dev_guard_not_ci "${CI:-}" "${GITHUB_ACTIONS:-}" "${RUNNER_OS:-}" "${RUNNER_ENVIRONMENT:-}"
dev_load_pins "$repo/unraid/build-pins.env"

# ============================================================================================ inner
if [ "$inner" -eq 1 ]; then
  [ "${POLYSEAT_DEV_INNER:-}" = 1 ] || dev_die '--inner is internal; run dev-build.sh without it'
  : "${POLYSEAT_DEV_CACHE:?}" "${POLYSEAT_DEV_OUT:?}" "${POLYSEAT_DEV_RUNID:?}"
  cache=$POLYSEAT_DEV_CACHE
  out=$POLYSEAT_DEV_OUT
  [ -d "$cache" ] && [ ! -L "$cache" ] || dev_die 'cache directory missing or a symlink'
  [ -d "$out" ] && [ ! -L "$out" ] || dev_die 'output directory missing or a symlink'
  [ -z "$(ls -A "$out")" ] || dev_die 'output directory is not empty; refusing to overwrite'
  mkdir -p "${HOME:?}"

  t_start=$(date +%s)
  die() { dev_die "$1"; }
  core_check_pins "$INCUS_COMMIT" "$RAFT_COMMIT" "$COWSQL_COMMIT" "$LXC_COMMIT"

  export CORE_REPO="$repo" CORE_JOBS="$jobs" CORE_LABEL=localdev CORE_MODE=local-dev
  export GOCACHE="$cache/go-build" GOMODCACHE="$cache/go-mod" GOPATH="$cache/gopath" GOFLAGS=-trimpath GOTOOLCHAIN=local
  export CGO_LDFLAGS_ALLOW='(-Wl,-wrap,pthread_create)|(-Wl,-z,now)'
  mkdir -p "$GOCACHE" "$GOMODCACHE" "$GOPATH" "$cache/mirrors" "$cache/deps" "$cache/work"

  # A FIXED build path keeps the path-dependent parts of cgo binaries identical between runs (a per-run
  # directory made incusd differ). Docker: a fresh tmpfs path. Native: a stable path; an earlier tree is
  # moved aside, never deleted.
  work=${POLYSEAT_DEV_WORK:-"$cache/work/build"}
  if [ -e "$work" ] && [ -z "${POLYSEAT_DEV_WORK:-}" ]; then mv "$work" "$work.old.$(date +%s)"; fi
  mkdir -p "$work" "$out/bin"
  key=$(dev_cache_key "$(sha256sum "$here/lib/build-core.sh" | cut -d' ' -f1)" "${POLYSEAT_DEV_IMAGE_ID:-native}")
  deps="$cache/deps/$key"
  prefix="$deps/prefix"
  export PKG_CONFIG_PATH="$prefix/lib/pkgconfig" CGO_CFLAGS="-I$prefix/include" CGO_LDFLAGS="-L$prefix/lib"
  export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export GOBIN="$work/go-bin"
  mkdir -p "$GOBIN"

  t_deps=$(date +%s)
  if [ -f "$deps/.complete" ]; then
    deps_cache=hit
  else
    deps_cache=miss
    # An interrupted earlier build is moved aside, never deleted.
    if [ -e "$deps" ]; then mv "$deps" "$deps.incomplete.$(date +%s)"; fi
    mkdir -p "$prefix"
    core_build_deps "$prefix" "$work" "$cache/mirrors"
    : > "$deps/.complete"
  fi
  t_deps_done=$(date +%s)

  checkout_pinned https://github.com/lxc/incus.git "$INCUS_COMMIT" "$work/incus" "$cache/mirrors/incus.git"
  core_build_incus "$work/incus"
  t_incus_done=$(date +%s)
  core_package "$work/incus" "$prefix" "$out"
  t_pkg_done=$(date +%s)

  printf '%s\n' \
    'LOCAL-DEV BUILD — NOT FOR UNRAID.' \
    'Built locally from the pinned sources by unraid/scripts/dev-build.sh. Not a release, not a CI test artifact,' \
    'not signed, not vulnerability-reviewed. Its bundle version carries the label "localdev" and its hashes differ' \
    'from the CI build by design (-trimpath, different toolchain image). Final test artifacts come from CI only.' \
    'Do not execute it on Unraid, and do not modify Docker, libvirt, networking or GOW.' \
    > "$out/NOTICE.txt"

  {
    printf 'build_mode=local-dev\nlabel=LOCAL-DEV / NOT-FOR-UNRAID\n'
    printf 'git_commit=%s\ngit_dirty_files=%s\ngit_diff_sha256=%s\n' \
      "${POLYSEAT_DEV_GIT_SHA:-unknown}" "${POLYSEAT_DEV_GIT_DIRTY:-unknown}" "${POLYSEAT_DEV_GIT_DIFF:-unknown}"
    printf 'run_id=%s\nbuilt_utc=%s\nbuilder=%s\nbuilder_image_id=%s\nbase_image=%s\n' \
      "$POLYSEAT_DEV_RUNID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mode" "${POLYSEAT_DEV_IMAGE_ID:-native}" "$BUILDER_BASE_IMAGE"
    printf 'limits_jobs=%s\nlimits_memory_gb=%s\nlimits_cpus=%s\n' "$jobs" "${POLYSEAT_DEV_MEM_GB:-n/a}" "${POLYSEAT_DEV_CPUS:-n/a}"
    printf 'deps_cache_key=%s\ndeps_cache=%s\n' "$key" "$deps_cache"
    printf 'seconds_deps=%s\nseconds_incus=%s\nseconds_package=%s\nseconds_total=%s\n' \
      "$((t_deps_done - t_deps))" "$((t_incus_done - t_deps_done))" "$((t_pkg_done - t_incus_done))" "$((t_pkg_done - t_start))"
    grep -v '^#' "$repo/unraid/build-pins.env"
    printf 'tool_go=%s\ntool_gcc=%s\ntool_meson=%s\ntool_patchelf=%s\nhost=%s\n' \
      "$(go version)" "$(gcc --version | head -n1)" "$(meson --version)" "$(patchelf --version)" "$(uname -sm)"
  } > "$out/PROVENANCE.txt"

  core_checksums "$out"
  (cd "$out" && sha256sum PROVENANCE.txt >> SHA256SUMS && sha256sum --check --quiet SHA256SUMS)
  printf 'local-dev build complete (NOT FOR UNRAID): %s\n' "$out"
  exit 0
fi

# ============================================================================================ host
dev_home=${POLYSEAT_DEV_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/polyseat-unraid-dev}
dev_guard_home "$dev_home" "$HOME" "$(id -u)"

git_sha=$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)
git_dirty=$(git -C "$repo" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
git_diff=$(git -C "$repo" diff HEAD 2>/dev/null | sha256sum | cut -d' ' -f1)
suffix=
[ "$git_dirty" = 0 ] || suffix=-dirty
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%s' "$git_sha" | cut -c1-7)$suffix"
out_dir="$dev_home/out/$run_id"
cache_dir="$dev_home/cache"

if [ "$dry" -eq 1 ]; then
  printf 'dry run (nothing created)\nmode=%s jobs=%s memory=%sGB\ndev_home=%s\ncache=%s\noutput=%s\nrepo=%s (%s, %s modified files)\n' \
    "$mode" "$jobs" "$mem_gb" "$dev_home" "$cache_dir" "$out_dir" "$repo" "$git_sha" "$git_dirty"
  printf 'pins:\n'
  grep -v '^#' "$repo/unraid/build-pins.env" | sed 's/^/  /'
  if [ "$mode" = docker ]; then
    printf 'container arguments:\n'
    dev_docker_args "$(id -u)" "$(id -g)" "$jobs" "$mem_gb" dry-run-name "$repo" "$cache_dir" "$out_dir" | sed 's/^/  /'
  fi
  exit 0
fi

# Validate the toolchain BEFORE creating anything, so a failed check leaves no directories behind.
if [ "$mode" = native ]; then
  for tool in go gcc make meson ninja patchelf autoreconf pkg-config git python3 readelf; do
    command -v "$tool" >/dev/null 2>&1 || dev_die "native mode needs '$tool' on PATH (nothing is installed for you)"
  done
  [ "$(go env GOVERSION)" = "go$GO_VERSION" ] || dev_die "native mode needs Go $GO_VERSION (found $(go env GOVERSION))"
else
  command -v docker >/dev/null 2>&1 || dev_die 'docker not found (use --native with a host toolchain instead)'
  docker info >/dev/null 2>&1 || dev_die 'cannot talk to docker (no permission or daemon down); nothing was changed'
fi

umask 077
mkdir -p "$dev_home" "$cache_dir"
chmod 700 "$dev_home"
mkdir -p "$dev_home/out"
dev_guard_resources "$dev_home" 10 "$mem_gb" /proc/meminfo
[ ! -e "$out_dir" ] || dev_die 'output directory already exists; refusing to overwrite'
mkdir "$out_dir"

if [ "$mode" = native ]; then
  POLYSEAT_DEV_INNER=1 POLYSEAT_DEV_CACHE="$cache_dir" POLYSEAT_DEV_OUT="$out_dir" POLYSEAT_DEV_RUNID="$run_id" \
    POLYSEAT_DEV_GIT_SHA="$git_sha" POLYSEAT_DEV_GIT_DIRTY="$git_dirty" POLYSEAT_DEV_GIT_DIFF="$git_diff" \
    POLYSEAT_DEV_MEM_GB="$mem_gb" POLYSEAT_DEV_CPUS="$jobs" \
    nice -n 10 "$0" --inner --native --jobs "$jobs" --memory "$mem_gb"
  exit 0
fi

# The tag depends only on what shapes the image (Dockerfile, base image, Go version and checksum).
image_tag="polyseat-unraid-dev-builder:$({ cat "$here/dev/Dockerfile"; printf '%s\n' "$BUILDER_BASE_IMAGE" "$GO_VERSION" "$GO_LINUX_AMD64_SHA256"; } | sha256sum | cut -c1-12)"
if ! docker image inspect "$image_tag" >/dev/null 2>&1; then
  printf 'building builder image %s (once; apt + pinned Go, no privileged options)\n' "$image_tag"
  docker build -q -t "$image_tag" -f "$here/dev/Dockerfile" \
    --build-arg "BASE=$BUILDER_BASE_IMAGE" --build-arg "GO_VERSION=$GO_VERSION" \
    --build-arg "GO_SHA256=$GO_LINUX_AMD64_SHA256" "$here/dev" > /dev/null
fi
image_id=$(docker image inspect --format '{{.Id}}' "$image_tag")

set -f
IFS='
'
# shellcheck disable=SC2046  # newline-separated argv from dev_docker_args, globbing disabled above
set -- $(dev_docker_args "$(id -u)" "$(id -g)" "$jobs" "$mem_gb" "polyseat-dev-build-$$" "$repo" "$cache_dir" "$out_dir")
IFS=' 
'
set +f
docker "$@" \
  -e POLYSEAT_DEV_INNER=1 -e POLYSEAT_DEV_CACHE=/cache -e POLYSEAT_DEV_OUT=/out -e "POLYSEAT_DEV_RUNID=$run_id" \
  -e POLYSEAT_DEV_WORK=/tmp/polyseat-build \
  -e "POLYSEAT_DEV_GIT_SHA=$git_sha" -e "POLYSEAT_DEV_GIT_DIRTY=$git_dirty" -e "POLYSEAT_DEV_GIT_DIFF=$git_diff" \
  -e "POLYSEAT_DEV_IMAGE_ID=$image_id" -e "POLYSEAT_DEV_MEM_GB=$mem_gb" -e "POLYSEAT_DEV_CPUS=$jobs" \
  "$image_tag" /repo/unraid/scripts/dev-build.sh --inner --docker --jobs "$jobs" --memory "$mem_gb"
printf 'output: %s\n' "$out_dir"
