# 🚧 WIP — DO NOT USE — Next implementation milestones

The branch includes the latest original Polyseat **0.44.0** (12 upstream
commits, merged into the **WIP branch only**) and an isolated **Incus 7.5.1**
build plus **real CPU-only BusyBox start/stop/restart** on a disposable
Ubuntu runner ([green GitHub Actions](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41097086718)).
It also has pure offline VM/GPU/network models, a non-installing filesystem
transaction lab with crash/rollback tests, hostile archive validation,
and a build-source SBOM generator.
The output is **not** an installable Unraid bundle, and the disabled `.plg`
must remain disabled.

1. Keep the pinned C dependencies and Incus 7.5.1 **CPU-only container**
   proof passing in GitHub Actions, separate from any Unraid host.
2. Close and audit ALL dynamic dependencies for a verified Unraid ABI; complete
   the runtime SBOM, software signing/attestations, vulnerability assessment
   and a no-extraction archive verifier.
3. Implement a real supervisor only in a disposable Unraid-compatible VM,
   with root-only socket, strict UID/idmaps and isolated networking. Do not
   modify `br0`, Docker, libvirt or the live gaming VM.
4. Extend the **disposable-only, manifest-journaled filesystem lab** into a
   reviewed candidate for actual persistent storage. It already checks atomic
   switching, recovery and health-check rollback, but lacks Unraid filesystem
   and service integration, bundle signatures and independent path ownership.
5. Test boot, upgrade, failure injection, insufficient storage, malicious
   archives and rollback on disposable hardware; require a human gate.
6. After the gaming VM is no longer an acceptance blocker **or** a second
   physical host-owned NVIDIA GPU is independently verified, perform real
   NVENC/Sunshine/Moonlight one- and two-seat tests on dedicated hardware.

No automatic deployment, service enablement, or GOW removal.
