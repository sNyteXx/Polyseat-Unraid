# Tower run 23 — GPU seat through the admission hook, 2026-11-01: PASS

Step 4 of [upstream-integration-plan.md](upstream-integration-plan.md).

**Candidate:**
- commit `3d72e15`, full lane 41303400975, all jobs green;
- the hook rehearsal there had 17 PASS lines, including case E: `deny`, stale and missing admission refused before the NVIDIA CLI ran.

| | |
|---|---|
| Run | `gpu-smoke` (M2 path): the RTX 5070 Ti by device access. Start and restart both passed the LXC nvidia hook, which now admits a start only on the supervisor's fresh `allow`. Self-test `nvidia` PASS twice; the GPU was released after each stop. **`GPU_RC=0`** |
| After | uninstall RC=0. `/var/lib/polyseat-lxc` (hook and admission file) removed. GPU on `nvidia`, idle. **`BR0 UNCHANGED`**. Host diff: only the expected plugin paths |

**Not covered:** a VM actually taking the GPU on Tower. That refusal is proven with real LXC in CI; no VM was
started here.
