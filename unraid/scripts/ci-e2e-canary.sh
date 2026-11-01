#!/bin/bash
# WIP. Real, signed gate=canary rehearsal on disposable GitHub-hosted runners ONLY.
# A passing result is Linux evidence, never Unraid approval. Keys are throwaway
# and separate; only CI CPU topology and unique cgroup names are rendered below.
set -euo pipefail
umask 077

fail() { printf 'CANARY E2E BLOCKED/FAILED: %s\n' "$*" >&2; exit 2; }
[[ ${CI:-} == true && ${GITHUB_ACTIONS:-} == true &&
   ${RUNNER_OS:-} == Linux && ${RUNNER_ENVIRONMENT:-} == github-hosted &&
   ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ! -e /etc/unraid-version ]] ||
  fail 'disposable GitHub-hosted Linux runner required; Unraid forbidden'
[[ $(uname -r) != *[Uu]nraid* && $(id -u) == 0 ]] ||
  fail 'root on disposable Linux required; Unraid kernel forbidden'
[[ ${GITHUB_WORKSPACE:-} == /home/runner/work/* &&
   ${RUNNER_TEMP:-} == /home/runner/work/_temp &&
   ${INCUS_PROOF_STAGE:-} == "$RUNNER_TEMP"/polyseat-incus-proof.* ]] ||
  fail 'actual hosted-runner workspace and build-stage paths required'
[[ -d $GITHUB_WORKSPACE && ! -L $GITHUB_WORKSPACE &&
   -d $INCUS_PROOF_STAGE && ! -L $INCUS_PROOF_STAGE ]] || fail 'unsafe runner paths'
for tool in bash python3 jq git ssh-keygen sha256sum tar findmnt mount umount mountpoint \
            ip ss iptables-save ip6tables-save nft pgrep stat readlink timeout strace; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing CI prerequisite: $tool"
done
[[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]] || fail 'unified cgroup v2 required'
if pgrep -x incusd >/dev/null; then fail 'another Incus daemon already exists'; fi

proof="$GITHUB_WORKSPACE/incus-build-proof"
source_ctl="$GITHUB_WORKSPACE/unraid/plugin/src/emhttp/scripts/polyseatctl"
# shellcheck source=unraid/scripts/lib/ci-runner-noise.sh
source "$GITHUB_WORKSPACE/unraid/scripts/lib/ci-runner-noise.sh"
[[ -d $proof && ! -L $proof && -f $proof/bundle.env && ! -L $proof/bundle.env &&
   -f $source_ctl && ! -L $source_ctl ]] || fail 'real build outputs missing'
grep -q '^POLYSEAT_RELEASE_GATE="closed"' "$source_ctl" || fail 'committed release gate is not closed'
for output in canary-e2e-result.txt canary-rehearsal.json canary-controller.txz; do
  [[ ! -e $proof/$output && ! -L $proof/$output ]] || fail "stale evidence exists: $output"
done
archive_sha=$(sed -n 's/^BUNDLE_SHA256=//p' "$proof/bundle.env")
[[ $archive_sha =~ ^[0-9a-f]{64}$ ]] || fail 'invalid bundle identity'
runtime_archive="$proof/bundle/$archive_sha.tar.gz"
runtime_manifest="$proof/bundle/$archive_sha.manifest.json"
[[ -f $runtime_archive && ! -L $runtime_archive && -f $runtime_manifest && ! -L $runtime_manifest ]] ||
  fail 'original build archive/manifest missing'
[[ $(sha256sum "$runtime_archive" | awk '{print $1}') == "$archive_sha" ]] || fail 'runtime archive hash mismatch'
image="$proof/payload/share/canary/busybox.tar.gz"
[[ -f $image && ! -L $image ]] || fail 'fixed BusyBox image missing from real payload'
image_sha=$(jq -er '.files[] | select(.path=="payload/share/canary/busybox.tar.gz") | .sha256' "$runtime_manifest")
[[ $(sha256sum "$image" | awk '{print $1}') == "$image_sha" ]] || fail 'fixed image not bound to runtime manifest'

work=$(mktemp -d /tmp/polyseat-canary-e2e-XXXXXXXX)
[[ $work == /tmp/polyseat-canary-e2e-* ]] || fail 'unexpected private workdir'
evidence="$proof/canary-e2e"
mkdir -- "$evidence"
steps_file="$evidence/steps.txt"
touch "$steps_file"

# Tower-like PATH: exactly the program names recorded read-only on the Unraid host, resolved to this
# runner's binaries. Run 1 failed on Tower because the runner (unlike Unraid) provided unsquashfs.
tower_tools="$GITHUB_WORKSPACE/unraid/abi/unraid-7.3.3-x86_64.tools"
tower_path="$work/tower-path"
[[ -f $tower_tools && ! -L $tower_tools ]] || fail 'recorded Tower tool inventory missing'
mkdir -m 0755 -- "$tower_path"
while IFS= read -r name; do
  [[ -z $name || $name == \#* ]] && continue
  [[ $name =~ ^(\[|[A-Za-z0-9][A-Za-z0-9._+-]*)$ ]] || fail "invalid Tower tool name: $name"
  resolved=$( PATH=/usr/sbin:/usr/bin:/sbin:/bin; type -P -- "$name" ) || resolved=""
  [[ -z $resolved ]] || ln -s -- "$resolved" "$tower_path/$name"
done < "$tower_tools"
for absent in unsquashfs mksquashfs nft python3; do
  [[ ! -e $tower_path/$absent && ! -L $tower_path/$absent ]] || fail "Tower-like PATH provides $absent, which Unraid lacks"
done
for present in bash jq tar gzip xz sha256sum ssh-keygen flock timeout unshare setpriv setsid renice mount umount \
               ip rsync setfattr newuidmap newgidmap ldconfig getconf logger du find awk sed; do
  [[ -L $tower_path/$present ]] || fail "runner lacks a program that Tower has and the canary needs: $present"
done
printf 'tower_tools_sha256=%s\nlinked_programs=%s\n' "$(sha256sum "$tower_tools" | awk '{print $1}')" \
  "$(find "$tower_path" -mindepth 1 | wc -l)" > "$evidence/tower-path.txt"
declare -a fixture_roots=() fixture_data=() fixture_ctls=() fixture_groups=()
fixture="" ctl_path="" flash="" data="" workload="" watchdog=""
smoke_job="" controller_file_sha="" ci_controller_sha=""
passed=0

ctl() {
  env -i PATH="$tower_path" LANG=C \
    POLYSEAT_ROOT="$fixture" POLYSEAT_CI_REAL_SYSTEM=1 CI=true GITHUB_ACTIONS=true \
    RUNNER_OS=Linux RUNNER_ENVIRONMENT=github-hosted "$ctl_path" "$@"
}
step() { printf 'PASS: %s\n' "$1"; printf '%s\n' "$1" >> "$steps_file"; }
expect_blocked() { # exact approval-gate refusal, not an arbitrary command error
  local label=$1 rc=0
  shift
  ctl "$@" > "$evidence/$label.txt" 2>&1 || rc=$?
  [[ $rc == 2 ]] || fail "expected approval refusal for $label, got $rc"
}

cleanup() {
  local status=$? i group attempt
  trap - EXIT
  set +e
  if [[ -n $smoke_job ]]; then
    # A failed assertion must not wait for an in-flight smoke/lock until TTL.
    # Kill only this test's already registered workload trees, then our own
    # direct child wrapper. Never select a process by name for termination.
    for group in "${fixture_groups[@]}"; do
      [[ ! -d $group || -L $group ]] || printf '1\n' > "$group/cgroup.kill"
    done
    if [[ $(awk '/^PPid:/ {print $2}' "/proc/$smoke_job/status" 2>/dev/null) == "$$" ]]; then
      kill -TERM "$smoke_job" 2>/dev/null
    fi
    for ((attempt=0; attempt<30; attempt++)); do
      kill -0 "$smoke_job" 2>/dev/null || break
      sleep 0.1
    done
    if [[ $(awk '/^PPid:/ {print $2}' "/proc/$smoke_job/status" 2>/dev/null) == "$$" ]]; then
      kill -KILL "$smoke_job" 2>/dev/null
    fi
    wait "$smoke_job" 2>/dev/null
  fi
  for ((i=${#fixture_roots[@]}-1; i>=0; i--)); do
    fixture=${fixture_roots[$i]} ctl_path=${fixture_ctls[$i]}
    ctl stop >> "$evidence/cleanup.txt" 2>&1
    ctl uninstall --approve >> "$evidence/cleanup.txt" 2>&1
    if mountpoint -q -- "${fixture_data[$i]}"; then
      umount -- "${fixture_data[$i]}" >> "$evidence/cleanup.txt" 2>&1 || status=2
    fi
    if [[ -d $fixture/var/log/polyseat-unraid ]]; then
      mkdir -p "$evidence/logs-$i"
      find "$fixture/var/log/polyseat-unraid" -maxdepth 1 -type f \
        -exec cp -t "$evidence/logs-$i" -- {} +
    fi
  done
  # No broad kill, no shared-cgroup changes: emergency cleanup targets only the
  # exact two groups registered for each private fixture, never the runner.
  for group in "${fixture_groups[@]}"; do
    if [[ -d $group && ! -L $group ]]; then
      printf '1\n' > "$group/cgroup.kill"
      find "$group" -depth -mindepth 1 -type d -exec rmdir -- {} \;
      rmdir -- "$group" || status=2
    fi
  done
  rm -f -- "$work/release-key" "$work/release-key.pub" "$work/approval-key" "$work/approval-key.pub"
  find "$evidence" -type f -exec chmod a+r -- {} +
  find "$evidence" -type d -exec chmod a+rx -- {} +
  if [[ $status != 0 || $passed != 1 ]]; then
    # A cleanup error must invalidate even an already written success report.
    rm -f -- "$proof/canary-rehearsal.json"
    printf 'FAIL: real canary rehearsal incomplete; not Unraid evidence.\n' > "$proof/canary-e2e-result.txt"
    chmod a+r "$proof/canary-e2e-result.txt"
    exit 2
  fi
  exit 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

# Capture rule semantics including chain policies. Only volatile counters and
# timestamps are normalized; read failures are fatal, never an empty snapshot.
host_state() {
  local snapshot="$evidence/host-$1" key path vfs
  mkdir -- "$snapshot"
  iptables-save | sed -E '/^#/d; s/\[[0-9]+:[0-9]+\]/[0:0]/g' > "$snapshot/iptables"
  ip6tables-save | sed -E '/^#/d; s/\[[0-9]+:[0-9]+\]/[0:0]/g' > "$snapshot/ip6tables"
  nft --stateless list ruleset > "$snapshot/nft"
  for key in net.ipv4.ip_forward net.ipv6.conf.all.forwarding \
    net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables net.core.bpf_jit_enable \
    user.max_user_namespaces kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns \
    fs.inotify.max_user_instances fs.inotify.max_user_watches kernel.keys.maxkeys kernel.pid_max \
    vm.max_map_count vm.overcommit_memory; do
    path="/proc/sys/${key//./\/}"
    if [[ -f $path ]]; then printf '%s=%s\n' "$key" "$(< "$path")";
    else printf '%s=ABSENT\n' "$key"; fi
  done > "$snapshot/sysctl"
  vfs=$(runner_vf_names /sys/class/net)  # runner noise, see lib/ci-runner-noise.sh; recorded, not compared
  printf '%s\n' "$vfs" > "$snapshot/excluded-runner-vfs"
  ip -j address show | jq -S --argjson skip "$vfs" '[.[] | select(.ifname as $n | any($skip[]; . == $n) | not) |
    {ifname,mtu,master,link_type,address,addr_info: [.addr_info[] | {family,local,prefixlen,scope,label}]}] |
    sort_by(.ifname)' > "$snapshot/addresses"
  ip -j link show | jq -S --argjson skip "$vfs" '[.[] | select(.ifname as $n | any($skip[]; . == $n) | not) |
    {ifname,mtu,master,link_type,address}] | sort_by(.ifname)' > "$snapshot/links"
  ss -H -lnt | awk '{print "tcp",$4,$5}' | sort > "$snapshot/tcp-listeners"
  ss -H -lnu | awk '{print "udp",$4,$5}' | sort > "$snapshot/udp-listeners"
  cp /proc/self/mountinfo "$snapshot/mountinfo"
  find /sys/fs/cgroup -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$snapshot/cgroups"
  cp /sys/fs/cgroup/cgroup.subtree_control "$snapshot/cgroup-subtree-control"
  sha256sum /etc/subuid /etc/subgid > "$snapshot/subids"
  stat -c '%n %u %g %a %s' /etc/subuid /etc/subgid >> "$snapshot/subids"
  cp /etc/subuid "$snapshot/subuid"
  cp /etc/subgid "$snapshot/subgid"
  for path in /var/lib/polyseat-lxc /var/lib/polyseat-lxc/rootfs; do
    if [[ -e $path || -L $path ]]; then stat -c '%n %F %u %g %a' "$path";
    else printf '%s ABSENT\n' "$path"; fi
  done > "$snapshot/lxc-mount-paths"
  # Do this after netfilter reads, so the snapshot itself cannot look like an
  # Incus-triggered module load. Module names, not volatile reference counts.
  awk '{print $1}' /proc/modules | sort > "$snapshot/modules"
}
assert_host_state() { # active allows exact RAM mounts, groups and the managed subid block
  local label=$1 mode=$2 item mount_path group base="$evidence/host-before" now="$evidence/host-$1"
  for item in iptables ip6tables nft sysctl addresses links tcp-listeners udp-listeners modules cgroup-subtree-control; do
    diff -u "$base/$item" "$now/$item" > "$evidence/diff-$label-$item.txt" ||
      fail "unscoped host change: $item ($label)"
  done
  if [[ $mode == active ]]; then
    for item in subuid subgid; do
      if cmp -s "$base/$item" "$now/$item"; then continue; fi
      { cat "$base/$item";
        printf '%s\n' '# BEGIN polyseat-unraid (managed block; removed on uninstall)' \
          'root:1000000:1000000000' '# END polyseat-unraid';
      } | cmp -s - "$now/$item" || fail "unscoped subordinate ID change ($item $label)"
    done
    cp "$now/mountinfo" "$now/mountinfo-scoped"
    cp "$now/cgroups" "$now/cgroups-scoped"
    for mount_path in "${fixture_data[@]}"; do
      awk -v exact="$mount_path" '$5 != exact' "$now/mountinfo-scoped" > "$work/mountinfo-filtered"
      cp "$work/mountinfo-filtered" "$now/mountinfo-scoped"
    done
    for group in "${fixture_groups[@]}"; do
      awk -v exact="${group##*/}" '$0 != exact' "$now/cgroups-scoped" > "$work/cgroups-filtered"
      cp "$work/cgroups-filtered" "$now/cgroups-scoped"
    done
    diff -u "$base/mountinfo" "$now/mountinfo-scoped" > "$evidence/diff-$label-mounts.txt" ||
      fail "unscoped host mount change ($label)"
    diff -u "$base/cgroups" "$now/cgroups-scoped" > "$evidence/diff-$label-cgroups.txt" ||
      fail "unscoped root cgroup change ($label)"
  else
    for item in mountinfo cgroups subids lxc-mount-paths; do
      diff -u "$base/$item" "$now/$item" > "$evidence/diff-$label-$item.txt" ||
        fail "host state not restored: $item ($label)"
    done
  fi
}

host_state before
ssh-keygen -q -t ed25519 -N '' -f "$work/release-key"
ssh-keygen -q -t ed25519 -N '' -f "$work/approval-key"
[[ $(awk '{print $2}' "$work/release-key.pub") != "$(awk '{print $2}' "$work/approval-key.pub")" ]] ||
  fail 'runtime and approval keys must be separate'
cp "$runtime_manifest" "$work/runtime.manifest.json"
ssh-keygen -q -Y sign -f "$work/release-key" -n polyseat-unraid-bundle "$work/runtime.manifest.json"
uuid=$(< /sys/class/dmi/id/product_uuid)
[[ $uuid =~ ^[0-9A-Fa-f-]{36}$ ]] || fail 'runner UUID unavailable'
host_sha=$(printf '%s' "${uuid,,}" | sha256sum | awk '{print $1}')
unset uuid
mkdir -- "$work/package"
# The upstream daemon built earlier in this job; the canary controller pins exactly this binary.
[[ -f $proof/daemon/polyseatd && ! -L $proof/daemon/polyseatd ]] || fail 'polyseatd build missing'
POLYSEATD_SHA256=$(sha256sum "$proof/daemon/polyseatd" | awk '{print $1}')
export POLYSEATD_SHA256
python3 "$GITHUB_WORKSPACE/unraid/scripts/build-canary-package.py" \
  --version "$(date -u +%Y.%m.%d).ci$GITHUB_RUN_ID" --out "$work/package" --acknowledge-artifacts-only
mapfile -t packages < <(find "$work/package" -maxdepth 1 -type f -name '*.txz')
[[ ${#packages[@]} == 1 ]] || fail 'expected exactly one canary controller package'
controller_package=${packages[0]}
controller_sha=$(sha256sum "$controller_package" | awk '{print $1}')
cp "$controller_package" "$proof/canary-controller.txz"
chmod a+r "$proof/canary-controller.txz"
host_cpuset=$(< /sys/fs/cgroup/cpuset.cpus.effective)
[[ $host_cpuset =~ ^[0-9]+([,-][0-9]+)*$ ]] || fail 'invalid delegated CI cpuset'
ci_cpuset=${host_cpuset%%,*}
ci_cpuset=${ci_cpuset%%-*}

fixture_prepare() {
  local label=$1 original_sha
  fixture=$(mktemp -d /tmp/polyseat-ctl-canary-XXXXXXXX)
  chmod 0711 "$fixture"
  flash="$fixture/boot/config/plugins/polyseat-unraid"
  data="$fixture/var/lib/polyseat-unraid-canary"
  ctl_path="$fixture/usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"
  workload="/sys/fs/cgroup/polyseat-unraid-canary-ci-$GITHUB_RUN_ID-$label"
  watchdog="/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID-$label"
  [[ ! -e $workload && ! -L $workload && ! -e $watchdog && ! -L $watchdog ]] || fail 'pre-existing CI cgroup'
  fixture_roots+=("$fixture") fixture_data+=("$data") fixture_ctls+=("$ctl_path")
  fixture_groups+=("$workload" "$watchdog")
  mkdir -p "$flash" "$fixture/etc" "$fixture/var/lib" "$data"
  chmod 0755 "$fixture/var" "$fixture/var/lib" "$data"
  mount -t tmpfs -o size=128m,nosuid,nodev,mode=0755 tmpfs "$data"
  [[ $(findmnt -n -o FSTYPE -T "$data") == tmpfs ]] || fail 'fixture data is not RAM-backed'
  tar --extract --xz --file "$controller_package" --directory "$fixture" --no-same-owner
  original_sha=$(sha256sum "$ctl_path" | awk '{print $1}')
  [[ -z $controller_file_sha || $controller_file_sha == "$original_sha" ]] || fail 'controller package changed'
  controller_file_sha=$original_sha
  # Explicit CI-only fixture rendering. No function, auth, namespace, quota or
  # timeout substitution is permitted. Production sources remain gate=closed.
  python3 - "$ctl_path" "$host_cpuset" "$ci_cpuset" "$workload" "$watchdog" "$tower_path" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
text = path.read_text()
if len(re.findall(r'^POLYSEAT_RELEASE_GATE="canary"(?:\s+#.*)?$', text, re.M)) != 1:
    raise SystemExit('not the real canary package')
if len(sys.argv) != 7 or not re.fullmatch(r'/tmp/polyseat-canary-e2e-[A-Za-z0-9]{8}/tower-path', sys.argv[6]):
    raise SystemExit('CI SAFE_PATH must be the private Tower-like PATH directory')
for name, value in zip(('CANARY_HOST_CPUSET', 'CANARY_CPUSET', 'CANARY_CGROUP',
                        'CANARY_WATCHDOG_CGROUP', 'SAFE_PATH'), sys.argv[2:]):
    text, count = re.subn(r'^' + name + r'="[^"\n]*"', name + '="' + value + '"', text, flags=re.M)
    if count != 1:
        raise SystemExit('CI constant missing/duplicated: ' + name)
path.write_text(text)
PY
  ci_controller_sha=$(sha256sum "$ctl_path" | awk '{print $1}')
  jq -n --arg label "$label" --arg file "$ci_controller_sha" --arg cpus "$ci_cpuset" \
    --arg host_cpus "$host_cpuset" --arg workload "$workload" --arg watchdog "$watchdog" --arg safe "$tower_path" \
    '{label:$label,ci_controller_sha256:$file,overrides:{CANARY_CPUSET:$cpus,
      CANARY_HOST_CPUSET:$host_cpus,CANARY_CGROUP:$workload,CANARY_WATCHDOG_CGROUP:$watchdog,SAFE_PATH:$safe}}' \
    > "$evidence/render-$label.json"
  printf 'version="7.3.3"\n' > "$fixture/etc/unraid-version"
  printf 'SERVICE="disabled"\nDATA_DIR="/var/lib/polyseat-unraid-canary"\nACCEPT_NO_LSM="yes"\n' \
    > "$flash/polyseat-unraid.cfg"
  printf 'polyseat-unraid-release namespaces="polyseat-unraid-bundle" %s\n' \
    "$(awk '{print $1,$2}' "$work/release-key.pub")" > "$flash/allowed_signers"
  printf 'polyseat-unraid-canary-approver namespaces="polyseat-unraid-canary-approval-v1" %s\n' \
    "$(awk '{print $1,$2}' "$work/approval-key.pub")" > "$flash/canary_allowed_signers"
}
approval() { # signed TTL seconds [actions JSON]; this never signs a live-production host token
  local ttl=$1 actions=${2:-'["stage","activate","start","canary-smoke"]'} now
  [[ $ttl =~ ^[0-9]+$ && $ttl -gt 0 && $ttl -le 1800 ]] || fail 'invalid test TTL'
  now=$(date -u +%s)
  jq -n --arg host "$host_sha" --arg archive "$archive_sha" --argjson now "$now" --argjson ttl "$ttl" \
    --argjson actions "$actions" \
    '{schema:1,host_sha256:$host,archive_sha256:$archive,issued_at:$now,expires_at:($now+$ttl),
      actions:$actions}' > "$flash/canary-approval.json"
  rm -f -- "$flash/canary-approval.json.sig"
  ssh-keygen -q -Y sign -f "$work/approval-key" -n polyseat-unraid-canary-approval-v1 "$flash/canary-approval.json"
  chmod 0600 "$flash/canary-approval.json" "$flash/canary-approval.json.sig"
}
stage_activate() {
  ctl stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
  ctl activate "$archive_sha" --approve
  ctl verify
}
assert_namespace() {
  local pid=$1 ns
  [[ $pid =~ ^[0-9]+$ && -r /proc/$pid/cgroup ]] || fail 'live daemon PID missing'
  [[ $(< "/proc/$pid/cgroup") == "0::/${workload##*/}/daemon"* ]] || fail 'daemon escaped bounded subtree'
  for ns in mnt cgroup pid; do
    [[ $(readlink "/proc/$pid/ns/$ns") != "$(readlink "/proc/$$/ns/$ns")" ]] || fail "daemon shares host $ns namespace"
  done
  [[ $(< "$workload/cpuset.cpus.effective") == "$ci_cpuset" &&
     $(< "$workload/cpu.max") == '100000 1000000' &&
     $(< "$workload/memory.max") == 536870912 && $(< "$workload/memory.swap.max") == 0 &&
     $(< "$workload/pids.max") == 256 && $(< "$workload/memory.oom.group") == 1 ]] || fail 'workload quota mismatch'
  [[ -z $(< "$workload/cgroup.subtree_control") && -z $(< "$workload/daemon/cgroup.controllers") ]] ||
    fail 'LXC could delegate controllers outside the no-controller leaf'
  local watch_pid
  watch_pid=$(jq -er .pid "$fixture/var/run/polyseat-unraid/canary-session.json")
  [[ $(< "/proc/$watch_pid/cgroup") == "0::/${watchdog##*/}" &&
     $(< "$watchdog/memory.max") == 33554432 && $(< "$watchdog/pids.max") == 16 ]] ||
    fail 'independent watchdog containment missing'
  printf 'daemon_host_pid=%s watchdog_host_pid=%s workload=%s\n' "$pid" "$watch_pid" "$workload" >> "$evidence/containment.txt"
  for ns in mnt cgroup pid net; do
    printf '%s host=%s daemon=%s\n' "$ns" "$(readlink "/proc/$$/ns/$ns")" "$(readlink "/proc/$pid/ns/$ns")" >> "$evidence/containment.txt"
  done
}
observe_container() { # a missed short-lived PID is retried; wrong containment is fatal
  local line pid ns_pid cg nspid daemon_depth uid_map
  line=$(sed -n 's/.*canary instance host_pid=\([0-9][0-9]*\) namespace_pid=\([0-9][0-9]*\).*/\1 \2/p' "$evidence/smoke.txt" | tail -n 1)
  [[ -n $line ]] || return 1
  read -r pid ns_pid <<< "$line"
  [[ -r /proc/$pid/cgroup && -r /proc/$pid/status ]] || return 1
  cg=$(< "/proc/$pid/cgroup") || return 1
  # API PID belongs to the daemon's PID namespace, not the container's final
  # PID=1. Map its corresponding NSpid column through the daemon's depth.
  daemon_depth=$(awk '/^NSpid:/ {print NF-1}' "/proc/$daemon_pid/status") || return 1
  [[ $daemon_depth =~ ^[0-9]+$ && $daemon_depth -gt 1 ]] || fail 'daemon PID namespace depth missing'
  nspid=$(awk -v column="$((daemon_depth + 1))" '/^NSpid:/ {print $column}' "/proc/$pid/status") || return 1
  [[ $cg == "0::/${workload##*/}/daemon/"* && $nspid == "$ns_pid" ]] || fail 'container escaped workload subtree or PID namespace'
  uid_map=$(< "/proc/$pid/uid_map") || return 1
  awk '$1==0 && $2>=100000 && $3>0 {safe=1} END {exit !safe}' <<< "$uid_map" ||
    fail 'container is not using an unprivileged root UID mapping'
  printf 'container_host_pid=%s namespace_pid=%s cgroup=%s\n' "$pid" "$ns_pid" "$cg" >> "$evidence/containment.txt"
  return 0
}
fixture_finish() {
  ctl stop
  ctl uninstall --approve
  [[ ! -e $workload && ! -e $watchdog ]] || fail 'canary cgroups survived controller teardown'
  umount -- "$data"
}

fixture_prepare lifecycle
expect_blocked missing-approval stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
approval 600
# Wrong-purpose key cannot grant canary actions, despite a valid runtime key.
cp "$flash/canary-approval.json.sig" "$work/valid-approval.sig"
rm -- "$flash/canary-approval.json.sig"
ssh-keygen -q -Y sign -f "$work/release-key" -n polyseat-unraid-canary-approval-v1 "$flash/canary-approval.json"
expect_blocked wrong-signing-key stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
cp "$work/valid-approval.sig" "$flash/canary-approval.json.sig"
ctl stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
step signature-stage
ctl activate "$archive_sha" --approve
ctl verify
step activate
expect_blocked forbidden-restart restart
expect_blocked forbidden-boot boot
[[ ! -e $data/incus ]] || fail 'start did not begin with fresh RAM data'
approval 600
ctl start --approve
ctl health
daemon_pid=$(< "$fixture/var/run/polyseat-unraid/incusd.pid")
assert_namespace "$daemon_pid"
step prestart-policy
host_state active
assert_host_state active active

ctl canary-smoke --approve > "$evidence/smoke.txt" 2>&1 &
smoke_job=$!
observed=0
smoke_started=$SECONDS
while kill -0 "$smoke_job" 2>/dev/null; do
  if [[ $observed == 0 ]] && observe_container; then
    observed=1
    host_state container
    assert_host_state container active
  fi
  (( SECONDS - smoke_started < 300 )) || fail 'bounded real smoke timed out'
  sleep 0.05
done
smoke_rc=0
wait "$smoke_job" || smoke_rc=$?
smoke_job=""
[[ $smoke_rc == 0 && $observed == 1 ]] || fail "real smoke failed or live containment unobserved (rc=$smoke_rc observed=$observed)"
[[ $(grep -c '^POLYSEAT_CANARY_OK$' "$evidence/smoke.txt") == 2 ]] || fail 'both real container execs were not evidenced'
for phase in storage import start restart delete daemon-stop; do
  grep -q "canary phase: $phase$" "$evidence/smoke.txt" || fail "real lifecycle phase missing: $phase"
done
[[ $(grep -c 'canary phase: exec$' "$evidence/smoke.txt") == 2 &&
   $(grep -c 'canary phase: stop$' "$evidence/smoke.txt") == 2 ]] || fail 'both stop/restart cycles must be evidenced'
[[ ! -e $workload && ! -e $watchdog ]] || fail 'smoke did not hard-stop its own groups'
restart_rc=0
ctl start --approve > "$evidence/reused-state-refusal.txt" 2>&1 || restart_rc=$?
[[ $restart_rc != 0 ]] || fail 'reused Incus state was accepted'
grep -Eq 'prior state/session exists|workspace is not empty' "$evidence/reused-state-refusal.txt" ||
  fail 'reused-state denial was not the fresh-state policy'
step canary-smoke
fixture_finish

# Separate fresh fixture: no DB reset, fake process, mocked clock or TTL override.
fixture_prepare expiry
approval 600
stage_activate
approval 45
expiry=$(jq -er .expires_at "$flash/canary-approval.json")
ctl start --approve
ctl health
daemon_pid=$(< "$fixture/var/run/polyseat-unraid/incusd.pid")
assert_namespace "$daemon_pid"
ttl_started=$SECONDS
while [[ -e /proc/$daemon_pid ]]; do
  (( SECONDS - ttl_started < 65 )) || fail 'daemon outlived signed TTL plus teardown margin'
  sleep 0.2
done
# The conservative monotonic deadline can fire less than one second before
# the epoch deadline because both clocks are rounded down. Require the real
# supervisor's expiry reason, not just an unrelated crash near that time.
for ((attempt=0; attempt<30; attempt++)); do
  [[ -f $fixture/var/run/polyseat-unraid/canary-session.json.reason ]] &&
    [[ $(< "$fixture/var/run/polyseat-unraid/canary-session.json.reason") == expired ]] && break
  sleep 0.1
done
[[ $(< "$fixture/var/run/polyseat-unraid/canary-session.json.reason") == expired ]] ||
  fail 'workload death was not caused by the real expiry supervisor'
[[ $(< "$workload/cgroup.events") == *'populated 0'* ]] || fail 'TTL left workload processes alive'
for ((attempt=0; attempt<20; attempt++)); do
  [[ $(date -u +%s) -lt $expiry ]] || break
  sleep 0.1
done
[[ $(date -u +%s) -ge $expiry ]] || fail 'runner wall clock diverged from bounded monotonic expiry'
expect_blocked expired-token start --approve
grep -q 'expired' "$evidence/expired-token.txt" || fail 'expiry refusal did not come from approval check'
step expiry-refusal
fixture_finish

# Third fresh fixture: prove the in-workload guardian actually reacts when
# the separate supervisor is SIGKILLed. Its EXIT trap cannot help in this case.
fixture_prepare supervisor-loss
approval 600
stage_activate
approval 600
supervisor_expiry=$(jq -er .expires_at "$flash/canary-approval.json")
ctl start --approve
ctl health
daemon_pid=$(< "$fixture/var/run/polyseat-unraid/incusd.pid")
assert_namespace "$daemon_pid"
[[ $watchdog == "/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID-supervisor-loss" &&
   -d $watchdog && ! -L $watchdog && $(< "$watchdog/cgroup.events") == *'populated 1'* &&
   $(< "$workload/cgroup.events") == *'populated 1'* ]] || fail 'supervisor-loss fixture is not live and bounded'
printf 'SIGKILL supervisor subtree only: %s\n' "$watchdog" > "$evidence/supervisor-loss.txt"
printf '1\n' > "$watchdog/cgroup.kill"
for ((attempt=0; attempt<50; attempt++)); do
  [[ $(< "$workload/cgroup.events") != *'populated 0'* ]] || break
  sleep 0.1
done
[[ $(< "$workload/cgroup.events") == *'populated 0'* ]] ||
  fail 'guardian did not stop workload within five seconds of supervisor loss'
[[ $(date -u +%s) -lt $supervisor_expiry ]] || fail 'supervisor-loss result was confused with token expiry'
printf 'PASS: guardian emptied workload before token expiry after supervisor-only SIGKILL.\n' >> "$evidence/supervisor-loss.txt"
step supervisor-loss
fixture_finish

# Fourth fresh fixture: the exact Tower order (canary-smoke starts the daemon itself, no `start`), the Tower
# token actions, the Tower-like PATH and a complete per-process exec inventory. It also reproduces the run-1
# failure on purpose and proves the controller refuses a missing program before creating any state.
traced() { # label ctl-args... ; strace stops only at exec/process creation (seccomp-bpf), so timing stays real
  local label=$1
  shift
  mkdir -p "$evidence/exec-trace"
  strace -ff -qq --seccomp-bpf -s 160 -e trace=execve,execveat,clone,clone3,fork,vfork -e signal=none \
    -o "$evidence/exec-trace/$label" /usr/bin/env -i PATH="$tower_path" LANG=C \
    POLYSEAT_ROOT="$fixture" POLYSEAT_CI_REAL_SYSTEM=1 CI=true GITHUB_ACTIONS=true \
    RUNNER_OS=Linux RUNNER_ENVIRONMENT=github-hosted "$ctl_path" "$@"
}
fixture_prepare tower-sequence
approval 600 '["stage","activate","canary-smoke"]'
traced stage stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
traced activate activate "$archive_sha" --approve
ctl verify
slot="$fixture/usr/local/polyseat-unraid/slots/$archive_sha"
[[ -x $slot/payload/tools/unsquashfs && ! -L $slot/payload/tools/unsquashfs && ! -e $tower_path/unsquashfs ]] ||
  fail 'bundled unsquashfs missing, or the Tower-like PATH provides one'
# Negative 1: run 1 on Tower, reproduced exactly — the real daemon without the bundled helper directory.
neg_rc=0
env -i PATH="$tower_path" HOME="$work" INCUS_DIR="$work/negative-incus" LANG=C \
  timeout 20 "$slot/payload/bin/incusd" --group "" > "$evidence/negative-unsquashfs.txt" 2>&1 || neg_rc=$?
[[ $neg_rc != 0 && $neg_rc != 124 ]] || fail "daemon without the bundled unsquashfs did not fail fast (rc=$neg_rc)"
# shellcheck disable=SC2016  # the literal Go error text contains "$PATH"
grep -Fq 'exec: "unsquashfs": executable file not found in $PATH' "$evidence/negative-unsquashfs.txt" ||
  fail 'a missing unsquashfs did not reproduce the Tower failure'
[[ ! -e $work/negative-incus ]] || fail 'the refused daemon start created state'
step missing-unsquashfs-reproduced
# Negative 2: a missing host program is refused before any one-shot state, cgroup or session exists.
mv -- "$tower_path/rsync" "$work/rsync.hidden"
refuse_rc=0
ctl canary-smoke --approve > "$evidence/missing-program-refusal.txt" 2>&1 || refuse_rc=$?
mv -- "$work/rsync.hidden" "$tower_path/rsync"
if [[ $refuse_rc == 0 ]] ||
   ! grep -q 'required daemon programs missing on PATH: rsync' "$evidence/missing-program-refusal.txt"; then
  fail 'a missing host program was not refused by the controller'
fi
[[ ! -e $data/incus && ! -e $data/one-shot.json && ! -e $fixture/var/run/polyseat-unraid/canary-session.json &&
   ! -e $workload && ! -e $watchdog &&
   ! -e /var/lib/polyseat-lxc ]] ||
  fail 'the refused start left one-shot state, cgroups or a session behind'
step missing-program-refusal
# Positive: the exact Tower sequence with only the bundled unsquashfs available.
traced canary-smoke canary-smoke --approve > "$evidence/tower-sequence-smoke.txt" 2>&1 ||
  fail 'Tower-sequence canary-smoke failed (see tower-sequence-smoke.txt)'
if ! grep -q '^PASS: fresh CPU-only canary lifecycle' "$evidence/tower-sequence-smoke.txt" ||
   [[ $(grep -c '^POLYSEAT_CANARY_OK$' "$evidence/tower-sequence-smoke.txt") != 2 ]]; then
  fail 'Tower sequence did not complete both container execs'
fi
[[ ! -e $workload && ! -e $watchdog ]] || fail 'Tower sequence did not remove its own groups'
step tower-sequence
jq -r '.files[]' "$proof/busybox-image-provenance.json" > "$work/image-files.txt"
for label in stage activate canary-smoke; do
  lifecycle=()
  [[ $label != canary-smoke ]] || lifecycle=(--require-lifecycle)
  python3 "$GITHUB_WORKSPACE/unraid/scripts/exec-inventory.py" --trace-dir "$evidence/exec-trace" --prefix "$label" \
    --tower-tools "$tower_tools" --tower-path "$tower_path" --fixture "$fixture" --controller "$ctl_path" \
    --image-files "$work/image-files.txt" "${lifecycle[@]}" \
    --output "$evidence/exec-inventory-$label.json" > "$evidence/exec-inventory-$label.txt" ||
    fail "exec inventory found a program Unraid lacks or an incomplete trace ($label)"
done
tar -C "$evidence" -czf "$evidence/exec-trace.tar.gz" exec-trace
find "$evidence/exec-trace" -type f -name '*.*' -delete
rmdir -- "$evidence/exec-trace"
step exec-inventory
fixture_finish

if pgrep -x incusd >/dev/null; then fail 'Incus daemon or helper survived teardown'; fi
for group in "${fixture_groups[@]}"; do [[ ! -e $group ]] || fail 'a CI cgroup remains'; done
step cleanup
host_state after
assert_host_state after final
step host-state-unchanged

commit=$(git -C "$GITHUB_WORKSPACE" rev-parse HEAD)
[[ $commit =~ ^[0-9a-f]{40}$ ]] || fail 'source commit unavailable'
jq -n --arg commit "$commit" --arg archive "$archive_sha" --arg controller "$controller_sha" \
  --arg controller_file "$controller_file_sha" --arg image "$image_sha" \
  --rawfile steps "$steps_file" --slurpfile lifecycle "$evidence/render-lifecycle.json" \
  --slurpfile expiry "$evidence/render-expiry.json" \
  --slurpfile supervisor_loss "$evidence/render-supervisor-loss.json" \
  --slurpfile tower_sequence "$evidence/render-tower-sequence.json" \
  --slurpfile inventory "$evidence/exec-inventory-canary-smoke.json" \
  '{schema:1,commit:$commit,archive_sha256:$archive,controller_sha256:$controller,
    controller_file_sha256:$controller_file,image_sha256:$image,platform:"disposable-linux",
    status:"passed",steps:($steps|split("\n")|map(select(length>0))),
    ci_renderings:[$lifecycle[0],$expiry[0],$supervisor_loss[0],$tower_sequence[0]],
    exec_inventory:($inventory[0] | {ok, processes, "tower-path", denied, bundle, container,
      "host-absolute-known", "missing-everywhere-but-on-tower"}),
    caveat:"Real canary gate and lifecycle on disposable Linux; not an Unraid test or production approval"}' \
  > "$proof/canary-rehearsal.json"
printf 'PASS: signed canary gate, real CPU-only lifecycle, namespace/cgroup containment, TTL, supervisor-loss fail-safe and host-state restoration; NOT Unraid approval.\n' \
  > "$proof/canary-e2e-result.txt"
chmod a+r "$proof/canary-rehearsal.json" "$proof/canary-e2e-result.txt"
passed=1
