#!/bin/sh
# WIP: disposable GitHub-hosted runner ONLY. Does not access Unraid, VM or GPU.
# Real Incus daemon and one CPU-only BusyBox container, with no configured NIC.
set -eu

fail() {
  printf 'CPU SMOKE BLOCKED: %s\n' "$1" >&2
  exit 2
}

if [ "${CI:-}" != true ] || [ "${GITHUB_ACTIONS:-}" != true ] ||
   [ "${RUNNER_OS:-}" != Linux ] ||
   [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ] ||
   [ -e /etc/unraid-version ]; then
  fail 'GitHub-hosted disposable Linux runner required; Unraid forbidden'
fi
[ "$(id -u)" -eq 0 ] || fail 'root required only on disposable CI runner'
: "${RUNNER_TEMP:?}"
: "${INCUS_PROOF_STAGE:?}"
: "${GITHUB_WORKSPACE:?}"

case "$INCUS_PROOF_STAGE" in
  "$RUNNER_TEMP"/polyseat-incus-proof.*) ;;
  *) fail 'unexpected CI staging directory' ;;
esac
if [ ! -d "$INCUS_PROOF_STAGE" ] || [ -L "$INCUS_PROOF_STAGE" ]; then
  fail 'staging directory missing or symlinked'
fi

bin="$INCUS_PROOF_STAGE/go-bin"
library="$INCUS_PROOF_STAGE/prefix/lib"
importer="$INCUS_PROOF_STAGE/incus/test/deps/import-busybox"
if [ ! -x "$bin/incusd" ] || [ ! -x "$bin/incus" ] || [ ! -r "$importer" ]; then
  fail 'pinned Incus binaries or upstream BusyBox importer missing'
fi

export LD_LIBRARY_PATH="$library"
export INCUS_DIR="$INCUS_PROOF_STAGE/smoke-state"
export INCUS_CONF="$INCUS_PROOF_STAGE/smoke-client"
export INCUS_EXEC_PATH="$bin/incusd"
export INCUS_SKIP_INSTANCE_TYPES=true
export PATH="$bin:$PATH"
umask 077
mkdir -p "$INCUS_DIR" "$INCUS_CONF"

if command -v pgrep >/dev/null 2>&1 && pgrep -x incusd >/dev/null 2>&1; then
  fail 'another Incus daemon is already running on the disposable runner'
fi

daemon_pid=
cleanup() {
  # Only the unique lab instance and its daemon are within this script's scope.
  if [ -S "$INCUS_DIR/unix.socket" ]; then
    "$bin/incus" stop polyseat-ci-cpu --force --timeout 15 >/dev/null 2>&1 || true
    "$bin/incus" delete polyseat-ci-cpu >/dev/null 2>&1 || true
  fi
  if [ -n "$daemon_pid" ] && kill -0 "$daemon_pid" 2>/dev/null; then
    kill -TERM "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

printf '%s\n' 'WIP CPU-only smoke: Incus starts on disposable runner only.'
"$bin/incusd" --debug --logfile "$INCUS_PROOF_STAGE/incusd-smoke.log" &
daemon_pid=$!

ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  if [ -S "$INCUS_DIR/unix.socket" ] && "$bin/incus" info >/dev/null 2>&1; then
    ready=true
    break
  fi
  if ! kill -0 "$daemon_pid" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
if [ "$ready" != true ]; then
  sed -n '1,160p' "$INCUS_PROOF_STAGE/incusd-smoke.log" >&2 || true
  fail 'Incus daemon not healthy within 60 seconds'
fi

# NEVER use `incus admin init --minimal`: upstream auto-init creates incusbr0.
# This explicit preseed creates a dir pool and disk-only profile, no NIC/bridge.
preseed="$INCUS_PROOF_STAGE/smoke-preseed.yaml"
printf '%s\n' \
  'config: {}' \
  'storage_pools:' \
  '  - name: default' \
  '    driver: dir' \
  'profiles:' \
  '  - name: default' \
  '    devices:' \
  '      root:' \
  '        type: disk' \
  '        path: /' \
  '        pool: default' > "$preseed"
"$bin/incus" admin init --preseed < "$preseed"

"$bin/incus" profile show default > "$INCUS_PROOF_STAGE/smoke-profile.txt"
if grep -Eq '(eth0:|type: nic|network:)' "$INCUS_PROOF_STAGE/smoke-profile.txt"; then
  fail 'default profile unexpectedly contains network configuration'
fi
if [ -e /sys/class/net/incusbr0 ]; then
  fail 'unexpected Incus bridge created in CPU-only smoke'
fi

# The verified upstream source tree contains a BusyBox-only image generator.
# It uses locally installed busybox-static, not an external image registry.
python3 "$importer" --alias polyseat-ci-busybox

"$bin/incus" init polyseat-ci-busybox polyseat-ci-cpu --storage default
"$bin/incus" start polyseat-ci-cpu
"$bin/incus" exec polyseat-ci-cpu -- /bin/busybox echo CPU_ONLY_SMOKE_OK
"$bin/incus" stop polyseat-ci-cpu --force --timeout 15
"$bin/incus" start polyseat-ci-cpu
"$bin/incus" exec polyseat-ci-cpu -- /bin/busybox echo CPU_ONLY_RESTART_OK
"$bin/incus" stop polyseat-ci-cpu --force --timeout 15
"$bin/incus" delete polyseat-ci-cpu

if [ -e /sys/class/net/incusbr0 ]; then
  fail 'a bridge appeared after container lifecycle'
fi
printf '%s\n' 'PASS: real CPU-only Incus container start, exec, stop, restart, cleanup.'
