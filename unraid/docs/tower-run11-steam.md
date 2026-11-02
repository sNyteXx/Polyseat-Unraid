# Tower run 11 — Steam canary, 2026-10-31: Vulkan device found, presentation crashed (no /dev/nvidia-modeset)

Candidate `5c7f9ff` (full lane 41237081233, 13/13). The verified runtime `b59e55cb…` was reused; seat image
`24fa5602…`. Run under the owner's standing GO.

| Step (UTC) | Result |
|---|---|
| pre 20:15 | `gpu-preflight` PASS (MemAvailable 19.7 GiB); run-10 residue `ce078881…` removed exactly; br0 `9c:6b:00:1e:07:f8`, ports `bond0 vnet15` |
| 20:18–20:21 | Same as run 10: DHCP (.149), GL 64/32 bit on the RTX, NVENC, Steam self-updated to "Sign in to Steam" and stopped completely. Vulkan: `vkcube` printed `Selected GPU 0: NVIDIA GeForce RTX 5070 Ti, type: DiscreteGpu, apiVersion 1.4.329`, then segfaulted, on Wayland and on Xwayland alike |
| after | uninstall RC=0; GPU idle on `nvidia`; **`BR0 UNCHANGED`**; no leftover veth. The host diff shows only the known transient UDP count (61 → 60, as in run 8) |

**Cause:** the Vulkan instance and device work, but presentation crashes. NVIDIA's Vulkan WSI on Wayland and X11 goes through NVKMS
(`/dev/nvidia-modeset`), which the seat did not have. In libnvidia-container 1.20.1 that node comes only with the
`display` capability, and upstream seats get `all`.

**Fix:** the Steam seat's capabilities add `display`, which mounts `/dev/nvidia-modeset` and nothing else. M2's `gpu-smoke` stays
without it.
