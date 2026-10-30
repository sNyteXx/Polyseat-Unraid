# 🚧 WIP — DO NOT USE — Active gaming VM protection

The existing Windows gaming VM is **running and must stay available**. This
repository's work is restricted to GitHub and offline CI. No action may stop,
pause, reconfigure or inspect the live VM, detach PCI devices, unbind `vfio-pci`,
rebind the NVIDIA driver, reload GPU modules or alter libvirt XML. GOW, Docker,
Unraid `br0`, existing shares and networking are excluded from ownership.

## Feasibility decision

- **One physical GPU exclusively bound to `vfio-pci`: BLOCKED** for simultaneous
  Unraid-host NVENC/Sunshine seats. More CI cannot make that GPU available.
- **Different, host-owned NVIDIA GPU with proven IOMMU separation:** offline
  planning can proceed while the VM remains active, but real NVENC, streams,
  power and resource isolation require separate disposable hardware tests.
- **Uncertain driver owner, VM inventory or IOMMU grouping:** BLOCKED, never
  attempt a rebinding or a VM stop as automatic remediation.
- **No reviewed client-reachable isolated network:** BLOCKED for live streaming.
  The offline plan models `isolated-no-uplink` and cannot stream Moonlight.

## CI evidence versus real acceptance

`unraid/tests/test_lab.py` simulates an active VM owning a `vfio-pci` GPU and
asserts that its ownership never changes. A second mocked GPU may produce a
**non-executable** one-/two-seat proposal. No GPU, Moonlight, Steam, Anti-Cheat
or Unraid host is used by these tests.

Even if the physical VM is eventually shut down voluntarily, the software
must not switch GPU ownership automatically; any hardware reassignment needs
a separately reviewed operator maintenance window and backup plan.
