#!/bin/sh
# WIP: disposable GitHub-hosted runner ONLY. Does not access Unraid, a VM or a GPU.
# Imports the Polyseat desktop seat image into a real Incus daemon, runs ONE unprivileged container WITHOUT any NIC and
# lets the checks inside the image (polyseat-selftest) prove the headless sway desktop and Sunshine's software encode.
#
# Parameters (environment): SEAT_IMAGE (tar.xz, required), SEAT_IMAGE_JSON (seat-image.json), OUT_DIR,
#   INCUS_BIN (default incus), INCUSD_BIN (default incusd), INCUS_DIR (default: fresh /tmp dir),
#   LXC_ROOTFS_MOUNT (optional directory a custom-built liblxc expects; created and removed here).
# The host-side incus sequence is fixed on purpose (see run_cycle); an Unraid controller can allow-list exactly it.
set -eu

fail() {
  printf 'DESKTOP SMOKE BLOCKED: %s\n' "$1" >&2
  exit 2
}

if [ "${CI:-}" != true ] || [ "${GITHUB_ACTIONS:-}" != true ] ||
   [ "${RUNNER_OS:-}" != Linux ] ||
   [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ] ||
   [ -e /etc/unraid-version ]; then
  fail 'GitHub-hosted disposable Linux runner required; Unraid forbidden'
fi
[ "$(id -u)" -eq 0 ] || fail 'root required only on disposable CI runner'
: "${GITHUB_WORKSPACE:?}"
: "${SEAT_IMAGE:?}"

INCUS_BIN=${INCUS_BIN:-incus}
INCUSD_BIN=${INCUSD_BIN:-incusd}
SEAT_IMAGE_JSON=${SEAT_IMAGE_JSON:-$(dirname "$SEAT_IMAGE")/seat-image.json}
OUT_DIR=${OUT_DIR:-$GITHUB_WORKSPACE/desktop-smoke-out}
[ -f "$SEAT_IMAGE" ] && [ ! -L "$SEAT_IMAGE" ] || fail 'seat image missing or a symlink'
[ -f "$SEAT_IMAGE_JSON" ] || fail 'seat-image.json missing'
command -v jq >/dev/null 2>&1 || fail 'jq required'
command -v "$INCUS_BIN" >/dev/null 2>&1 || fail "incus client not found: $INCUS_BIN"
command -v "$INCUSD_BIN" >/dev/null 2>&1 || [ -x "$INCUSD_BIN" ] || fail "incusd not found: $INCUSD_BIN"
if command -v pgrep >/dev/null 2>&1 && pgrep -x incusd >/dev/null 2>&1; then
  fail 'another Incus daemon is already running on the disposable runner'
fi

want_sha=$(jq -r .image_sha256 "$SEAT_IMAGE_JSON")
got_sha=$(sha256sum "$SEAT_IMAGE" | cut -d' ' -f1)
[ "$want_sha" = "$got_sha" ] || fail "image sha256 $got_sha does not match seat-image.json ($want_sha)"

CT=polyseat-desktop-cpu
ALIAS=polyseat-desktop
mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)

if [ -z "${INCUS_DIR:-}" ]; then
  INCUS_DIR=$(mktemp -d /tmp/polyseat-incus-desktop.XXXXXXXX)
  chmod 0711 "$INCUS_DIR"
fi
export INCUS_DIR
export INCUS_CONF="$OUT_DIR/incus-client-conf"
# The pinned liblxc is built without AppArmor (like the Unraid target, which has no LSM); this disposable runner has it.
export INCUS_SECURITY_APPARMOR=false
export INCUS_SKIP_INSTANCE_TYPES=true
mkdir -p "$INCUS_CONF"

# GitHub-hosted Ubuntu runners need not allocate subordinate IDs to root, which an unprivileged container needs.
# Modifies /etc/subuid and /etc/subgid on the *throwaway CI VM only*, after the strict guard above.
ensure_root_subids() {
  subid_file=$1
  usermod_flag=$2
  [ -f "$subid_file" ] || fail "missing CI subordinate-ID file: $subid_file"
  command -v usermod >/dev/null 2>&1 || fail 'CI runner missing usermod'
  if awk -F: '$1 == "root" && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $3 >= 65536 { found=1 } END { exit !found }' "$subid_file"; then
    return
  fi
  if awk -F: '$2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $3 > 0 && $2 <= 1655359 && ($2 + $3 - 1) >= 1000000 { found=1 } END { exit !found }' "$subid_file"; then
    fail "CI subordinate-ID range 1000000-1655359 already allocated in $subid_file"
  fi
  usermod "$usermod_flag" 1000000-1655359 root
}
command -v newuidmap >/dev/null 2>&1 || fail 'CI runner missing newuidmap'
ensure_root_subids /etc/subuid --add-subuids
ensure_root_subids /etc/subgid --add-subgids

if [ -n "${LXC_ROOTFS_MOUNT:-}" ]; then
  (umask 022; mkdir -p "$LXC_ROOTFS_MOUNT")
fi

daemon_pid=
sampler_pid=
cleanup() {
  [ -z "$sampler_pid" ] || kill "$sampler_pid" 2>/dev/null || true
  if [ -S "$INCUS_DIR/unix.socket" ]; then
    "$INCUS_BIN" delete "$CT" --force >/dev/null 2>&1 || true
    "$INCUS_BIN" image delete "$ALIAS" >/dev/null 2>&1 || true
  fi
  if [ -n "$daemon_pid" ] && kill -0 "$daemon_pid" 2>/dev/null; then
    kill -TERM "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- host used-memory sampler (whole-runner delta, includes incusd, lxc monitor, the container and the pool cache) ----
mem_used_kb() { awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {print t-a}' /proc/meminfo; }
baseline_kb=$(mem_used_kb)
(
  while :; do
    mem_used_kb >> "$OUT_DIR/host-mem-used-kb.samples"
    sleep 1
  done
) &
sampler_pid=$!

# --- daemon: own scope (so its cgroup memory.peak is separate), dir pool, NO network ------------------------------------
printf '%s\n' 'Desktop smoke: starting Incus on a disposable runner only.'
scope=polyseat-ci-incusd
if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  (umask 022; exec systemd-run --scope --quiet --unit="$scope" -p MemoryAccounting=yes -p TasksAccounting=yes -- \
    "$INCUSD_BIN" --debug --logfile "$OUT_DIR/incusd.log" >/dev/null 2>&1) &
else
  scope=
  (umask 022; exec "$INCUSD_BIN" --debug --logfile "$OUT_DIR/incusd.log" >/dev/null 2>&1) &
fi
daemon_pid=$!
ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  if [ -S "$INCUS_DIR/unix.socket" ] && "$INCUS_BIN" info >/dev/null 2>&1; then
    ready=true
    break
  fi
  kill -0 "$daemon_pid" 2>/dev/null || break
  attempt=$((attempt + 1))
  sleep 1
done
if [ "$ready" != true ]; then
  sed -n '1,120p' "$OUT_DIR/incusd.log" >&2 || true
  fail 'Incus daemon not healthy within 60 seconds'
fi

# NEVER `incus admin init --minimal` (it creates incusbr0). Explicit preseed: dir pool, disk-only default profile.
printf '%s\n' 'config: {}' 'storage_pools:' '  - name: default' '    driver: dir' 'profiles:' '  - name: default' \
  '    devices:' '      root:' '        type: disk' '        path: /' '        pool: default' > "$OUT_DIR/preseed.yaml"
"$INCUS_BIN" admin init --preseed < "$OUT_DIR/preseed.yaml"
"$INCUS_BIN" profile show default > "$OUT_DIR/default-profile.txt"
if grep -Eq '(eth0:|type: nic|network:)' "$OUT_DIR/default-profile.txt"; then
  fail 'default profile unexpectedly contains network configuration'
fi
[ ! -e /sys/class/net/incusbr0 ] || fail 'unexpected Incus bridge'

# --- helpers --------------------------------------------------------------------------------------------------------
cg_dir() { # name fragment -> cgroup v2 directory (empty if none)
  find /sys/fs/cgroup -maxdepth 3 -type d -name "*$1*" 2>/dev/null | head -n 1
}
read_cg() { # dir file
  if [ -n "$1" ] && [ -r "$1/$2" ]; then cat "$1/$2"; else echo null; fi
}
cpu_usec() { # dir
  if [ -n "$1" ] && [ -r "$1/cpu.stat" ]; then awk '$1 == "usage_usec" {print $2}' "$1/cpu.stat"; else echo null; fi
}

measure() { # label: container cgroup peaks, read while the container still runs
  dir=$(cg_dir "$CT")
  jq -n --arg label "$1" --arg dir "$dir" --argjson mem_peak "$(read_cg "$dir" memory.peak)" \
    --argjson mem_cur "$(read_cg "$dir" memory.current)" --argjson pids_peak "$(read_cg "$dir" pids.peak)" \
    --argjson pids_cur "$(read_cg "$dir" pids.current)" --argjson cpu_usec "$(cpu_usec "$dir")" \
    '{label: $label, cgroup: $dir, memory_peak_bytes: $mem_peak, memory_current_bytes: $mem_cur,
      pids_peak: $pids_peak, pids_current: $pids_cur, cpu_usage_usec: $cpu_usec}' > "$OUT_DIR/measure-$1.json"
  [ -n "$dir" ] || fail "no cgroup found for $CT; cannot measure"
}

verify_result() { # json_file screenshot_file mode profile
  jq -e --arg mode "$3" --arg profile "$4" \
    '.schema == 1 and .mode == $mode and .profile == $profile and .verdict == "PASS" and (.checks | length) > 0' \
    "$1" >/dev/null || return 1
  [ "$(head -c 8 "$2" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ] || return 1
  [ "$(sha256sum "$2" | cut -d' ' -f1)" = "$(jq -r .evidence.screenshot.sha256 "$1")" ] || return 1
}

fail_with_logs() {
  printf 'DESKTOP SMOKE FAILED: %s\n' "$1" >&2
  "$INCUS_BIN" info "$CT" --show-log > "$OUT_DIR/incus-info-$CT.txt" 2>&1 || true
  [ ! -r "$INCUS_DIR/logs/$CT/lxc.log" ] || cp "$INCUS_DIR/logs/$CT/lxc.log" "$OUT_DIR/lxc.log" || true
  "$INCUS_BIN" exec "$CT" -- journalctl -b --no-pager -n 400 > "$OUT_DIR/container-journal.txt" 2>&1 || true
  "$INCUS_BIN" exec "$CT" -- sh -c 'cat /var/lib/polyseat-selftest/result.json 2>/dev/null' > "$OUT_DIR/result-last.json" 2>&1 || true
  exit 1
}

# One start / selftest / pull / stop cycle. These are the ONLY incus calls made against the container.
run_cycle() { # n
  "$INCUS_BIN" start "$CT"
  rc=0
  "$INCUS_BIN" exec "$CT" -- /usr/local/bin/polyseat-selftest desktop \
    > "$OUT_DIR/result-$1.json" 2> "$OUT_DIR/selftest-$1.stderr" || rc=$?
  "$INCUS_BIN" file pull "$CT/var/lib/polyseat-selftest/screenshot.png" "$OUT_DIR/screenshot-$1.png" || true
  "$INCUS_BIN" file pull "$CT/var/lib/polyseat-selftest/sunshine.log" "$OUT_DIR/sunshine-$1.log" || true
  measure "cycle-$1"
  [ "$rc" -eq 0 ] || fail_with_logs "polyseat-selftest desktop exited $rc in cycle $1"
  verify_result "$OUT_DIR/result-$1.json" "$OUT_DIR/screenshot-$1.png" desktop cpu || fail_with_logs "cycle $1 result did not verify"
}

stop_cycle() { # n
  started=$(date +%s)
  "$INCUS_BIN" stop "$CT" --timeout 60 || fail_with_logs "graceful stop failed in cycle $1"
  printf '%s\n' "$(( $(date +%s) - started ))" > "$OUT_DIR/stop-seconds-$1.txt"
}

# --- the fixed sequence ---------------------------------------------------------------------------------------------
"$INCUS_BIN" image import "$SEAT_IMAGE" --alias "$ALIAS"
"$INCUS_BIN" init "$ALIAS" "$CT" \
  -c boot.autostart=false -c security.privileged=false -c security.nesting=false \
  -c limits.cpu=2 -c limits.memory=2GiB -c limits.processes=1024
"$INCUS_BIN" config show "$CT" --expanded > "$OUT_DIR/instance-config.txt"
if grep -Eq '(type: nic|type: gpu|type: unix-char|type: proxy)' "$OUT_DIR/instance-config.txt"; then
  fail 'instance has a device other than the root disk'
fi

run_cycle 1
stop_cycle 1

# Rootfs as stored by the pool (the Unraid pool will live on tmpfs): precise sizes while the container is stopped.
rootfs_dir="$INCUS_DIR/storage-pools/default/containers/$CT"
if [ -d "$rootfs_dir/rootfs" ]; then
  du -sb --apparent-size "$rootfs_dir/rootfs" | cut -f1 > "$OUT_DIR/rootfs-apparent-bytes.txt"
  du -sk "$rootfs_dir/rootfs" | cut -f1 > "$OUT_DIR/rootfs-allocated-kib.txt"
fi

run_cycle 2
# CI-only negative check, after the desktop cycle: with no GPU the NVIDIA self-test must fail cleanly, changing nothing.
rc=0
"$INCUS_BIN" exec "$CT" -- /usr/local/bin/polyseat-selftest nvidia \
  > "$OUT_DIR/result-nvidia-negative.json" 2> "$OUT_DIR/selftest-nvidia-negative.stderr" || rc=$?
nvidia_ok=false
if [ "$rc" -ne 0 ] && jq -e '.mode == "nvidia" and .verdict == "FAIL" and (.reason | test("nvidia"))' \
  "$OUT_DIR/result-nvidia-negative.json" >/dev/null; then
  nvidia_ok=true
fi
[ "$nvidia_ok" = true ] || fail_with_logs 'nvidia self-test did not fail cleanly without a GPU'
stop_cycle 2

"$INCUS_BIN" delete "$CT"
"$INCUS_BIN" image delete "$ALIAS"

# --- whole-tree numbers, then leftovers -------------------------------------------------------------------------------
scope_dir=
[ -z "$scope" ] || scope_dir=$(cg_dir "$scope.scope")
peak_used_kb=$(sort -n "$OUT_DIR/host-mem-used-kb.samples" | tail -n 1)
jq -n --arg scope "$scope_dir" --argjson incusd_peak "$(read_cg "$scope_dir" memory.peak)" \
  --argjson incusd_pids_peak "$(read_cg "$scope_dir" pids.peak)" \
  --argjson baseline_kb "$baseline_kb" --argjson peak_kb "$peak_used_kb" \
  --argjson c1 "$(cat "$OUT_DIR/measure-cycle-1.json")" --argjson c2 "$(cat "$OUT_DIR/measure-cycle-2.json")" \
  --argjson apparent "$(cat "$OUT_DIR/rootfs-apparent-bytes.txt" 2>/dev/null || echo null)" \
  --argjson allocated_kib "$(cat "$OUT_DIR/rootfs-allocated-kib.txt" 2>/dev/null || echo null)" \
  --argjson stop1 "$(cat "$OUT_DIR/stop-seconds-1.txt")" --argjson stop2 "$(cat "$OUT_DIR/stop-seconds-2.txt")" \
  '{container: [$c1, $c2],
    incusd_scope: {cgroup: $scope, memory_peak_bytes: $incusd_peak, pids_peak: $incusd_pids_peak},
    host_used_memory_delta_peak_bytes: (($peak_kb - $baseline_kb) * 1024),
    tree_memory_peak_upper_bound_bytes: (([$c1.memory_peak_bytes, $c2.memory_peak_bytes] | max) + ($incusd_peak // 0)),
    rootfs_apparent_bytes: $apparent, rootfs_allocated_bytes: (if $allocated_kib then $allocated_kib * 1024 else null end),
    graceful_stop_seconds: [$stop1, $stop2]}' > "$OUT_DIR/measurements.json"

kill "$sampler_pid" 2>/dev/null || true
sampler_pid=
if [ -S "$INCUS_DIR/unix.socket" ]; then
  [ -z "$("$INCUS_BIN" list --format csv)" ] || fail_with_logs 'instances left behind'
fi
kill -TERM "$daemon_pid" 2>/dev/null || true
wait "$daemon_pid" 2>/dev/null || true
daemon_pid=
attempt=0
while [ "$attempt" -lt 20 ]; do
  pgrep -x incusd >/dev/null 2>&1 || pgrep -f 'lxc monitor' >/dev/null 2>&1 || break
  attempt=$((attempt + 1))
  sleep 1
done
leftover=$(ps -eo uid=,pid=,args= | awk '$1 >= 1000000 {print}')
if [ -n "$leftover" ] || pgrep -x incusd >/dev/null 2>&1 || [ -e /sys/class/net/incusbr0 ] || [ -n "$(cg_dir "$CT")" ]; then
  printf '%s\n' "$leftover" > "$OUT_DIR/leftover.txt"
  fail_with_logs 'processes, bridge or cgroup left behind after cleanup'
fi
printf '%s\n' 'PASS: desktop seat booted twice, headless sway + software Sunshine proven, stopped gracefully, no leftovers.'
