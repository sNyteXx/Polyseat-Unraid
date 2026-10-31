# Tower run 12 — Steam canary, 2026-10-31: PASS

Candidate `43780f2`, full lane 41239709843 (all six jobs green). The verified runtime `b59e55cb…` (pinned Incus
patch: fe: host veth) was reused. Controller `0e2a5f87…` with the `display` capability; seat image `90304897…`.
Run under the owner's standing GO for Steam tests: br0 LAN for the test only, RAM only, nobody signs in.

| Step (UTC) | Result |
|---|---|
| pre 20:47 | `gpu-preflight` PASS (VMs off, no holder, MemAvailable 15.5 GiB); run-11 residue `b9d0f931…` removed exactly; br0 `9c:6b:00:1e:07:f8`, ports `bond0 vnet15` |
| 20:51–20:55 | W1–W7 and the GPU preflight PASS; start; `steam lan: veth77aad736 in br0`; self-test **PASS** (below); stop; `released (GPU, veth77aad736)`; delete; daemon stop. **`STEAM_RC=0`** |
| after | uninstall RC=0; GPU on `nvidia`, idle; **`BR0 UNCHANGED`**; no leftover veth; no network change in the host diff. The after snapshot caught the owner backup's transient snapshot mounts; live it was 441 lines with none of ours. `/tmp` kit removed exactly |

## Self-test (all PASS)

| Check | Result |
|---|---|
| nvidia-gpu, nvidia-32bit | RTX 5070 Ti; NVIDIA's 32 bit libraries injected (compat32) |
| lan | eth0 192.168.178.150/24 by DHCP from the LAN router, gateway .1, DNS resolves Valve's update host |
| system, user manager, profile | running; `user@1000`; `nvidia` |
| sway, output, screenshot | GLES2/GBM on the RTX; HEADLESS-1 1280x720 |
| Sunshine | `h264_nvenc`; capture `wlr`; web UI localhost only, no UPnP |
| gl-64, gl-32 | `NVIDIA GeForce RTX 5070 Ti/PCIe/SSE2` in 64 **and** 32 bit |
| vulkan | `vkcube` rendered 120 frames via Wayland on the RTX 5070 Ti (Vulkan 1.4.329) |
| steam-ui, steam-screenshot | Steam bootstrapped and self-updated over the LAN; `steamwebhelper` up with **"Sign in to Steam"**; 1523 colours |
| netns-only | `lo`, the inert `tunl0` and the allowed `eth0`; no other interface |
| steam-stop, stop-session | every Steam process gone; Sunshine and sway gone |

Resources: the canary group peaked at 7.64 GB of 8 GiB, most of it RAM-resident files (rootfs and Steam's update),
because data stays in RAM by owner decision. Lowest host MemAvailable was 9.7 GiB; the watchdog limit is 4 GiB.

## The way here (runs 9–12)

- **Run 9:** br0 adopted the random MAC of Incus' host veth, and the veth stayed after the failure path. This
  exposed that the canary deny-tools never ran on Tower (`/run` is noexec). Fixed by a pinned Incus patch (fe: host
  veth) and by executable, self-tested deny-tools.
- **Run 10:** Steam ran; the vulkaninfo tool crashed on `VK_KHR_display`.
- **Run 11:** vkcube found the RTX but crashed when presenting, because `/dev/nvidia-modeset` was missing. Fixed
  with the `display` capability, which in libnvidia-container 1.20.1 adds only that node; upstream seats get `all`.

## Not covered by this run

- signing in, games and Proton;
- a persistent Steam library and home (RAM only for now);
- Moonlight pairing and streaming to the Shield or the Tab (M3);
- gamescope, Big Picture, input;
- the upstream daemon.
