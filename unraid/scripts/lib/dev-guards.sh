#!/bin/sh
# shellcheck shell=sh
# WIP / DO NOT USE ON UNRAID. Guard functions for dev-build.sh (local-dev, NOT-FOR-UNRAID), SOURCED.
# Every guard takes its inputs as ARGUMENTS so tests can exercise it without environment tricks; the
# wrapper always passes the real values. Nothing here installs, starts or deletes anything.

dev_die() {
  printf 'DEV-BUILD BLOCKED: %s\n' "$1" >&2
  exit 2
}

# dev_guard_host UNRAID_FILE UID OS ARCH
dev_guard_host() {
  [ ! -e "$1" ] || dev_die 'never run a build on Unraid'
  [ "$2" != 0 ] || dev_die 'refusing to run as root: the local-dev build is unprivileged'
  [ "$3" = Linux ] || dev_die 'Linux required'
  [ "$4" = x86_64 ] || dev_die 'x86_64 required'
}

# Refuse inside CI: the CI path has its own, stricter script. This is NOT a bypass of that gate and it
# never sets CI variables itself.
dev_guard_not_ci() { # CI GITHUB_ACTIONS RUNNER_OS RUNNER_ENVIRONMENT
  if [ -n "$1" ] || [ -n "$2" ] || [ -n "$3" ] || [ -n "$4" ]; then
    dev_die 'CI environment detected; use unraid/scripts/build-incus-artifact.sh in the GitHub workflow'
  fi
}

# dev_guard_home DIR HOME UID : absolute, below $HOME, no symlinks, owned by us, private
dev_guard_home() {
  case "$1" in
    /*) ;;
    *) dev_die 'POLYSEAT_DEV_HOME must be an absolute path' ;;
  esac
  case "$1" in
    */../* | */.. | *'/./'* | */.) dev_die 'POLYSEAT_DEV_HOME must be a normalized path' ;;
  esac
  case "$1/" in
    "$2"/*) ;;
    *) dev_die 'POLYSEAT_DEV_HOME must be below your home directory' ;;
  esac
  [ "$1" != "$2" ] || dev_die 'POLYSEAT_DEV_HOME must not be the home directory itself'
  if [ -L "$1" ]; then dev_die 'POLYSEAT_DEV_HOME must not be a symlink'; fi
  if [ -e "$1" ]; then
    [ -d "$1" ] || dev_die 'POLYSEAT_DEV_HOME exists and is not a directory'
    [ "$(stat -c %u "$1")" = "$3" ] || dev_die 'POLYSEAT_DEV_HOME is not owned by you'
    [ "$(stat -c %a "$1")" = 700 ] || dev_die 'POLYSEAT_DEV_HOME must have mode 0700'
  fi
}

# Strict KEY=VALUE reader: the file is never sourced, so it cannot execute anything.
dev_load_pins() { # file
  if [ ! -f "$1" ] || [ -L "$1" ]; then
    dev_die "pin file missing: $1"
  fi
  while IFS= read -r line; do
    case "$line" in '' | '#'*) continue ;; esac
    printf '%s\n' "$line" | grep -Eq '^[A-Z][A-Z0-9_]*=[A-Za-z0-9._:@/+-]+$' ||
      dev_die "malformed pin line: $line"
    key=${line%%=*}
    val=${line#*=}
    case "$key" in
      INCUS_COMMIT | INCUS_TAG_OBJECT | RAFT_COMMIT | COWSQL_COMMIT | LXC_COMMIT | SQUASHFS_TOOLS_COMMIT | \
      SQUASHFS_TOOLS_TAG_OBJECT)
        printf '%s' "$val" | grep -Eq '^[0-9a-f]{40}$' || dev_die "$key must be a full lowercase 40-character SHA" ;;
      GO_LINUX_AMD64_SHA256 | BUSYBOX_DEB_SHA256 | BUSYBOX_BINARY_SHA256 | SUNSHINE_PKG_SHA256)
        printf '%s' "$val" | grep -Eq '^[0-9a-f]{64}$' || dev_die "$key must be a 64-character SHA-256" ;;
      SEAT_BASE_IMAGE)
        printf '%s' "$val" | grep -Eq '^archlinux@sha256:[0-9a-f]{64}$' || dev_die 'SEAT_BASE_IMAGE must be the official archlinux image pinned by digest' ;;
      SEAT_ARCHIVE_DATE)
        printf '%s' "$val" | grep -Eq '^20[0-9]{2}/(0[1-9]|1[0-2])/(0[1-9]|[12][0-9]|3[01])$' || dev_die 'SEAT_ARCHIVE_DATE must be YYYY/MM/DD' ;;
      SUNSHINE_PKG_VERSION)
        printf '%s' "$val" | grep -Eq '^[0-9.]+-[0-9.]+$' || dev_die 'SUNSHINE_PKG_VERSION must look like 2026.914.233613-1.1' ;;
      SUNSHINE_PKG_URL)
        case "$val" in
          https://github.com/superuser404notfound/Polyseat/releases/download/sunshine-*-x86_64.pkg.tar.zst) ;;
          *) dev_die 'SUNSHINE_PKG_URL must be the Polyseat sunshine release asset' ;;
        esac ;;
      BUILDER_BASE_IMAGE)
        printf '%s' "$val" | grep -Eq '^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$' || dev_die 'BUILDER_BASE_IMAGE must be pinned by digest' ;;
      INCUS_RELEASE | LXC_RELEASE | RAFT_RELEASE | COWSQL_RELEASE | GO_VERSION | BUSYBOX_VERSION | BUSYBOX_DEB_VERSION) ;;
      SQUASHFS_TOOLS_RELEASE)
        printf '%s' "$val" | grep -Eq '^[0-9]+\.[0-9]+(\.[0-9]+)?$' || dev_die 'SQUASHFS_TOOLS_RELEASE must be a plain release number' ;;
      BUSYBOX_DEB_URL)
        case "$val" in
          https://security.ubuntu.com/ubuntu/pool/main/b/busybox/busybox-static_*_amd64.deb) ;;
          *) dev_die 'BUSYBOX_DEB_URL must be the official Ubuntu amd64 busybox-static package' ;;
        esac ;;
      *) dev_die "unknown pin key: $key" ;;
    esac
    eval "$key=\$val"
    # shellcheck disable=SC2163  # exporting the variable named by $key is intended
    export "$key"
  done < "$1"
  for need in INCUS_COMMIT INCUS_RELEASE INCUS_TAG_OBJECT RAFT_COMMIT COWSQL_COMMIT LXC_COMMIT LXC_RELEASE GO_VERSION \
              GO_LINUX_AMD64_SHA256 BUILDER_BASE_IMAGE BUSYBOX_VERSION BUSYBOX_DEB_VERSION BUSYBOX_DEB_URL \
              BUSYBOX_DEB_SHA256 BUSYBOX_BINARY_SHA256 SQUASHFS_TOOLS_COMMIT SQUASHFS_TOOLS_RELEASE \
              SQUASHFS_TOOLS_TAG_OBJECT; do
    eval "[ -n \"\${$need:-}\" ]" || dev_die "pin missing: $need"
  done
}

# The deps cache is keyed by everything that can change the result; a different pin, core script,
# toolchain image or flag set can never reuse another entry.
dev_cache_key() { # core_script_sha256 builder_image_id
  printf '%s\n' "$RAFT_COMMIT" "$COWSQL_COMMIT" "$LXC_COMMIT" "$SQUASHFS_TOOLS_COMMIT" "$GO_VERSION" "$BUILDER_BASE_IMAGE" "$1" "$2" |
    sha256sum | cut -c1-24
}

# Plain argv, one element per line, for `docker run`. No privileged mode, no extra capabilities, no
# devices, no host namespaces, no docker socket: only the three bind mounts below.
dev_docker_args() { # uid gid cpus mem_gb name repo cache out
  printf '%s\n' run --rm --init --name "$5" \
    --user "$1:$2" --cap-drop ALL --security-opt no-new-privileges \
    --cpus "$3" --memory "$4g" --memory-swap "$4g" --pids-limit 2048 \
    --read-only --tmpfs /tmp:rw,exec,nosuid,size=6g \
    --ipc private \
    -e HOME=/tmp/home -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$6:/repo:ro" -v "$7:/cache" -v "$8:/out"
}

# dev_guard_resources PATH MIN_FREE_GB MEM_GB MEMINFO_FILE
dev_guard_resources() {
  free_kb=$(df -Pk "$1" | awk 'NR==2 {print $4}')
  [ "$free_kb" -ge $(($2 * 1024 * 1024)) ] || dev_die "less than $2 GB free on the cache filesystem; aborting"
  avail_kb=$(awk '/^MemAvailable:/ {print $2}' "$4")
  need_kb=$(($3 * 1024 * 1024 + 2 * 1024 * 1024))
  [ "$avail_kb" -ge "$need_kb" ] || dev_die "less than $3 GB (+2 GB headroom) of memory available; aborting"
}
