#!/bin/bash
# WIP. M1 desktop canary rehearsal: the exact Tower sequence of the signed gate=canary controller
# (stage, activate, seat-image-stage, desktop-smoke, uninstall) with the real pinned runtime bundle and
# the real seat image, on a disposable GitHub-hosted runner ONLY. Throwaway keys. Not Unraid evidence.
set -euo pipefail
umask 077

fail() { printf 'DESKTOP CANARY E2E BLOCKED/FAILED: %s\n' "$*" >&2; exit 2; }
[[ ${CI:-} == true && ${GITHUB_ACTIONS:-} == true &&
   ${RUNNER_OS:-} == Linux && ${RUNNER_ENVIRONMENT:-} == github-hosted &&
   ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ! -e /etc/unraid-version ]] ||
  fail 'disposable GitHub-hosted Linux runner required; Unraid forbidden'
[[ $(uname -r) != *[Uu]nraid* && $(id -u) == 0 ]] || fail 'root on disposable Linux required'
[[ ${GITHUB_WORKSPACE:-} == /home/runner/work/* && -d $GITHUB_WORKSPACE && ! -L $GITHUB_WORKSPACE ]] ||
  fail 'actual hosted-runner workspace required'
[[ ${SEAT_IMAGE_DIR:-} == "$GITHUB_WORKSPACE"/* && -d $SEAT_IMAGE_DIR && ! -L $SEAT_IMAGE_DIR ]] ||
  fail 'SEAT_IMAGE_DIR must be the downloaded seat image artifact inside the workspace'
for tool in bash python3 jq git ssh-keygen sha256sum tar findmnt mount umount mountpoint \
            ip ss iptables-save ip6tables-save nft pgrep stat readlink timeout; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing CI prerequisite: $tool"
done
[[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]] || fail 'unified cgroup v2 required'
if pgrep -x incusd >/dev/null; then fail 'another Incus daemon already exists'; fi

proof="$GITHUB_WORKSPACE/incus-build-proof"
source_ctl="$GITHUB_WORKSPACE/unraid/plugin/src/emhttp/scripts/polyseatctl"
[[ -d $proof && ! -L $proof && -f $proof/bundle.env && -f $source_ctl ]] || fail 'real build outputs missing'
grep -q '^POLYSEAT_RELEASE_GATE="closed"' "$source_ctl" || fail 'committed release gate is not closed'
archive_sha=$(sed -n 's/^BUNDLE_SHA256=//p' "$proof/bundle.env")
[[ $archive_sha =~ ^[0-9a-f]{64}$ ]] || fail 'invalid bundle identity'
runtime_archive="$proof/bundle/$archive_sha.tar.gz"
runtime_manifest="$proof/bundle/$archive_sha.manifest.json"
[[ $(sha256sum "$runtime_archive" | awk '{print $1}') == "$archive_sha" ]] || fail 'runtime archive hash mismatch'
jq -e '.files | any(.[]; .path == "payload/share/lxc/hooks/nvidia" and .mode == 493)' "$runtime_manifest" >/dev/null ||
  fail 'runtime bundle lacks the LXC nvidia hook'

mapfile -t images < <(find "$SEAT_IMAGE_DIR" -maxdepth 1 -type f \( -name '*.tar.xz' -o -name '*.tar.gz' \))
[[ ${#images[@]} == 1 ]] || fail 'expected exactly one seat image tarball'
seat_image=${images[0]}
seat_json="$SEAT_IMAGE_DIR/seat-image.json"
[[ -f $seat_json && ! -L $seat_json ]] || fail 'seat-image.json missing'
seat_sha=$(sha256sum "$seat_image" | awk '{print $1}')
[[ $(jq -er '.image_sha256' "$seat_json") == "$seat_sha" ]] || fail 'seat image not bound to its build record'
unpacked=$(jq -er '.unpacked_bytes // .unpacked_size_bytes // .rootfs_bytes' "$seat_json") || fail 'unpacked size missing'
[[ $unpacked =~ ^[0-9]+$ ]] || fail 'invalid unpacked size'

work=$(mktemp -d /tmp/polyseat-desktop-e2e-XXXXXXXX)
evidence="$proof/desktop-canary-e2e"
[[ ! -e $evidence ]] || fail 'stale desktop evidence'
mkdir -- "$evidence"
fixture="" passed=0 workload="" watchdog=""

# Tower-like PATH, exactly as in ci-e2e-canary.sh (programs recorded read-only on the Unraid host).
tower_tools="$GITHUB_WORKSPACE/unraid/abi/unraid-7.3.3-x86_64.tools"
tower_path="$work/tower-path"
mkdir -m 0755 -- "$tower_path"
while IFS= read -r name; do
  [[ -z $name || $name == \#* ]] && continue
  [[ $name =~ ^(\[|[A-Za-z0-9][A-Za-z0-9._+-]*)$ ]] || fail "invalid Tower tool name: $name"
  resolved=$( PATH=/usr/sbin:/usr/bin:/sbin:/bin; type -P -- "$name" ) || resolved=""
  [[ -z $resolved ]] || ln -s -- "$resolved" "$tower_path/$name"
done < "$tower_tools"
for absent in unsquashfs mksquashfs nft python3; do
  [[ ! -e $tower_path/$absent ]] || fail "Tower-like PATH provides $absent, which Unraid lacks"
done

ctl() {
  env -i PATH="$tower_path" LANG=C \
    POLYSEAT_ROOT="$fixture" POLYSEAT_CI_REAL_SYSTEM=1 CI=true GITHUB_ACTIONS=true \
    RUNNER_OS=Linux RUNNER_ENVIRONMENT=github-hosted "$ctl_path" "$@"
}
step() { printf 'PASS: %s\n' "$1"; printf '%s\n' "$1" >> "$evidence/steps.txt"; }

host_state() {
  local snapshot="$evidence/host-$1" key path
  mkdir -- "$snapshot"
  iptables-save | sed -E '/^#/d; s/\[[0-9]+:[0-9]+\]/[0:0]/g' > "$snapshot/iptables"
  ip6tables-save | sed -E '/^#/d; s/\[[0-9]+:[0-9]+\]/[0:0]/g' > "$snapshot/ip6tables"
  nft --stateless list ruleset > "$snapshot/nft"
  for key in net.ipv4.ip_forward net.ipv6.conf.all.forwarding user.max_user_namespaces kernel.pid_max; do
    path="/proc/sys/${key//./\/}"
    if [[ -f $path ]]; then printf '%s=%s\n' "$key" "$(< "$path")"; else printf '%s=ABSENT\n' "$key"; fi
  done > "$snapshot/sysctl"
  ip -j link show | jq -S '[.[] | {ifname,mtu,master,link_type,address}] | sort_by(.ifname)' > "$snapshot/links"
  ss -H -lnt | awk '{print "tcp",$4,$5}' | sort > "$snapshot/tcp-listeners"
  ss -H -lnu | awk '{print "udp",$4,$5}' | sort > "$snapshot/udp-listeners"
  awk '{print $5}' /proc/self/mountinfo | sort > "$snapshot/mountpoints"
  find /sys/fs/cgroup -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort > "$snapshot/cgroups"
  sha256sum /etc/subuid /etc/subgid > "$snapshot/subids"
  awk '{print $1}' /proc/modules | sort > "$snapshot/modules"
  for path in /var/lib/polyseat-lxc /var/lib/polyseat-lxc/rootfs; do
    if [[ -e $path || -L $path ]]; then stat -c '%n %F %u %g %a' "$path"; else printf '%s ABSENT\n' "$path"; fi
  done > "$snapshot/lxc-mount-paths"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -n $fixture ]]; then
    ctl stop >> "$evidence/cleanup.txt" 2>&1
    ctl uninstall --approve >> "$evidence/cleanup.txt" 2>&1
    mountpoint -q -- "$fixture/var/lib/polyseat-unraid-canary" && umount -- "$fixture/var/lib/polyseat-unraid-canary"
    mkdir -p "$evidence/logs"
    find "$fixture/var/log/polyseat-unraid" -maxdepth 1 -type f -exec cp -t "$evidence/logs" -- {} + 2>/dev/null
    if [[ -d $fixture/var/run/polyseat-unraid/desktop-evidence ]]; then
      cp -r -- "$fixture/var/run/polyseat-unraid/desktop-evidence" "$evidence/controller-evidence"
    fi
  fi
  for group in "$workload" "$watchdog"; do
    if [[ -n $group && -d $group && ! -L $group ]]; then
      printf '1\n' > "$group/cgroup.kill"
      find "$group" -depth -mindepth 1 -type d -exec rmdir -- {} \;
      rmdir -- "$group" || status=2
    fi
  done
  rm -f -- "$work/release-key" "$work/approval-key"
  find "$evidence" -type f -exec chmod a+r -- {} +
  find "$evidence" -type d -exec chmod a+rx -- {} +
  if [[ $status != 0 || $passed != 1 ]]; then
    printf 'FAIL: desktop canary rehearsal incomplete; not Unraid evidence.\n' > "$proof/desktop-canary-e2e-result.txt"
    chmod a+r "$proof/desktop-canary-e2e-result.txt"
    exit 2
  fi
  exit 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

host_state before
ssh-keygen -q -t ed25519 -N '' -f "$work/release-key"
ssh-keygen -q -t ed25519 -N '' -f "$work/approval-key"
cp "$runtime_manifest" "$work/runtime.manifest.json"
ssh-keygen -q -Y sign -f "$work/release-key" -n polyseat-unraid-bundle "$work/runtime.manifest.json"
commit=$(git -C "$GITHUB_WORKSPACE" rev-parse HEAD)
SOURCE_DATE_EPOCH=$(git -C "$GITHUB_WORKSPACE" log -1 --format=%ct) python3 -I \
  "$GITHUB_WORKSPACE/unraid/scripts/make-seat-manifest.py" --image "$seat_image" --unpacked-bytes "$unpacked" \
  --source-commit "$commit" --out "$work/seat.manifest.json" > /dev/null
ssh-keygen -q -Y sign -f "$work/release-key" -n polyseat-unraid-seat-image "$work/seat.manifest.json"
uuid=$(< /sys/class/dmi/id/product_uuid)
host_sha=$(printf '%s' "${uuid,,}" | sha256sum | awk '{print $1}')
# The exact controller package the CPU canary rehearsal of this run used (same artifact, not a rebuild).
controller_package="$proof/canary-controller.txz"
[[ -f $controller_package && ! -L $controller_package ]] || fail 'CPU rehearsal controller package missing'
controller_sha=$(sha256sum "$controller_package" | awk '{print $1}')
[[ $(jq -er .controller_sha256 "$proof/canary-rehearsal.json") == "$controller_sha" ]] ||
  fail 'controller package differs from the one the CPU canary rehearsal bound'
seat_manifest_sha=$(sha256sum "$work/seat.manifest.json" | awk '{print $1}')

# Two CPUs of the runner (the desktop budget is two CPUs' worth on Tower's four E-cores).
host_cpuset=$(< /sys/fs/cgroup/cpuset.cpus.effective)
mapfile -t cpus < <(python3 -c 'import sys
out=[]
for part in sys.argv[1].split(","):
    a,_,b=part.partition("-"); out+=range(int(a),int(b or a)+1)
print("\n".join(map(str,out[:2])))' "$host_cpuset")
[[ ${#cpus[@]} == 2 ]] || fail 'runner needs at least two CPUs'
# Written exactly as the kernel reads cpuset.cpus back (the controller compares the read-back verbatim).
if (( cpus[1] == cpus[0] + 1 )); then ci_cpuset="${cpus[0]}-${cpus[1]}"; else ci_cpuset="${cpus[0]},${cpus[1]}"; fi

fixture=$(mktemp -d /tmp/polyseat-ctl-desktop-XXXXXXXX)
chmod 0711 "$fixture"
flash="$fixture/boot/config/plugins/polyseat-unraid"
data="$fixture/var/lib/polyseat-unraid-canary"
ctl_path="$fixture/usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"
workload="/sys/fs/cgroup/polyseat-unraid-canary-ci-$GITHUB_RUN_ID-desktop"
watchdog="/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID-desktop"
mkdir -p "$flash" "$fixture/etc" "$fixture/var/lib" "$data"
chmod 0755 "$fixture/var" "$fixture/var/lib" "$data"
# RAM-backed like Tower's /var/lib; sized above the controller's monitored 3 GiB data budget.
mount -t tmpfs -o size=4g,nosuid,nodev,mode=0755 tmpfs "$data"
tar --extract --xz --file "$controller_package" --directory "$fixture" --no-same-owner
python3 - "$ctl_path" "$host_cpuset" "$ci_cpuset" "$workload" "$watchdog" "$tower_path" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
text = path.read_text()
if len(re.findall(r'^POLYSEAT_RELEASE_GATE="canary"(?:\s+#.*)?$', text, re.M)) != 1:
    raise SystemExit('not the real canary package')
for name, value in zip(('CANARY_HOST_CPUSET', 'CANARY_CPUSET', 'CANARY_CGROUP',
                        'CANARY_WATCHDOG_CGROUP', 'SAFE_PATH'), sys.argv[2:]):
    text, count = re.subn(r'^' + name + r'="[^"\n]*"', name + '="' + value + '"', text, flags=re.M)
    if count != 1:
        raise SystemExit('CI constant missing/duplicated: ' + name)
path.write_text(text)
PY
jq -n --arg file "$(sha256sum "$ctl_path" | awk '{print $1}')" --arg cpus "$ci_cpuset" --arg host "$host_cpuset" \
  --arg workload "$workload" --arg watchdog "$watchdog" --arg safe "$tower_path" \
  '{ci_controller_sha256:$file,overrides:{CANARY_CPUSET:$cpus,CANARY_HOST_CPUSET:$host,CANARY_CGROUP:$workload,
    CANARY_WATCHDOG_CGROUP:$watchdog,SAFE_PATH:$safe}}' > "$evidence/render.json"
printf 'version="7.3.3"\n' > "$fixture/etc/unraid-version"
printf 'SERVICE="disabled"\nDATA_DIR="/var/lib/polyseat-unraid-canary"\nACCEPT_NO_LSM="yes"\n' > "$flash/polyseat-unraid.cfg"
printf 'polyseat-unraid-release namespaces="polyseat-unraid-bundle,polyseat-unraid-seat-image" %s\n' \
  "$(awk '{print $1,$2}' "$work/release-key.pub")" > "$flash/allowed_signers"
printf 'polyseat-unraid-canary-approver namespaces="polyseat-unraid-canary-approval-v1" %s\n' \
  "$(awk '{print $1,$2}' "$work/approval-key.pub")" > "$flash/canary_allowed_signers"
now=$(date -u +%s)
jq -n --arg host "$host_sha" --arg archive "$archive_sha" --arg seat "$seat_sha" --argjson now "$now" \
  '{schema:1,host_sha256:$host,archive_sha256:$archive,seat_image_sha256:$seat,issued_at:$now,
    expires_at:($now+1500),actions:["stage","activate","seat-image-stage","desktop-smoke"]}' > "$flash/canary-approval.json"
ssh-keygen -q -Y sign -f "$work/approval-key" -n polyseat-unraid-canary-approval-v1 "$flash/canary-approval.json"
chmod 0600 "$flash/canary-approval.json" "$flash/canary-approval.json.sig"

ctl stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve
ctl activate "$archive_sha" --approve
ctl verify
step stage-activate
# A bundle signature must never be accepted for the seat image.
cp "$work/runtime.manifest.json.sig" "$work/wrong.sig"
rc=0; ctl seat-image-stage "$seat_image" "$work/seat.manifest.json" "$work/wrong.sig" --approve > "$evidence/wrong-signature.txt" 2>&1 || rc=$?
[[ $rc == 5 ]] || fail "seat image accepted a wrong signature (rc=$rc)"
ctl seat-image-stage "$seat_image" "$work/seat.manifest.json" "$work/seat.manifest.json.sig" --approve
step seat-image-stage

sampler() { # host-side, read-only: peak of the bounded workload group every second
  local peak=0 cur pids=0 p
  while [[ -d $workload || ! -e $evidence/sampler.stop ]]; do
    [[ -e $evidence/sampler.stop ]] && break
    if [[ -r $workload/memory.current ]]; then
      cur=$(< "$workload/memory.current"); (( cur > peak )) && peak=$cur
      p=$(< "$workload/pids.current"); (( p > pids )) && pids=$p
      printf '%s %s %s\n' "$(date -u +%s)" "$cur" "$p" >> "$evidence/sampler.txt"
    fi
    sleep 1
  done
  printf 'memory_peak_bytes=%s\npids_peak=%s\n' "$peak" "$pids" > "$evidence/sampler-peak.txt"
}
sampler &
sampler_pid=$!
started=$SECONDS
rc=0
ctl desktop-smoke --approve > "$evidence/desktop-smoke.txt" 2>&1 || rc=$?
touch "$evidence/sampler.stop"
wait "$sampler_pid" || true
printf 'desktop_smoke_seconds=%s\n' "$((SECONDS - started))" >> "$evidence/sampler-peak.txt"
[[ $rc == 0 ]] || fail "desktop-smoke failed (rc=$rc, see desktop-smoke.txt)"
grep -q '^PASS: CPU-only desktop canary' "$evidence/desktop-smoke.txt" || fail 'desktop-smoke did not report PASS'
for phase in start restart; do
  grep -q "desktop self-test ($phase) PASS" "$evidence/desktop-smoke.txt" || fail "self-test $phase not evidenced"
done
[[ ! -e $workload && ! -e $watchdog ]] || fail 'desktop-smoke did not remove its own groups'
step desktop-smoke

ctl uninstall --approve
[[ ! -e $fixture/var/run/polyseat-unraid/seat-image/image ]] || fail 'uninstall left the staged seat image'
cp -r -- "$fixture/var/run/polyseat-unraid/desktop-evidence" "$evidence/controller-evidence"
umount -- "$data"
fixture_done=$fixture
fixture=""
if pgrep -x incusd >/dev/null; then fail 'Incus daemon or helper survived teardown'; fi
step cleanup
host_state after
for item in iptables ip6tables nft sysctl links tcp-listeners udp-listeners mountpoints cgroups subids modules lxc-mount-paths; do
  diff -u "$evidence/host-before/$item" "$evidence/host-after/$item" > "$evidence/diff-$item.txt" ||
    fail "host state changed: $item"
done
step host-state-unchanged

jq -n --arg commit "$commit" --arg archive "$archive_sha" --arg seat "$seat_sha" --arg fixture "$fixture_done" \
  --arg controller "$controller_sha" --arg seat_manifest "$seat_manifest_sha" \
  --slurpfile manifest "$work/seat.manifest.json" --slurpfile render "$evidence/render.json" \
  --rawfile steps "$evidence/steps.txt" --rawfile peak "$evidence/sampler-peak.txt" \
  --slurpfile st "$evidence/controller-evidence/selftest-start.json" \
  --slurpfile rs "$evidence/controller-evidence/selftest-restart.json" \
  '{schema:1,commit:$commit,archive_sha256:$archive,controller_sha256:$controller,seat_image_sha256:$seat,
    seat_manifest_sha256:$seat_manifest,seat_manifest:$manifest[0],ci_rendering:$render[0],platform:"disposable-linux",
    status:"passed",steps:($steps|split("\n")|map(select(length>0))),measurements:$peak,
    selftest:{start:$st[0],restart:$rs[0]},
    caveat:"Desktop canary gate and lifecycle on disposable Linux; not an Unraid test or production approval"}' \
  > "$proof/desktop-canary-rehearsal.json"
printf 'PASS: signed desktop canary (seat image stage, systemd + headless sway + Sunshine software encoder, start/stop/restart/delete) and host-state restoration; NOT Unraid approval.\n' \
  > "$proof/desktop-canary-e2e-result.txt"
chmod a+r "$proof/desktop-canary-rehearsal.json" "$proof/desktop-canary-e2e-result.txt"
passed=1
