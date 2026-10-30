# 🚧 WIP — DO NOT USE — Acceptance matrix

Green source CI is **not** Unraid certification. A gate can be marked complete
only with recorded evidence from the relevant disposable test environment.

| Gate | Requirement | Status |
|---|---|---|
| A | Read-only host diagnostics on actual Unraid release | Script present; real host not validated |
| B1 | Immutable Incus/raft/cowsql source pins, Ubuntu build proof | CI implementation; completion must be checked in Actions |
| B2 | Complete runtime bundle, SBOM, signature/provenance and ABI test | TODO — not the same as B1 |
| C | Non-systemd service lifecycle, boot persistence, UID mapping | TODO |
| D | Network isolation without Docker, libvirt or bridge changes | TODO |
| E | One seat with verified host GPU ownership, NVIDIA NVENC and input | TODO |
| F | Two seats and Moonlight soak test | TODO |
| G | Shared library and per-seat storage on selected filesystem | TODO |
| H | Backup, stage, upgrade, rollback and uninstall without data loss | Design only; implementation and tests TODO |
| I | Steam/Proton/Once Human and anti-cheat | Unverified; no compatibility guarantee |

There is no installable `.plg` until B2–H have passed. Production deployment
or GOW migration requires a separate, explicit operator decision.
