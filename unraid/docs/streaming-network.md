# WIP — LAN reachability for seats (plan, needs an owner network GO)

Nothing here is implemented on a host. M1/M2 containers have **no NIC at all**.

## Measured (read-only, Tower, 2026-10-31)

- `br0` (192.168.178.24/24, gw .1, static) bridges `bond0` (active-backup, `eth0` 2.5 GbE) and `vnet15`
  (the running UbuntuServer VM). STP off, VLAN filtering off, multicast snooping on.
- `bridge-nf-call-iptables=1`: bridged frames traverse the host's iptables FORWARD chain — the path the VM on
  `vnet15` already uses successfully.
- Docker's custom `br0` network is **ipvlan** (`DOCKER_NETWORK_TYPE=1`, `shim-br0` = ipvlan l2 bridge). A macvlan
  on `br0` is therefore expected to fail (one rx-handler per lower device); not attempted.
- No listener on 47800 or 47984–48010 on the host today (GOW/Wolf not running).

## Options

| Option | How | Host effect | Verdict |
|---|---|---|---|
| **A. Bridged veth on `br0`** (upstream's `nictype=bridged parent=br0`) | each seat gets its own MAC; DHCP lease (or reservation) from the router | a transient `veth` port on `br0` while the seat runs — the same pattern libvirt uses for VMs (`vnet15`); no br0/bond/firewall config change | **recommended** |
| B. ipvlan l2 on `br0` | static IP per seat, shares br0's MAC (like Docker br0 containers) | transient ipvlan child; needs owner-assigned static IPs outside the DHCP range | fallback if A misbehaves |
| C. `proxy` devices on the host IP | forward host ports into the seat | binds Moonlight ports on 192.168.178.24 (collides with GOW/Wolf), loses the client address for UDP, one seat only | rejected |
| D. Managed Incus bridge + NAT (upstream `polyseatbr0`) | — | new bridge + iptables NAT rules | rejected (host firewall change) |

## Port list per seat (own IP, LAN only)

| Port | Proto | Use |
|---|---|---|
| 47984 | TCP | Sunshine HTTPS (paired clients) |
| 47989 | TCP | Sunshine HTTP (pairing, app list) |
| 47990 | TCP | Sunshine web UI — **bound to the seat itself only** (`origin_web_ui_allowed = pc`), never LAN |
| 48010 | TCP | RTSP session setup |
| 47998, 47999, 48000 | UDP | video, control, audio |
| 48002 | UDP | microphone (unused) |
| 5353 | UDP | mDNS discovery (avahi in the seat; optional, clients can add the IP manually) |

`upnp = disabled` in every seat; no router port forwarding, no WAN exposure, no Tailscale/WireGuard exposure.
Pairing requires the 4-digit PIN shown on the Moonlight client, entered by the owner. The host firewall is not
touched: seat traffic is bridged, not routed by Tower.

## Owner decisions needed before M3

1. Option A (DHCP, optionally a router reservation per seat MAC) or B (two static IPs outside the DHCP range).
2. Fixed MAC addresses for seat 1 (Shield) and seat 2 (Tab S9 Ultra) so router reservations stay stable.
3. Test window in which Shield/Tablet can pair and stream; the owner types the PIN.

## M3 acceptance (one session)

Picture + audio + controller input on the Shield for ≥ 10 min at 1080p60 (then 4K60 if the Shield's decoder and
the Wi-Fi/LAN path allow), measured: NVENC utilisation and sessions, host CPU, seat RAM, Moonlight's own stats
(frame drops, network loss, decode latency). Input needs `/dev/uinput` (module present, not loaded today); loading it
is a host change and is part of the M3 GO. Upstream's input broker additionally needs `python3` and `bpftrace`,
which Tower does not have — M3 uses Sunshine's own virtual devices only.
