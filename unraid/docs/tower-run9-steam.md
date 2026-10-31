# Tower run 9 — Steam canary, 2026-10-31: failed closed; br0 MAC side effect (incident, fixed)

Scope: owner GO for Steam tests without asking (2026-10-31): a br0-bridged veth during the test only, RAM only. Commit `d015cb6`
(full lane 41229616936, all six jobs green, steam-rehearsal 9 PASS). Runtime `60a6d48a…`, controller package `8cff022e…`,
seat image `95d0c419…`.

| Step (UTC) | Result |
|---|---|
| pre 18:44 | `gpu-preflight` PASS; run-8 residue `4f1de8f9…` (measured twice) removed exactly |
| 18:47–18:48 | W1–W7 and the GPU preflight PASS; import, start; `steam lan: veth3b6e2d69 in br0 for 00:16:3e:50:53:01` |
| self-test 18:48:22–32 | **on the RTX:** NVIDIA 32 bit libraries injected; LAN by DHCP from the real router (192.168.178.147, DNS ok); systemd, sway, screenshot; Sunshine `h264_nvenc`; **GL 64 and 32 bit on the RTX 5070 Ti**. Then `vulkan: no NVIDIA Vulkan device` (no detail captured) stopped the test before Steam. |
| failure path | instance stopped and deleted; daemon hard stop; uninstall RC=0; GPU on `nvidia`, idle |

## Incident: br0 took the seat veth's MAC for ten minutes

The host diff after the run showed two extra interfaces: `veth3b6e2d69` (in br0) and its peer `vethc6ad790e` (MAC
`00:16:3e:50:53:01`). It also showed **br0 and shim-br0 with MAC `1e:61:f7:02:cc:86` instead of `9c:6b:00:1e:07:f8`**.

- **Bridge MAC:** A Linux bridge without an explicitly set address uses the lowest MAC of its ports. Incus gives the host end of the veth a random MAC; this one (`1e:…`) was below bond0's (`9c:…`), so from 18:48:21 br0 used it. shim-br0 followed, and so did the ipvlan Docker containers on br0.
- **Why the veth stayed:** Incus 7.5.1 only knows nftables. Its bridged NIC stop parses `nft --terse --json -nn list ruleset`; without nft the stop aborts before it removes its veth. The canary's read-only nft stub existed but was never found: the deny-tools lived in `/var/run`, which Unraid mounts noexec, and Go's `exec.LookPath` skips non-executable files.
- **Remediation, 18:58:**
  - The veth pair was proven ours by the peer's seat MAC, by being one pair, and by its host end sitting in br0. Exactly that pair was removed.
  - br0 and shim-br0 returned to `9c:6b:00:1e:07:f8`, with ports `bond0 vnet15`.
  - A fresh snapshot shows links and addresses identical to the baseline.
- **Impact:** IPv4 is static (`USE_DHCP=no`, .24 unchanged). A LAN client (UbuntuDev) holds .24 and .2 at the original MAC and reaches them. No client caches the temporary MAC. Frames to bond0's own MAC stay local to the bridge throughout, so no outage was observable.

## Fixes (owner decision: patch Incus)

- **Incus patch** (`unraid/patches/incus/host-veth-mac.patch`, pinned by SHA-256 in `build-pins.env`, applied with `git apply --check` first, recorded in the proof):
  - the host end of every veth pair gets `fe:` + the peer MAC's last five bytes, as libvirt does for VM taps; fe:… is never the lowest port MAC, so br0 keeps its own;
  - it is set at creation (`RTM_NEWLINK`), before the port joins the bridge;
  - the Steam rehearsal now puts a `9c:6b:00:1e:07:f8` uplink into its fixture bridge and fails if the bridge MAC moves.
- **deny-tools** moved to `/var/lib/polyseat-lxc/deny-tools` (exec-capable, removed exactly on uninstall) with a self-test that fails closed. **Pre-existing since run 2:** on Tower the daemon never saw the stubs and could have found the real `modprobe`, `zfs` or `lvm`. No run's snapshot showed a module change.
- **Vulkan diagnostics:** the self-test keeps up to 60 lines of `vulkaninfo` with the loader's errors and lists `icd.d`. A Vulkan failure no longer hides Steam's own result; it still fails the verdict.
- **Run driver:** the rollback removes a leftover seat veth only with the same ownership proof and reports it loudly, then prints br0's MAC and ports.
