#!/bin/bash
# shellcheck disable=SC2015  # `check && pass || fail`: pass only prints, fail exits; both outcomes are intended
# Inside the disposable rehearsal container only (see local-steam-rehearsal.sh).
set -euo pipefail
[[ -f /.dockerenv && ! -e /etc/unraid-version ]] || { echo 'container only' >&2; exit 2; }
res=/out/summary.txt; : > "$res"
pass() { printf 'PASS %s\n' "$1" | tee -a "$res"; }
fail() { printf 'FAIL %s\n' "$1" | tee -a "$res"; exit 1; }
trap 'chmod -R a+rX /out 2>/dev/null || true' EXIT
# The controller's exact NIC (polyseatctl STEAM_NIC_PARENT / STEAM_HWADDR).
parent=br0 mac=00:16:3e:50:53:01 c=polyseat-steam-rehearsal

slot=/opt/slot; mkdir -p "$slot"; tar -xzf /in/bundle.tar.gz -C "$slot"
# The bundled liblxc mounts every rootfs here (lxc_rootfs_mount), as the controller prepares it.
mkdir -p /var/lib/polyseat-lxc/rootfs

# Fixture LAN: br0 + DHCP/DNS (dnsmasq) + NAT to this container's uplink, in place of Tower's br0 and router.
ip link add "$parent" type bridge && ip addr add 10.77.0.1/24 dev "$parent" && ip link set "$parent" up
sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -s 10.77.0.0/24 ! -o "$parent" -j MASQUERADE
upstream=$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf)
dnsmasq --interface="$parent" --bind-interfaces --except-interface=lo --dhcp-range=10.77.0.10,10.77.0.50,1h \
  --dhcp-option=option:router,10.77.0.1 --dhcp-option=option:dns-server,10.77.0.1 --no-resolv \
  --server="${upstream:-1.1.1.1}" --pid-file=/run/dnsmasq.pid --log-dhcp --log-facility=/out/dnsmasq.log
pass "fixture LAN: $parent 10.77.0.1/24, dnsmasq DHCP+DNS (upstream ${upstream:-1.1.1.1}), NAT"
# Tower's uplink in br0 is bond0 with 9c:6b:00:1e:07:f8; a bridge without an explicit address takes the lowest port
# MAC (Tower run 9: Incus' random host-veth MAC replaced it). The seat's veth must never change the bridge's MAC.
uplink_mac=9c:6b:00:1e:07:f8
ip link add uplink0 type dummy && ip link set uplink0 address "$uplink_mac" && ip link set uplink0 master "$parent" &&
  ip link set uplink0 up
[[ $(cat "/sys/class/net/$parent/address") == "$uplink_mac" ]] || fail "fixture: $parent did not take the uplink MAC"
members() { find "/sys/class/net/$parent/brif" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null | sort | tr '\n' ' '; }
before=$(members)  # uplink0

# The controller's own deny-tools (its read-only nft stub included, self-tested), first on the daemon's PATH as on Tower.
deny=/var/lib/polyseat-lxc/deny-tools
# shellcheck disable=SC1090  # the real controller function
( source <(sed '$d' /in/emhttp/scripts/polyseatctl); init_paths; canary_deny_tools_prepare ) || fail 'controller deny-tools'
pass "controller deny-tools: $(find "$deny" -type f -printf '%f ' | tr -s ' ')"

mkdir -p /sys/fs/cgroup/daemon
INCUS_DIR=/var/lib/incus-test; export INCUS_DIR
( echo $BASHPID > /sys/fs/cgroup/daemon/cgroup.procs
  exec env -i PATH="$deny:$slot/payload/tools:/usr/sbin:/usr/bin:/sbin:/bin" HOME=/root LANG=C INCUS_DIR="$INCUS_DIR" \
    INCUS_EXEC_PATH="$slot/payload/bin/incusd" INCUS_SKIP_INSTANCE_TYPES=true INCUS_SECURITY_APPARMOR=false \
    "$slot/payload/bin/incusd" --group "" --logfile /out/incusd.log ) > /out/incusd.out 2>&1 &
inc() { env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root INCUS_DIR="$INCUS_DIR" timeout "${T:-120}" "$slot/payload/bin/incus" "$@"; }
for _ in $(seq 60); do [[ -S $INCUS_DIR/unix.socket ]] && inc info >/dev/null 2>&1 && break; sleep 1; done
inc info >/dev/null || fail 'daemon not healthy'
inc storage create default dir >/dev/null; inc profile device add default root disk path=/ pool=default >/dev/null
T=600 inc image import /in/seat.tar.xz --alias polyseat-desktop >/dev/null
# The controller's init for steam-smoke minus the GPU (no NVIDIA here) and minus limits.*: on Tower the canary
# envelope's outer group is the hard limit and Incus skips them there (rehearsed by the CI desktop canary); here
# LXC could not delegate the controllers. The NIC line is byte-identical. Memory is measured outside (docker stats).
T=600 inc init polyseat-desktop "$c" --storage default --config boot.autostart=false --config security.privileged=false \
  --config security.nesting=false >/dev/null
inc config device add "$c" eth0 nic nictype=bridged "parent=$parent" "hwaddr=$mac" name=eth0 >/dev/null
# Persistent Steam storage, as the controller mounts it (on Tower: /mnt/cache/polyseat/steam-seat on cache/polyseat),
# owned by the seat's player (uid 1000 in the container = 1001000 on the host), mode 0700.
st=/srv/polyseat-steam-seat
mkdir -p "$st" && chown 1001000:1001000 "$st" && chmod 0700 "$st"
inc config device add "$c" steam disk "source=$st" path=/home/player/.local/share/Steam >/dev/null
inc image delete polyseat-desktop >/dev/null
inc query "/1.0/instances/$c" > /out/instance-before-start.json
pass 'seat image imported; instance with the controller NIC (bridged br0, fixed MAC)'

T=120 inc start "$c" > /out/start.txt 2>&1 || {
  cp "$INCUS_DIR/logs/$c/lxc.log" /out/lxc-start-failed.log 2>/dev/null || true
  fail "start failed: $(grep -E 'ERROR' "$INCUS_DIR/logs/$c/lxc.log" 2>/dev/null | head -n 3 | tr '\n' '|' | cut -c1-400)"
}
inc query "/1.0/instances/$c" > /out/instance-running.json
jq -r '.config | to_entries[] | select(.key | startswith("volatile.eth0")) | .key' /out/instance-running.json |
  sort > /out/volatile-eth0-keys.txt
pass "running; eth0 volatile keys: $(tr '\n' ' ' < /out/volatile-eth0-keys.txt)"
pass "br0 members while running: $(members)"
veth=$(jq -r '.config["volatile.eth0.host_name"]' /out/instance-running.json)
[[ $(cat "/sys/class/net/$parent/address") == "$uplink_mac" ]] && [[ $(cat "/sys/class/net/$veth/address") == fe:* ]] &&
  pass "bridge MAC unchanged ($uplink_mac); host veth $veth has $(cat "/sys/class/net/$veth/address")" ||
  fail "bridge MAC $(cat "/sys/class/net/$parent/address") (want $uplink_mac), host veth MAC $(cat "/sys/class/net/$veth/address" 2>/dev/null)"
# The controller's Vulkan-manifest push, same flags (neutral target: no NVIDIA driver here to point Vulkan at).
printf '%s\n' '{"file_format_version": "1.0.1", "ICD": {"library_path": "libGLX_nvidia.so.0", "api_version": "1.4.329"}}' > /tmp/icd.json
inc file push -p --uid 0 --gid 0 --mode 0644 /tmp/icd.json "$c/usr/share/polyseat-rehearsal/icd.d/nvidia_icd.json" &&
  [[ $(inc exec "$c" -- stat -c '%u %a' /usr/share/polyseat-rehearsal/icd.d/nvidia_icd.json) == "0 644" ]] &&
  pass 'file push -p --uid 0 --gid 0 --mode 0644 works into the running seat' || fail 'file push'

rc=0
T=1500 inc exec "$c" -- /usr/local/bin/polyseat-selftest steam-cpu > /out/selftest.json 2> /out/selftest.log || rc=$?
for f in screenshot.png screenshot-desktop.png screenshot-steam.png; do
  inc exec "$c" -- cat "/var/lib/polyseat-selftest/$f" > "/out/$f" 2>/dev/null || rm -f "/out/$f"
done
printf 'incus_dir_kb_after_selftest=%s\n' "$(du -sk "$INCUS_DIR" | cut -f1)" | tee /out/memory.txt
jq -r '.checks[] | "\(.status) \(.name): \(.detail)"' /out/selftest.json 2>/dev/null | cut -c1-220 | tee /out/checks.txt
[[ $rc == 0 ]] && jq -e '.verdict == "PASS"' /out/selftest.json >/dev/null && pass 'polyseat-selftest steam-cpu PASS' ||
  { inc stop "$c" --force >/dev/null 2>&1 || true; fail "polyseat-selftest steam-cpu failed (rc=$rc): $(jq -r .reason /out/selftest.json 2>/dev/null)"; }

# M3 session plumbing (software profile here): LAN, Sunshine with this session's credentials, Steam; the PIN path
# reaches Sunshine's API (no Moonlight here, so it must answer that no device is waiting); stop leaves nothing.
T=300 inc exec "$c" -- /usr/local/bin/polyseat-session start cpu > /out/session-start.json 2>/out/session-start.err || true
jq -e '.ok == true and (.ip | startswith("10.77.0.")) and .sunshine == "active" and .steam == "active"' /out/session-start.json >/dev/null &&
  pass "session start: $(jq -c '{ip, sunshine, encoder, steam}' /out/session-start.json)" || fail "session start: $(head -c 300 /out/session-start.json)"
T=60 inc exec "$c" -- /usr/local/bin/polyseat-session pin 0427 > /out/session-pin.json 2>&1 || true
grep -q 'no device is waiting' /out/session-pin.json &&
  pass 'session pin: Sunshine API reached with the session credentials (no device waiting)' || fail "session pin: $(head -c 300 /out/session-pin.json)"
T=60 inc exec "$c" -- /usr/local/bin/polyseat-session pin 042 > /out/session-pin-bad.json 2>&1 || true
grep -q 'four digits' /out/session-pin-bad.json && pass 'session pin: a malformed PIN is refused' || fail 'session pin: malformed PIN'
T=120 inc exec "$c" -- /usr/local/bin/polyseat-session stop > /out/session-stop.json 2>&1 || true
jq -e '.ok == true and .stopped == true' /out/session-stop.json >/dev/null && pass 'session stop: Steam, Sunshine and sway gone, credentials removed' ||
  fail "session stop: $(head -c 300 /out/session-stop.json)"
# Steam's own files must have landed on the host storage, not in the seat's rootfs.
[[ -f $st/.polyseat-seat-runs && -e $st/steam.sh && $(stat -c %u "$st/steam.sh") == 1001000 ]] &&
  pass "Steam's files are on the persistent storage ($(du -sh "$st" | cut -f1), owned by 1001000)" ||
  fail "Steam's files are not on the persistent storage: $(find "$st" -mindepth 1 -maxdepth 1 -printf '%f ' | head -c 200)"
T=60 inc stop "$c" --timeout 30 || T=30 inc stop "$c" --force
after=$(members)
# Errors of the NIC's start/stop (the daemon-start nftables warning about the refused test table is expected).
grep -E 'level=error.*(device=eth0|veth|Failed to stop device|netprio)' /out/incusd.log | cut -c1-300 > /out/incusd-nic-errors.txt || true
[[ ! -s /out/incusd-nic-errors.txt ]] && pass 'stop: no eth0/nft error in incusd.log' || fail "stop: $(head -n1 /out/incusd-nic-errors.txt)"
[[ $after == "$before" ]] && pass "stopped: br0 members back to '${before}' (host veth removed)" || fail "br0 members after stop: '$after' (before '$before')"
T=120 inc delete "$c" >/dev/null
inc admin shutdown >/dev/null 2>&1 || true
echo DONE | tee -a "$res"
