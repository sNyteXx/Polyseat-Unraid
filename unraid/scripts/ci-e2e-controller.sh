#!/bin/sh
# WIP / DO NOT USE ON UNRAID. CPU-only end-to-end run of polyseatctl with the real, CI-built
# runtime bundle and a real incusd. Disposable GitHub-hosted Linux runner ONLY, as root.
# Signing uses a throwaway key generated here; this proves behaviour, not release trust.
set -eu

fail() {
  printf 'E2E BLOCKED/FAILED: %s\n' "$1" >&2
  exit 2
}
step() { printf '\n== %s\n' "$1"; if [ -n "${steps_file:-}" ]; then printf '%s\n' "$1" >> "$steps_file"; fi; }

if [ "${CI:-}" != true ] || [ "${GITHUB_ACTIONS:-}" != true ] ||
   [ "${RUNNER_OS:-}" != Linux ] || [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ] ||
   [ -e /etc/unraid-version ]; then
  fail 'GitHub-hosted disposable Linux runner required; Unraid forbidden'
fi
[ "$(id -u)" -eq 0 ] || fail 'root required (disposable runner only)'
: "${GITHUB_WORKSPACE:?}"
: "${INCUS_PROOF_STAGE:?}"
proof="$GITHUB_WORKSPACE/incus-build-proof"
ctl_src="$GITHUB_WORKSPACE/unraid/plugin/src/emhttp/scripts/polyseatctl"
steps_file="$proof/e2e-steps.txt"
rm -f "$steps_file" "$proof/e2e-result.txt"
importer="$INCUS_PROOF_STAGE/incus/test/deps/import-busybox"
if [ ! -f "$proof/bundle.env" ] || [ ! -x "$ctl_src" ] || [ ! -r "$importer" ]; then fail 'build outputs missing'; fi
if pgrep -x incusd >/dev/null 2>&1; then fail 'an incusd already runs on this runner'; fi

root=$(mktemp -d /tmp/polyseat-ctl-XXXXXXXX)
case "$root" in /tmp/polyseat-ctl-*) ;; *) fail 'unexpected sandbox path' ;; esac
chmod 0711 "$root"
work="$root/work"
mkdir -p "$work" "$root/etc" "$root/mnt/cache/appdata/polyseat" \
  "$root/boot/config/plugins/polyseat-unraid"
chmod 0755 "$root/mnt" "$root/mnt/cache" "$root/mnt/cache/appdata" "$root/mnt/cache/appdata/polyseat"
printf 'version="7.3.3"\n' > "$root/etc/unraid-version"
data="$root/mnt/cache/appdata/polyseat"
flash="$root/boot/config/plugins/polyseat-unraid"
write_cfg() { # service-setting accept-no-lsm
  printf 'SERVICE="%s"\nDATA_DIR="/mnt/cache/appdata/polyseat"\nHEALTH_TIMEOUT="90"\nACCEPT_NO_LSM="%s"\n' \
    "$1" "${2:-no}" > "$flash/polyseat-unraid.cfg"
}
write_cfg disabled
cur="$root/usr/local/polyseat-unraid/current"
incus_version=$(sed -n 's/^INCUS_RELEASE=v//p' "$proof/build-info.txt")
min_glibc=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["glibc_max"])' "$proof/abi-report.json")

ssh-keygen -q -t ed25519 -N '' -f "$work/key"
printf 'polyseat-unraid-release namespaces="polyseat-unraid-bundle" %s\n' \
  "$(awk '{print $1, $2}' "$work/key.pub")" > "$flash/allowed_signers"

ctl() {
  env POLYSEAT_ROOT="$root" POLYSEAT_CI_REAL_SYSTEM=1 CI="$CI" GITHUB_ACTIONS="$GITHUB_ACTIONS" \
    RUNNER_ENVIRONMENT="$RUNNER_ENVIRONMENT" "$@"
}
polyseatctl() { ctl "$ctl_src" "$@"; }
polyseatctl_crash() { crash_at=$1; shift; ctl env POLYSEAT_CRASH_AT="$crash_at" "$ctl_src" "$@"; }
incus() {
  env INCUS_DIR="$data/incus" INCUS_CONF="$root/var/run/polyseat-unraid/client" \
    PATH="$cur/payload/bin:$PATH" "$cur/payload/bin/incus" "$@"
}
ensure_running() {
  n=0
  while [ "$n" -lt 30 ]; do
    [ "$(incus list -f csv -c s polyseat-ci-cpu)" = RUNNING ] && return 0
    n=$((n + 1))
    sleep 1
  done
  printf 'container was not restored by Incus; starting it explicitly\n'
  incus start polyseat-ci-cpu
}
must_exec() {
  incus exec polyseat-ci-cpu -- /bin/busybox echo "$1" | grep -qx "$1" || fail "container exec failed: $1"
}
status_field() { polyseatctl status | sed -n "s/^$1=//p"; }
expect_status() { # state version
  [ "$(status_field state)" = "$1" ] || fail "expected state $1, got $(status_field state)"
  [ "$(status_field version)" = "$2" ] || fail "expected version $2, got $(status_field version)"
}

# Build a signed variant bundle from the CI payload; sets VA (archive), VM (manifest), VS (signature).
variant() { # name version modifier-function
  v_name=$1 v_version=$2 v_modifier=$3
  v_payload="$work/payload-$v_name"
  mkdir -p "$v_payload" "$work/out-$v_name"
  cp -a "$proof/payload/." "$v_payload/"
  "$v_modifier" "$v_payload"
  v_paths=$(python3 "$GITHUB_WORKSPACE/unraid/scripts/make-runtime-bundle.py" \
    --payload "$v_payload" --out "$work/out-$v_name" --version "$v_version" \
    --incus-version "$incus_version" --min-glibc "$min_glibc" --abi-report "$proof/abi-report.json")
  VA=$(printf '%s\n' "$v_paths" | sed -n 1p)
  VM=$(printf '%s\n' "$v_paths" | sed -n 2p)
  ssh-keygen -q -Y sign -f "$work/key" -n polyseat-unraid-bundle "$VM"
  VS="$VM.sig"
}
mod_none() { :; }
mod_v2() { mkdir -p "$1/share"; printf 'v2\n' > "$1/share/marker.txt"; }
mod_v4() { mkdir -p "$1/share"; printf 'v4\n' > "$1/share/marker.txt"; }
mod_v5() { mkdir -p "$1/share"; printf 'v5\n' > "$1/share/marker.txt"; }
# shellcheck disable=SC2016  # the wrapper script must expand $1/$0 when it runs
mod_failing() {
  mv "$1/bin/incusd" "$1/bin/incusd.real"
  printf '%s\n' '#!/bin/sh' \
    'if [ "${1:-}" = --version ]; then exec "$(dirname "$0")/incusd.real" --version; fi' \
    'echo "simulated start failure" >&2' 'exit 1' > "$1/bin/incusd"
  chmod 0755 "$1/bin/incusd"
}

# Host-state evidence for the production-canary plan: what does a CPU-only Incus run change on the host?
sysctl_keys='net.ipv4.ip_forward net.ipv6.conf.all.forwarding net.bridge.bridge-nf-call-iptables net.core.bpf_jit_enable
  user.max_user_namespaces fs.inotify.max_user_instances fs.inotify.max_user_watches kernel.keys.maxkeys kernel.pid_max
  vm.max_map_count vm.overcommit_memory'
host_state() { # label
  d="$work/hs.$1"
  mkdir -p "$d"
  lsmod | awk 'NR>1 {print $1}' | sort > "$d/modules"
  { iptables-save 2>/dev/null || true; } | grep -v '^[#:]' | sort > "$d/iptables"
  { ip6tables-save 2>/dev/null || true; } | grep -v '^[#:]' | sort > "$d/ip6tables"
  { nft list ruleset 2>/dev/null || true; } > "$d/nft"
  for k in $sysctl_keys; do printf '%s=%s\n' "$k" "$(sysctl -n "$k" 2>/dev/null || echo n/a)"; done > "$d/sysctl"
  ip -br addr | awk '{print $1,$3}' | sort > "$d/addresses"
  ss -ltnH 2>/dev/null | awk '{print $4}' | sort > "$d/listeners"
  grep -vE ' (/tmp/polyseat-ctl-|/proc/|/sys/)' /proc/mounts | awk '{print $2,$3}' | sort > "$d/mounts"
  find /sys/fs/cgroup -mindepth 1 -maxdepth 1 -printf '%f\n' | sort > "$d/cgroup-top"
}
host_state before
ip -br link | awk '{print $1}' | sort > "$work/links.before"
cat /etc/subuid /etc/subgid 2>/dev/null | sha256sum > "$work/subid.before"
mkdir -p "$data/seats" "$data/games"
printf 'seat-config\n' > "$data/seats/seat1.cfg"
head -c 4096 /dev/urandom > "$data/games/save.dat"
(cd "$data" && sha256sum seats/seat1.cfg games/save.dat > "$work/user-data.sha256")

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    if [ -s "$work/diff.nft" ]; then
      printf '%s\n' '--- nftables host-state difference (first 120 lines, disposable CI runner)' >&2
      sed -n '1,120p' "$work/diff.nft" >&2
    fi
    printf '\n== FAILURE DIAGNOSTICS (disposable runner)\n' >&2
    incus info --show-log polyseat-ci-cpu >&2 2>/dev/null || true
    for f in "$data"/incus/logs/polyseat-ci-cpu/lxc.log "$root"/var/log/polyseat-unraid/incusd.log \
             "$root"/var/log/polyseat-unraid/incusd.out; do
      [ -r "$f" ] && { printf -- '--- %s (tail)\n' "$f" >&2; tail -n 60 "$f" >&2; }
    done
    ls -ld /var/lib/polyseat-lxc /var/lib/polyseat-lxc/rootfs >&2 2>/dev/null || true
  fi
  polyseatctl stop >/dev/null 2>&1 || true
  mkdir -p "$proof/e2e-logs"
  cp "$root"/var/log/polyseat-unraid/* "$proof/e2e-logs/" 2>/dev/null || true
  # The controller writes private (0600, root) logs; the CI user must be able to read and upload the copies.
  chmod a+rX "$proof/e2e-logs" 2>/dev/null || true
  chmod a+r "$proof"/e2e-logs/* "$proof"/e2e-steps.txt "$proof"/e2e-result.txt "$proof"/host-state-diff.txt 2>/dev/null || true
}
trap cleanup EXIT

step '1. install first bundle (update from empty), service stays down'
v1_version="$incus_version-polyseat.ci1"
variant v1 "$v1_version" mod_none
polyseatctl update "$VA" "$VM" "$VS" --approve
expect_status ACTIVE "$v1_version"
polyseatctl verify

step '2. LSM gate, then start without systemd and check health'
# The runner has AppArmor active, the bundled liblxc has none (like the target Unraid, which has
# no LSM at all). The controller must refuse until the operator accepts it explicitly.
if [ -d /sys/kernel/security/apparmor ]; then
  rc=0
  polyseatctl start || rc=$?
  [ "$rc" -ne 0 ] || fail 'start was allowed although AppArmor is active and unsupported by the bundle'
  if pgrep -x incusd >/dev/null 2>&1; then fail 'daemon started despite the LSM gate'; fi
  printf 'LSM gate refused the start as designed; accepting the disposable-runner state explicitly\n'
else
  printf 'AppArmor not active on this runner; the gate cannot be exercised here\n'
fi
write_cfg disabled yes
polyseatctl start
polyseatctl health

step '3. disk-only preseed, tiny CPU-only container, exec'
printf '%s\n' 'config: {}' 'storage_pools:' '  - name: default' '    driver: dir' 'profiles:' \
  '  - name: default' '    devices:' '      root:' '        type: disk' '        path: /' \
  '        pool: default' > "$work/preseed.yaml"
incus admin init --preseed < "$work/preseed.yaml"
if incus profile show default | grep -Eq '(eth0:|type: nic|network:)'; then fail 'unexpected NIC in profile'; fi
PATH="$cur/payload/bin:$PATH" INCUS_DIR="$data/incus" INCUS_CONF="$root/var/run/polyseat-unraid/client" \
  python3 "$importer" --alias polyseat-ci-busybox
incus init polyseat-ci-busybox polyseat-ci-cpu --storage default
incus start polyseat-ci-cpu
must_exec E2E_FIRST_START

step '4. stop and restart the container'
incus stop polyseat-ci-cpu --force --timeout 15
incus start polyseat-ci-cpu
must_exec E2E_SECOND_START

step '5. service restart keeps the container running'
old_pid=$(cat "$root/var/run/polyseat-unraid/incusd.pid")
polyseatctl restart
[ "$old_pid" != "$(cat "$root/var/run/polyseat-unraid/incusd.pid")" ] || fail 'daemon pid did not change'
polyseatctl health
must_exec E2E_AFTER_SERVICE_RESTART

step '6. update to v2 while running'
v2_version="$incus_version-polyseat.ci2"
variant v2 "$v2_version" mod_v2
polyseatctl update "$VA" "$VM" "$VS" --approve
expect_status RUNNING "$v2_version"
must_exec E2E_AFTER_UPDATE

step '7. failing update rolls back automatically'
variant v3 "$incus_version-polyseat.ci3" mod_failing
rc=0
polyseatctl update "$VA" "$VM" "$VS" --approve || rc=$?
[ "$rc" -eq 4 ] || fail "expected exit 4 from a failed activation, got $rc"
expect_status RUNNING "$v2_version"
must_exec E2E_AFTER_ROLLBACK

step '8. interrupted update, then recovery'
variant v4 "$incus_version-polyseat.ci4" mod_v4
rc=0
polyseatctl_crash after_switch update "$VA" "$VM" "$VS" --approve || rc=$?
[ "$rc" -eq 99 ] || fail "expected simulated crash exit 99, got $rc"
[ "$(status_field state)" = JOURNAL_PENDING ] || fail 'journal not pending after crash'
polyseatctl recover --approve
expect_status RUNNING "$v2_version"
must_exec E2E_AFTER_RECOVERY

step '9. update then manual rollback'
variant v5 "$incus_version-polyseat.ci5" mod_v5
polyseatctl update "$VA" "$VM" "$VS" --approve
polyseatctl rollback --approve
expect_status RUNNING "$v2_version"
must_exec E2E_AFTER_MANUAL_ROLLBACK

step '10. graceful stop stops instances; Incus restores their last power state when the daemon returns'
container_pid=$(incus info polyseat-ci-cpu | sed -n 's/^PID: //p')
[ -n "$container_pid" ] || fail 'could not read the container PID'
polyseatctl stop
if pgrep -x incusd >/dev/null 2>&1; then fail 'incusd (daemon or helper) still running after stop'; fi
[ ! -e "/proc/$container_pid" ] || fail 'container process survived the service stop'
polyseatctl start
# Incus restarts instances that were running at shutdown (their recorded power state). That is the
# wanted behaviour for an array stop/start; accept it, otherwise start the container explicitly.
ensure_running
must_exec E2E_AFTER_FULL_STOP

step '11. simulated reboot: RAM state gone, boot rebuilds from flash'
polyseatctl stop
case "$root" in /tmp/polyseat-ctl-*) rm -rf "$root/usr/local/polyseat-unraid" "$root/var/run/polyseat-unraid" ;; esac
write_cfg enabled yes
polyseatctl boot
expect_status RUNNING "$v2_version"
ensure_running
must_exec E2E_AFTER_BOOT

step '12. uninstall keeps user data'
incus delete polyseat-ci-cpu --force
polyseatctl uninstall --approve
[ "$(status_field state)" = ABSENT ] || fail 'state is not ABSENT after uninstall'

step '13. system and data state comparison'
if pgrep -x incusd >/dev/null 2>&1; then fail 'incusd left running'; fi
[ ! -e /sys/class/net/incusbr0 ] || fail 'an Incus bridge appeared'
host_state after
: > "$proof/host-state-diff.txt"
for item in modules iptables ip6tables nft sysctl addresses listeners mounts cgroup-top; do
  if ! diff -u "$work/hs.before/$item" "$work/hs.after/$item" > "$work/diff.$item"; then
    printf '== CHANGED: %s\n' "$item" >> "$proof/host-state-diff.txt"
    cat "$work/diff.$item" >> "$proof/host-state-diff.txt"
  fi
done
[ -s "$proof/host-state-diff.txt" ] || printf 'no host-state change in modules, netfilter, sysctls, addresses, listeners, mounts or cgroup top level\n' > "$proof/host-state-diff.txt"
# Netfilter rules and sysctls must be untouched by a disk-only container workload. Modules, addresses, listeners,
# mounts and top-level cgroups are recorded as evidence (a shared runner has unrelated churn there).
for item in iptables ip6tables nft sysctl; do
  diff -q "$work/hs.before/$item" "$work/hs.after/$item" >/dev/null || fail "host state changed: $item (see host-state-diff.txt)"
done
ip -br link | awk '{print $1}' | sort > "$work/links.after"
diff "$work/links.before" "$work/links.after" || fail 'network links changed'
cat /etc/subuid /etc/subgid 2>/dev/null | sha256sum > "$work/subid.after"
cmp "$work/subid.before" "$work/subid.after" || fail '/etc/subuid or /etc/subgid not restored exactly'
(cd "$data" && sha256sum --check --quiet "$work/user-data.sha256") || fail 'user data changed'
[ -d "$data/incus" ] || fail 'Incus state directory was unexpectedly removed'
[ -f "$flash/polyseat-unraid.cfg" ] || fail 'settings were removed'
if [ -e "$root/usr/local/polyseat-unraid" ]; then fail 'runtime directory still present'; fi
if [ -e /var/lib/polyseat-lxc ]; then fail 'LXC state directory was not removed by uninstall'; fi

printf '%s\n' 'PASS: controller end-to-end on real incusd (CPU-only, disposable runner; NOT Unraid evidence).' |
  tee "$proof/e2e-result.txt"
