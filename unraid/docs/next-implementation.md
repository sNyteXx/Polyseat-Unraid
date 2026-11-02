# WIP — Next validation milestones

The branch contains the upstream Polyseat 0.44.0 work, a native Incus 7.5.1/LXC 6.0.6 runtime,
a gated controller, Unraid WebUI scaffolding, exact-file transactions and disposable Linux lifecycle tests.
The repository controller stays closed and the installable PLG is not enabled.

## Next milestone: one supervised native CPU-only Tower test

The owner selected the production host for the first bounded compatibility check.
The governing scope and approval protocol are in [production-canary-plan.md](production-canary-plan.md).

Complete the technical preparation before proposing that test:

- Compile the reviewed dependency overlay, retain truthful `v7.5.1+dirty` metadata and verify original Go binaries.
- Inventory both shipped binaries, record all scanner findings and the source-level dispositions.
- Bind the exact runtime, fixed BusyBox image, canary controller, source commit, plan and CI evidence in a verifiable kit.
- Rehearse the actual canary gate on disposable Linux with the real runtime, hard resource limits,
  private namespaces, fresh state, fixed container command flow and lifetime/cleanup checks.
- Read-only Tower preflight, exact first-write/stop/rollback list and explicit owner sign-off.
  Signing custody, public fingerprints, an independent recovery path and a valid short-lived approval are operational gates.

The first Tower test uses fresh RAM-root state, no NIC/GPU/VM/host share and no automatic boot start.
It proves native start/exec/stop behaviour only, not persistence, ZFS, gaming or production readiness.

## Later milestones — not blockers to preparing the first small test

1. Explicitly approved ZFS/persistent-storage test, including idmapping and data retention.
2. Plugin-manager installation/removal, boot ordering and interaction with Unraid `cgroup2-unraid`.
3. Real Unraid update/recovery/rollback with an appropriate maintenance window and rollback evidence.
4. GPU/NVENC/Sunshine/Moonlight and seat lifecycle only after independent GPU ownership and
   interruption risk are agreed. Never rebind the GamingVM's GPU as a side effect of a CPU-only test.
5. Game Companion process integration and UI refinement after the native runtime boundary is validated.

No automatic deployment, release publication, service enablement, GOW removal or change to main.
