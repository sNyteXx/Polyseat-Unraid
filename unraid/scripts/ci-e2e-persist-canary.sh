#!/bin/bash
# WIP. Step 2 of unraid/docs/upstream-integration-plan.md: the signed gate=canary controller keeps its Incus state on
# a real ZFS dataset `cache/polyseat` mounted below the fixture's /mnt/cache, as on Tower. Three runs, each with its
# own token and the plugin's RAM state removed in between exactly as before a Tower run:
#   A  fresh: workspace created, seat created from the real seat image, self-test, clean stop recorded;
#   B  reused: the persisted database and seat start again, nothing auto-started, self-test, clean stop recorded;
#   C  no clean-stop record (as after a crash): refused before anything starts;
#   D  step 3: upstream polyseatd on the persisted Incus, its interface on the fixture LAN address only, the
#      password set before its first start; login, the locked host actions and the clean stop checked from outside.
# Disposable GitHub-hosted runner ONLY. Throwaway keys. Not Unraid evidence.
set -euo pipefail
umask 077

fail() { printf 'PERSIST CANARY E2E BLOCKED/FAILED: %s\n' "$*" >&2; exit 2; }
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
            pgrep stat readlink timeout setpriv zpool zfs curl ip; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing CI prerequisite: $tool"
done
[[ -c /dev/zfs ]] || fail 'ZFS kernel module not loaded'
[[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]] || fail 'unified cgroup v2 required'
if pgrep -x incusd >/dev/null; then fail 'another Incus daemon already exists'; fi
if zpool list -H -o name cache >/dev/null 2>&1; then fail 'a pool named cache already exists'; fi

proof="$GITHUB_WORKSPACE/incus-build-proof"
source_ctl="$GITHUB_WORKSPACE/unraid/plugin/src/emhttp/scripts/polyseatctl"
[[ -d $proof && ! -L $proof && -f $proof/bundle.env && -f $source_ctl ]] || fail 'real build outputs missing'
grep -q '^POLYSEAT_RELEASE_GATE="closed"' "$source_ctl" || fail 'committed release gate is not closed'
archive_sha=$(sed -n 's/^BUNDLE_SHA256=//p' "$proof/bundle.env")
[[ $archive_sha =~ ^[0-9a-f]{64}$ ]] || fail 'invalid bundle identity'
runtime_archive="$proof/bundle/$archive_sha.tar.gz"
runtime_manifest="$proof/bundle/$archive_sha.manifest.json"
[[ $(sha256sum "$runtime_archive" | awk '{print $1}') == "$archive_sha" ]] || fail 'runtime archive hash mismatch'

mapfile -t images < <(find "$SEAT_IMAGE_DIR" -maxdepth 1 -type f \( -name '*.tar.xz' -o -name '*.tar.gz' \))
[[ ${#images[@]} == 1 ]] || fail 'expected exactly one seat image tarball'
seat_image=${images[0]}
seat_json="$SEAT_IMAGE_DIR/seat-image.json"
[[ -f $seat_json && ! -L $seat_json ]] || fail 'seat-image.json missing'
seat_sha=$(sha256sum "$seat_image" | awk '{print $1}')
[[ $(jq -er '.image_sha256' "$seat_json") == "$seat_sha" ]] || fail 'seat image not bound to its build record'
unpacked=$(jq -er '.unpacked_bytes // .unpacked_size_bytes // .rootfs_bytes' "$seat_json") || fail 'unpacked size missing'
[[ $unpacked =~ ^[0-9]+$ ]] || fail 'invalid unpacked size'

work=$(mktemp -d /tmp/polyseat-persist-e2e-XXXXXXXX)
evidence="$proof/persist-canary-e2e"
[[ ! -e $evidence ]] || fail 'stale persist evidence'
mkdir -- "$evidence"
fixture="" passed=0 workload="" watchdog="" pool_file=""

# Tower-like PATH, exactly as in ci-e2e-desktop-canary.sh (programs recorded read-only on the Unraid host).
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
[[ -e $tower_path/zfs && -e $tower_path/findmnt && -e $tower_path/df ]] || fail 'Tower-like PATH lacks zfs/findmnt/df'

ctl() {
  env -i PATH="$tower_path" LANG=C \
    POLYSEAT_ROOT="$fixture" POLYSEAT_CI_REAL_SYSTEM=1 CI=true GITHUB_ACTIONS=true \
    RUNNER_OS=Linux RUNNER_ENVIRONMENT=github-hosted "$ctl_path" "$@"
}
step() { printf 'PASS: %s\n' "$1"; printf '%s\n' "$1" >> "$evidence/steps.txt"; }

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [[ -n $fixture ]]; then
    ctl stop >> "$evidence/cleanup.txt" 2>&1
    ctl uninstall --approve >> "$evidence/cleanup.txt" 2>&1
    mkdir -p "$evidence/logs"
    find "$fixture/var/log/polyseat-unraid" -maxdepth 1 -type f -exec cp -t "$evidence/logs" -- {} + 2>/dev/null
    mountpoint -q -- "$fixture/var/run" && umount -l -- "$fixture/var/run"
  fi
  for group in "$workload" "$watchdog"; do
    if [[ -n $group && -d $group && ! -L $group ]]; then
      printf '1\n' > "$group/cgroup.kill"
      find "$group" -depth -mindepth 1 -type d -exec rmdir -- {} \;
      rmdir -- "$group" || status=2
    fi
  done
  ip link del br0 2>/dev/null
  zpool list -H -o name cache >/dev/null 2>&1 && zpool destroy -f cache
  [[ -n $pool_file ]] && rm -f -- "$pool_file"
  rm -f -- "$work/release-key" "$work/approval-key"
  find "$evidence" -type f -exec chmod a+r -- {} +
  find "$evidence" -type d -exec chmod a+rx -- {} +
  if [[ $status != 0 || $passed != 1 ]]; then
    printf 'FAIL: persist canary rehearsal incomplete; not Unraid evidence.\n' > "$proof/persist-canary-e2e-result.txt"
    chmod a+r "$proof/persist-canary-e2e-result.txt"
    exit 2
  fi
  exit 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

modprobe ipip
modprobe bonding max_bonds=0
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
controller_package="$proof/canary-controller.txz"
[[ -f $controller_package && ! -L $controller_package ]] || fail 'CPU rehearsal controller package missing'
controller_sha=$(sha256sum "$controller_package" | awk '{print $1}')
[[ $(jq -er .controller_sha256 "$proof/canary-rehearsal.json") == "$controller_sha" ]] ||
  fail 'controller package differs from the one the CPU canary rehearsal bound'

host_cpuset=$(< /sys/fs/cgroup/cpuset.cpus.effective)
mapfile -t cpus < <(python3 -c 'import sys
out=[]
for part in sys.argv[1].split(","):
    a,_,b=part.partition("-"); out+=range(int(a),int(b or a)+1)
print("\n".join(map(str,out[:2])))' "$host_cpuset")
[[ ${#cpus[@]} == 2 ]] || fail 'runner needs at least two CPUs'
if (( cpus[1] == cpus[0] + 1 )); then ci_cpuset="${cpus[0]}-${cpus[1]}"; else ci_cpuset="${cpus[0]},${cpus[1]}"; fi

fixture=$(mktemp -d /tmp/polyseat-ctl-persist-XXXXXXXX)
chmod 0711 "$fixture"
flash="$fixture/boot/config/plugins/polyseat-unraid"
ctl_path="$fixture/usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"
workload="/sys/fs/cgroup/polyseat-unraid-canary-ci-$GITHUB_RUN_ID-persist"
watchdog="/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID-persist"
mkdir -p "$flash" "$fixture/etc" "$fixture/var/lib" "$fixture/mnt"
chmod 0755 "$fixture/var" "$fixture/var/lib" "$fixture/mnt"

# Tower parity (measured read-only): the pool `cache` is mounted at /mnt/cache, 0777 and not ours; the dataset
# `cache/polyseat` has a quota and is mounted at /mnt/cache/polyseat, whose root the owner gave back to root:root 0755.
pool_file="$work/cache.zpool"
truncate -s 24G "$pool_file"
zpool create -f -o ashift=12 -O mountpoint="$fixture/mnt/cache" -O compression=lz4 -O atime=off -O xattr=sa cache "$pool_file"
chmod 0777 "$fixture/mnt/cache"
zfs create -o quota=20G -o recordsize=1M cache/polyseat
chown 0:0 "$fixture/mnt/cache/polyseat"
chmod 0755 "$fixture/mnt/cache/polyseat"
[[ $(findmnt -n -o SOURCE,FSTYPE --mountpoint "$fixture/mnt/cache/polyseat" | tr -s ' ') == 'cache/polyseat zfs' ]] ||
  fail 'fixture dataset not mounted as on Tower'
zfs get -H -o property,value quota,used,available,compression,xattr cache/polyseat > "$evidence/dataset.txt"
step zfs-dataset-like-tower

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

state="$fixture/mnt/cache/polyseat/incus"

ram_state_fresh() { # Tower: /run is a fresh 128 MiB noexec tmpfs; the plugin's RAM paths are removed before a run
  mountpoint -q -- "$fixture/var/run" && umount -- "$fixture/var/run"
  rm -rf -- "$fixture/var/lib/polyseat-unraid-canary" "$fixture/var/log/polyseat-unraid"
  mkdir -p "$fixture/var/run"
  chmod 0755 "$fixture/var/run"
  mount -t tmpfs -o size=128m,nosuid,nodev,noexec,mode=0755 tmpfs "$fixture/var/run"
}

install_run() { # name [token actions as JSON] : a token for this run only, then stage, activate and seat-image-stage
  local now actions=${2:-'["stage","activate","seat-image-stage","persist-smoke"]'}
  now=$(date -u +%s)
  rm -f -- "$flash/canary-approval.json" "$flash/canary-approval.json.sig"
  jq -n --arg host "$host_sha" --arg archive "$archive_sha" --arg seat "$seat_sha" --argjson now "$now" --argjson actions "$actions" \
    '{schema:1,host_sha256:$host,archive_sha256:$archive,seat_image_sha256:$seat,issued_at:$now,
      expires_at:($now+1500),actions:$actions}' > "$flash/canary-approval.json"
  ssh-keygen -q -Y sign -f "$work/approval-key" -n polyseat-unraid-canary-approval-v1 "$flash/canary-approval.json"
  chmod 0600 "$flash/canary-approval.json" "$flash/canary-approval.json.sig"
  ctl stage "$runtime_archive" "$work/runtime.manifest.json" "$work/runtime.manifest.json.sig" --approve > "$evidence/$1-install.txt" 2>&1
  ctl activate "$archive_sha" --approve >> "$evidence/$1-install.txt" 2>&1
  ctl seat-image-stage "$seat_image" "$work/seat.manifest.json" "$work/seat.manifest.json.sig" --approve >> "$evidence/$1-install.txt" 2>&1
}

finish_run() { # name : collect the controller's evidence, uninstall, check that nothing runs and the state stayed
  [[ ! -e $fixture/var/run/polyseat-unraid/persist-evidence ]] ||
    cp -r -- "$fixture/var/run/polyseat-unraid/persist-evidence" "$evidence/$1-controller-evidence"
  [[ ! -e $fixture/var/run/polyseat-unraid/polyseatd-evidence ]] ||
    cp -r -- "$fixture/var/run/polyseat-unraid/polyseatd-evidence" "$evidence/$1-polyseatd-evidence"
  ctl uninstall --approve >> "$evidence/$1-install.txt" 2>&1
  if pgrep -x incusd >/dev/null; then fail "$1: Incus daemon survived"; fi
  [[ ! -e $workload && ! -e $watchdog ]] || fail "$1: the run did not remove its groups"
  ram_state_fresh
}

ram_state_fresh
host_mounts_before=$(awk '{print $5}' /proc/self/mountinfo | sort | sha256sum)

# --- run A: fresh ---
install_run A
rc=0; ctl persist-smoke --approve > "$evidence/A-persist-smoke.txt" 2>&1 || rc=$?
[[ $rc == 0 ]] || fail "run A failed (rc=$rc, see A-persist-smoke.txt)"
grep -q '^PASS: persistent Incus state on cache/polyseat (fresh)' "$evidence/A-persist-smoke.txt" || fail 'run A: no fresh PASS'
grep -q 'desktop self-test (fresh) PASS' "$evidence/A-persist-smoke.txt" || fail 'run A: self-test not evidenced'
[[ $(stat -c '%u %g %a' "$state") == '0 0 711' && $(stat -c '%u %g %a' "$state/incus") == '0 0 711' ]] ||
  fail 'run A: workspace owner/mode'
[[ $(stat -c '%u %a' "$state/clean-stop.json") == '0 600' ]] || fail 'run A: no clean-stop record'
jq -e '.instances == [{"name":"polyseat-persist-cpu","status":"Stopped","autostart":"false"}]' "$state/clean-stop.json" >/dev/null ||
  fail 'run A: clean-stop record content'
[[ -d $state/incus/storage-pools/default/containers/polyseat-persist-cpu ]] || fail 'run A: the seat rootfs is not on the dataset'
finish_run A
step run-a-fresh

# --- run B: the persisted database and seat start again ---
install_run B
rc=0; ctl persist-smoke --approve > "$evidence/B-persist-smoke.txt" 2>&1 || rc=$?
[[ $rc == 0 ]] || fail "run B failed (rc=$rc, see B-persist-smoke.txt)"
grep -q '^PASS: persistent Incus state on cache/polyseat (reused)' "$evidence/B-persist-smoke.txt" || fail 'run B: no reused PASS'
grep -q 'persist phase: reuse' "$evidence/B-persist-smoke.txt" || fail 'run B: did not reuse'
! grep -q 'persist phase: create' "$evidence/B-persist-smoke.txt" || fail 'run B: created again'
jq -e '. == [{"name":"polyseat-persist-cpu","status":"Stopped","autostart":"false"}]' \
  "$fixture/var/run/polyseat-unraid/persist-evidence/instances-at-start.json" >/dev/null ||
  fail 'run B: the persisted seat was not found stopped at daemon start'
[[ -f $state/clean-stop.used.json && -f $state/clean-stop.json ]] || fail 'run B: record not consumed and rewritten'
finish_run B
step run-b-reused-nothing-autostarted

# --- run C: no clean-stop record, as after a crash ---
mv -- "$state/clean-stop.json" "$work/clean-stop.json.hidden"
install_run C
rc=0; ctl persist-smoke --approve > "$evidence/C-persist-smoke.txt" 2>&1 || rc=$?
[[ $rc != 0 ]] || fail 'run C: started without a clean-stop record'
grep -q 'recorded no clean stop' "$evidence/C-persist-smoke.txt" || fail 'run C: refused for another reason'
if pgrep -x incusd >/dev/null; then fail 'run C: an Incus daemon was started'; fi
finish_run C
step run-c-refused-without-clean-stop

# --- run D: upstream polyseatd on the persisted Incus, its interface on the LAN address only ---
mv -- "$work/clean-stop.json.hidden" "$state/clean-stop.json"  # the record run B wrote
daemon="$proof/daemon"
(cd "$daemon" && sha256sum --check --strict --quiet SHA256SUMS) || fail 'daemon build checksums'
ip link add br0 type bridge
ip addr add 10.77.0.1/24 dev br0
ip link set br0 up
lan=10.77.0.1
psd="$fixture/mnt/cache/polyseat/polyseatd"
install_run D '["stage","activate","seat-image-stage","daemon-stage","polyseatd-session"]'
ctl daemon-stage "$daemon/polyseatd" --approve >> "$evidence/D-install.txt" 2>&1 || fail 'run D: daemon-stage'
rc=0; ctl polyseatd-session --approve > "$evidence/D-no-password.txt" 2>&1 || rc=$?
[[ $rc != 0 ]] || fail 'run D: started without a password'
grep -q 'no password set' "$evidence/D-no-password.txt" || fail 'run D: refused for another reason than the missing password'
install -d -m 0700 -o 0 -g 0 "$psd"
password=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-20)
# Artifacts lose the executable bit; the controller restores it for polyseatd, this copy does for the tool.
install -m 0755 "$daemon/polyseat-passwd" "$work/polyseat-passwd"
printf '%s\n' "$password" | "$work/polyseat-passwd" "$psd" owner || fail 'run D: password tool'
[[ $(stat -c '%u %a' "$psd/credentials.json") == '0 600' ]] || fail 'run D: credentials mode'
ctl polyseatd-session --approve > "$evidence/D-polyseatd-session.txt" 2>&1 &
session_pid=$!
for _ in $(seq 120); do ctl polyseatd-status > "$evidence/D-status.json" 2>/dev/null && break; kill -0 "$session_pid" 2>/dev/null || break; sleep 1; done
jq -e '.alive and .listeners == ["01004D0A:BAB8"] and .session.setup == false' "$evidence/D-status.json" >/dev/null ||
  fail "run D: polyseatd not serving exactly $lan:47800 with a password set ($(head -c 300 "$evidence/D-status.json"))"
base="https://$lan:47800"
http() { curl -sk --max-time 10 -o "$evidence/D-http-body" -w '%{http_code}' "$@"; }
json=(-H 'Content-Type: application/json')
[[ $(http "$base/api/state") == 401 ]] || fail 'run D: state without login'
[[ $(http "${json[@]}" -X POST -d '{"username":"x","password":"takeover-attempt"}' "$base/api/setup") == 409 ]] || fail 'run D: setup takeover'
[[ $(http "${json[@]}" -X POST -d '{"username":"owner","password":"wrong-password"}' "$base/api/login") == 401 ]] || fail 'run D: wrong login'
[[ $(http "${json[@]}" -c "$work/jar" -X POST -d "{\"username\":\"owner\",\"password\":\"$password\"}" "$base/api/login") == 200 ]] ||
  fail 'run D: login'
[[ $(http -b "$work/jar" "$base/api/state") == 200 ]] || fail 'run D: state after login'
jq -e '.seats == [] and .config.web_update == false and .config.web_uninstall == false and .config.web_lan_bridge == false
  and .config.update_check == false' "$evidence/D-http-body" >/dev/null || fail 'run D: state content'
cp "$evidence/D-http-body" "$evidence/D-state.json"
for action in prepare update restart uninstall lan-bridge; do
  [[ $(http "${json[@]}" -b "$work/jar" -X POST -d '{}' "$base/api/$action") == 403 ]] || fail "run D: /api/$action not locked"
done
[[ $(http "$base/") == 200 ]] || fail 'run D: page'
! curl -sk --max-time 5 -o /dev/null "https://127.0.0.1:47800/api/session" || fail 'run D: also served on localhost'
step run-d-polyseatd-on-the-lan-only
ctl polyseatd-stop >> "$evidence/D-install.txt" 2>&1 || fail 'run D: stop request'
rc=0; wait "$session_pid" || rc=$?
[[ $rc == 0 ]] || fail "run D failed (rc=$rc, see D-polyseatd-session.txt)"
grep -q "^PASS: polyseatd served https://$lan:47800 on the LAN only" "$evidence/D-polyseatd-session.txt" || fail 'run D: no PASS'
[[ ! -e /run/polyseat ]] || fail 'run D: /run/polyseat left behind'
if pgrep -f "$fixture/var/lib/polyseat-unraid-daemon/polyseatd" >/dev/null; then fail 'run D: polyseatd survived'; fi
jq -e '.instances == [{"name":"polyseat-persist-cpu","status":"Stopped","autostart":"false"}]' "$state/clean-stop.json" >/dev/null ||
  fail 'run D: no clean stop recorded'
finish_run D
[[ ! -e $fixture/var/lib/polyseat-unraid-daemon ]] || fail 'run D: uninstall left the staged daemon'
ip link del br0
step run-d-clean-stop

host_mounts_after=$(awk '{print $5}' /proc/self/mountinfo | sort | sha256sum)
[[ $host_mounts_before == "$host_mounts_after" ]] || fail 'mount table changed across the runs'
du -sh "$state" > "$evidence/state-size.txt"
zfs get -H -o property,value used,available cache/polyseat >> "$evidence/dataset.txt"
mountpoint -q -- "$fixture/var/run" && umount -- "$fixture/var/run"
fixture_done=$fixture
fixture=""
step cleanup

jq -n --arg commit "$commit" --arg archive "$archive_sha" --arg seat "$seat_sha" --arg fixture "$fixture_done" \
  --arg controller "$controller_sha" --slurpfile render "$evidence/render.json" --rawfile steps "$evidence/steps.txt" \
  '{schema:1,commit:$commit,archive_sha256:$archive,controller_sha256:$controller,seat_image_sha256:$seat,
    ci_rendering:$render[0],platform:"disposable-linux, real ZFS dataset cache/polyseat",status:"passed",
    steps:($steps|split("\n")|map(select(length>0))),
    caveat:"Persistent-state canary on disposable Linux; not an Unraid test or production approval"}' \
  > "$proof/persist-canary-rehearsal.json"
printf 'PASS: persistent Incus state on a ZFS dataset (fresh, reused without autostart, refused without a clean stop) and polyseatd on the LAN only; NOT Unraid approval.\n' \
  > "$proof/persist-canary-e2e-result.txt"
chmod a+r "$proof/persist-canary-rehearsal.json" "$proof/persist-canary-e2e-result.txt"
passed=1
