# 🚧 WIP — DO NOT USE — Next implementation milestones

The branch contains an isolated **Incus 7.0.2 LTS build proof** that
[passed GitHub Actions](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41088560407).
It also has pure offline simulations for VM/GPU/network safety, lifecycle
transitions and archive validation, plus a build-source SBOM generator.
The output is **not** an installable Unraid bundle, and the disabled `.plg`
must remain disabled.

1. Keep the pinned C dependency and Incus build passing in GitHub Actions.
2. Close and audit ALL dynamic dependencies for a verified Unraid ABI; complete
   the runtime SBOM, software signing/attestations, vulnerability assessment
   and a no-extraction archive verifier.
3. Implement a real supervisor only in a disposable Unraid-compatible VM,
   with root-only socket, strict UID/idmaps and isolated networking. Do not
   modify `br0`, Docker, libvirt or the live gaming VM.
4. Translate the pure lifecycle model into a manifest-journaled filesystem
   implementation with atomic switch, idempotent recovery and backup tests.
5. Test boot, upgrade, failure injection, insufficient storage, malicious
   archives and rollback on disposable hardware; require a human gate.
6. After the gaming VM is no longer an acceptance blocker **or** a second
   physical host-owned NVIDIA GPU is independently verified, perform real
   NVENC/Sunshine/Moonlight one- and two-seat tests on dedicated hardware.

No automatic deployment, service enablement, or GOW removal.
