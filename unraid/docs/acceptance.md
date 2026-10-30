# 🚧 WIP — DO NOT USE — Acceptance matrix

| Gate | Requirement | Status |
|---|---|---|
| A | Read-only host diagnostics | Implemented, not validated on CI host |
| B | Pinned Incus bundle with reproducible build | TODO |
| C | Unraid lifecycle without systemd | TODO |
| D | Isolated network with no Docker/libvirt impact | TODO |
| E | One seat NVIDIA NVENC and input | TODO |
| F | Two seats and Moonlight 15-minute soak | TODO |
| G | Shared library and per-seat prefixes | TODO |
| H | Boot, upgrade, rollback, uninstall | TODO |
| I | Once Human Proton and anti-cheat | TODO / no compatibility guarantee |

Do not publish a working .plg until gates B–H pass. No destructive migration without separate approval.
