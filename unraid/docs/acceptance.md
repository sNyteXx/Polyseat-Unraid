# 🚧 WIP — DO NOT USE — Acceptance matrix

Green source CI is **not** Unraid certification. A gate can be marked complete
only with recorded evidence from the relevant disposable test environment.

| Gate | Requirement | Status |
|---|---|---|
| A | Read-only host diagnostics on actual Unraid release | Script present; real host not validated |
| B1 | Immutable Incus/raft/cowsql source pins, Ubuntu build proof | Incus **7.0.2 LTS** passed ([proof](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41088560407)); **7.5.1** selected and requires an independent green run; **not** Unraid runtime evidence |
| B2 | Complete runtime bundle, SBOM, signature/provenance and ABI test | Build-input SBOM generator added; complete runtime closure, signing, audit and Unraid ABI **TODO** |
| C | Non-systemd service lifecycle, boot persistence, UID mapping | Pure start/stop/stage/activate/rollback/uninstall tests; real service/boot **TODO** |
| D | Network isolation without Docker, libvirt or bridge changes | Offline plan rejects `br0`, NAT, host and bridged modes; real reachable isolation **TODO** |
| E | One seat with verified host GPU ownership, NVIDIA NVENC and input | Active VM/vfio conflicts blocked in fixtures; physical GPU/NVENC **TODO** |
| F | Two seats and Moonlight soak test | TODO |
| G | Shared library and per-seat storage on selected filesystem | TODO |
| H | Backup, stage, upgrade, rollback and uninstall without data loss | Pure transition and hostile-archive tests; filesystem transactions, crash recovery and disposable-host trials **TODO** |
| I | Steam/Proton/Once Human and anti-cheat | Unverified; no compatibility guarantee |

There is no installable `.plg` until B2–H have passed. Production deployment
or GOW migration requires a separate, explicit operator decision.

**All status items that mention offline tests are simulator evidence, not
completion of an acceptance gate.** No test has touched the running gaming VM.

The 7.5.1 proof additionally attempts a **no-GPU CLI-only smoke** (`incus` and
`incusd --version`) on the disposable runner. It does not launch the daemon.
