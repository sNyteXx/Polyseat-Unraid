# Tower run 10 — Steam canary, 2026-10-31: Steam runs on the RTX seat; br0 untouched; Vulkan check tool crashed

Candidate: commit `6b0137d`, full lane 41234773126 (all six jobs green). Its CI Steam rehearsal kept the fixture
bridge at `9c:6b:00:1e:07:f8` with that uplink attached, and the host veth was `fe:16:3e:50:53:01`. Artifacts:
runtime `b59e55cb…` with the pinned Incus patch, controller `0d8d1138…`, seat image `ebfa7d9b…`. Run under the
owner's standing GO for Steam tests.

| Step (UTC) | Result |
|---|---|
| pre 19:47 | `gpu-preflight` PASS (VMs off, no holder, MemAvailable 16.3 GiB); run-9 residue `df572b06…` removed exactly; br0 `9c:6b:00:1e:07:f8`, ports `bond0 vnet15` |
| 19:51–19:52 | W1–W7 and the GPU preflight PASS; start; `steam lan: veth81cfb4d5 in br0` |
| self-test 19:52–19:54 | the checks below |
| after | uninstall RC=0; GPU on `nvidia`, idle; **`BR0 UNCHANGED`** (MAC and ports identical before and after); no leftover veth; no network change in the host diff; `/tmp` kit removed exactly |

Self-test, in order:
- **Driver:** NVIDIA 32 bit libraries injected.
- **LAN:** DHCP from the router (192.168.178.148), DNS ok.
- **Desktop:** systemd, sway, screenshot.
- **Sunshine:** `h264_nvenc`.
- **GL:** 64 and 32 bit on the RTX 5070 Ti.
- **Steam:** bootstrapped and self-updated over the LAN; `steamwebhelper` up with **"Sign in to Steam"**; screenshot with 1523 colours. The 32 bit client `steam` and the 64 bit `steamwebhelper` both mapped NVIDIA's GL. The Steam Linux Runtime (`pv-adverb`, `srt-bwrap`) runs in the unprivileged seat without nesting. Steam stopped completely.
- **Vulkan:** **failed**. The driver manifest was in place, but `vulkaninfo` probes `VK_KHR_display`, which needs display control (`/dev/nvidia-modeset`) the seat deliberately lacks. It got `ERROR_UNKNOWN` and then segfaulted.

Resources: the canary group peaked at 7.64 GB of 8 GiB, mostly RAM-resident files (rootfs and Steam's update).
Lowest host MemAvailable was 13.1 GiB. Persistent storage on the cache pool would take most of that out of RAM.

## Fix

The self-test proves Vulkan with `vkcube`:
- 120 frames rendered through a window-system surface on the seat's display, Wayland first and xcb via Xwayland as fallback;
- the device it selected must be NVIDIA;
- no `VK_KHR_display`, so no display control is needed.

The seat keeps no `/dev/nvidia-modeset`.
