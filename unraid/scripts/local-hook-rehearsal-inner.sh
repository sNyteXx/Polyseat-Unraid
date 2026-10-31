#!/bin/bash
# shellcheck disable=SC2015  # `check && pass || fail`: pass only prints, fail exits; both outcomes are intended
# Inside the disposable rehearsal container only (see local-hook-rehearsal.sh).
set -euo pipefail
[[ -f /.dockerenv && ! -e /etc/unraid-version ]] || { echo 'container only' >&2; exit 2; }
res=/out/summary.txt; : > "$res"
pass() { printf 'PASS %s\n' "$1" | tee -a "$res"; }
trap 'chmod -R a+rX /out 2>/dev/null || true' EXIT
fail() { printf 'FAIL %s\n' "$1" | tee -a "$res"; exit 1; }

# Pinned runtime, extracted exactly as a slot (the hook comes from the bundle, the wrapper from the controller).
slot=/opt/slot; mkdir -p "$slot"; tar -xzf /in/bundle.tar.gz -C "$slot"
mkdir -p /var/lib/polyseat-lxc/rootfs
# Real controller function: copies wrapper + verbatim hook, creates the log directory for the container root uid.
manifest=/tmp/manifest.json
jq -n --arg h "$(sha256sum "$slot/payload/share/lxc/hooks/nvidia" | cut -d' ' -f1)" \
  '{files:[{path:"payload/share/lxc/hooks/nvidia",mode:493,sha256:$h}]}' > "$manifest"
# shellcheck disable=SC1090,SC2034  # the real controller functions; both paths are read by lxc_hook_prepare
( source <(sed '$d' /in/emhttp/scripts/polyseatctl); init_paths; LXC_HOOK_WRAPPER=/in/emhttp/scripts/lxc-nvidia-hook
  LXC_HOOK_CLI_SHIM=/in/emhttp/scripts/lxc-nvidia-cli; lxc_hook_prepare "$manifest" "$slot" ) || fail 'controller lxc_hook_prepare'
pass "hooks prepared: $(stat -c '%a %U' /var/lib/polyseat-lxc/hooks/nvidia /var/lib/polyseat-lxc/hook-log | tr '\n' ' ')"

# Stub nvidia-container-cli: behaviour from a root-owned, world-readable control file.
mkdir -p /stub /stubctl; chmod 0755 /stubctl
cat > /stub/nvidia-container-cli <<'STUB'
#!/bin/bash
d=/var/lib/polyseat-lxc/hook-log
printf '%s\n' "$@" > "$d/stub-argv"; id > "$d/stub-id"
cat /stubctl/msg >&2
exit "$(cat /stubctl/rc)"
STUB
chmod 0755 /stub/nvidia-container-cli

# The daemon in a leaf cgroup without delegated controllers, as under the Tower canary envelope.
mkdir -p /sys/fs/cgroup/daemon
INCUS_DIR=/var/lib/incus-test; export INCUS_DIR
( echo $BASHPID > /sys/fs/cgroup/daemon/cgroup.procs
  exec env -i PATH="$slot/payload/tools:/stub:/usr/sbin:/usr/bin:/sbin:/bin" HOME=/root LANG=C INCUS_DIR="$INCUS_DIR" \
    INCUS_EXEC_PATH="$slot/payload/bin/incusd" INCUS_LXC_HOOK=/var/lib/polyseat-lxc/hooks \
    INCUS_SKIP_INSTANCE_TYPES=true INCUS_SECURITY_APPARMOR=false \
    "$slot/payload/bin/incusd" --group "" --logfile /out/incusd.log ) > /out/incusd.out 2>&1 &
inc() { env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root INCUS_DIR="$INCUS_DIR" timeout 120 "$slot/payload/bin/incus" "$@"; }
for _ in $(seq 60); do [[ -S $INCUS_DIR/unix.socket ]] && inc info >/dev/null 2>&1 && break; sleep 1; done
inc info >/dev/null || fail 'daemon not healthy'
inc storage create default dir >/dev/null; inc profile device add default root disk path=/ pool=default >/dev/null
inc image import "$slot/payload/share/canary/busybox.tar.gz" --alias bb >/dev/null
inc init bb c1 --config security.privileged=false --config nvidia.runtime=true \
  --config nvidia.driver.capabilities=compute,utility,video,graphics >/dev/null
pass 'daemon up, unprivileged container with nvidia.runtime=true (no GPU device)'
log=/var/lib/polyseat-lxc/hook-log
attempt() { # label rc msg -> start exit code
  printf '%s\n' "$2" > /stubctl/rc; printf '%s\n' "$3" > /stubctl/msg
  rm -f "$log"/{env.txt,hook.out,status,stub-argv,stub-id}
  # A failed start can leave Incus believing the instance runs (its aborted LXC monitor); reset first and wait
  # until Incus reports it stopped (CI run 41238762803: case B hit "The instance is already running").
  inc stop c1 --force >/dev/null 2>&1 || true
  local _
  for _ in $(seq 30); do
    [[ $(inc query /1.0/instances/c1/state 2>/dev/null | jq -r .status) == Stopped ]] && break
    inc stop c1 --force >/dev/null 2>&1 || true; sleep 1
  done
  local rc=0; inc start c1 > "/out/start-$1.txt" 2>&1 || rc=$?
  cp "$INCUS_DIR/logs/c1/lxc.log" "/out/lxc-$1.log" 2>/dev/null || true
  for f in env.txt hook.out status stub-argv stub-id; do cp "$log/$f" "/out/$1-$f" 2>/dev/null || true; done
  return "$rc"
}

# A: the run-5 failure, reproduced: hook unreachable for the container root uid (0700 on the path).
chmod 0700 /var/lib/polyseat-lxc/hooks
if attempt A-unreachable 0 ok; then fail 'A: start succeeded with an unreachable hook'; fi
grep -q 'Script exited with status 126' /out/lxc-A-unreachable.log && pass 'A: 0700 hook path -> "Script exited with status 126" (Tower run 5 reproduced)' ||
  fail 'A: expected status 126'
chmod 0755 /var/lib/polyseat-lxc/hooks

# B: the CLI succeeds -> the container starts; the wrapper recorded LXC's real hook context.
attempt B-ok 0 'stub: configured' || fail 'B: start failed with a succeeding CLI'
grep -Eq '^uid_map= ?0 1000000 1000000000;' /out/B-ok-env.txt && pass 'B: hook ran as container root in its user namespace (uid_map 0 1000000)' || fail 'B: uid map'
grep -q '^LXC_HOOK_TYPE=mount' /out/B-ok-env.txt && grep -q '^NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics' /out/B-ok-env.txt &&
  pass 'B: LXC hook environment as expected' || fail 'B: environment'
for a in --user configure --no-cgroups --compute --utility --video --graphics /var/lib/polyseat-lxc/rootfs; do
  grep -qxF -- "$a" /out/B-ok-stub-argv || fail "B: argument $a missing"
done
# Tower run 7: pivot_root fails on Unraid's initramfs root; the shim must put --no-pivot first (a global option).
[[ $(head -n1 /out/B-ok-stub-argv) == --no-pivot ]] && grep -q '^POLYSEAT_NVC_REAL=/stub/nvidia-container-cli$' /out/B-ok-env.txt &&
  pass 'B: the CLI shim ran the real CLI with --no-pivot first' || fail 'B: --no-pivot via the CLI shim'
grep -q -- '^--debug=/var/lib/polyseat-lxc/hook-log/nvidia-container-cli.log$' /out/B-ok-stub-argv &&
  grep -q -- '^--ldconfig=@' /out/B-ok-stub-argv && pass "B: CLI argv $(tr '\n' ' ' < /out/B-ok-stub-argv)" || fail 'B: debug/ldconfig args'
grep -qx 0 /out/B-ok-status && pass 'B: status 0 recorded' || fail 'B: status'
inc stop c1 --force >/dev/null

# C: the CLI fails like on Tower run 6 -> start fails; the error text survives in the wrapper log before deletion.
if attempt C-cli-fails 1 'nvidia-container-cli: mount error: stub failure for the rehearsal'; then fail 'C: start succeeded'; fi
grep -q 'Script exited with status 1' /out/lxc-C-cli-fails.log && pass 'C: LXC reports only "status 1" (as on Tower)' || fail 'C: lxc.log'
grep -q 'stub failure for the rehearsal' /out/C-cli-fails-hook.out && grep -qx 1 /out/C-cli-fails-status &&
  pass 'C: the CLI error text and exit status are in the wrapper log' || fail 'C: wrapper log'
# D (optional): NVIDIA's real nvidia-container-cli 1.20.1 in the same hook context, without any driver. It must be
# reached, its own error must be captured, and its debug log must open at the writable path (1.20.1 asserts that).
if [[ -x /realcli/usr/bin/nvidia-container-cli ]]; then
  cp -a /realcli/usr/lib/x86_64-linux-gnu/libnvidia-container* /usr/lib/x86_64-linux-gnu/ && ldconfig
  install -m 0755 /realcli/usr/bin/nvidia-container-cli /stub/nvidia-container-cli
  if attempt D-real-cli 0 unused; then fail 'D: start succeeded without any NVIDIA driver'; fi
  cp "$log/nvidia-container-cli.log" /out/D-real-cli-debug.log 2>/dev/null || true
  grep -q 'Script exited with status 1' /out/lxc-D-real-cli.log && grep -qx 1 /out/D-real-cli-status &&
    grep -q '^nvidia-container-cli: ' /out/D-real-cli-hook.out &&
    pass "D: real CLI reached as container root; its error captured: $(grep '^nvidia-container-cli: ' /out/D-real-cli-hook.out | head -n1)" ||
    fail 'D: real CLI error not captured'
  [[ -s /out/D-real-cli-debug.log ]] && pass "D: CLI debug log opened at the writable path ($(wc -c < /out/D-real-cli-debug.log) bytes)" ||
    fail 'D: CLI debug log missing'
fi
inc delete c1 --force >/dev/null 2>&1 || true
[[ -s /out/C-cli-fails-hook.out ]] && pass 'C: evidence kept after the instance was deleted' || fail 'C: evidence after delete'
inc admin shutdown >/dev/null 2>&1 || true
echo DONE | tee -a "$res"
