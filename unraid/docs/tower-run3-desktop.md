# Tower run 3 — M1 desktop canary, 2026-10-31: FAILED CLOSED at seat-image staging

Owner GO for exactly: commit `00b83e9b8526620523cd8c818b8936b50e8d3af1` (CI 41202858295, 11/11 green), runtime
`fa62a11969753bdae9f63ea22681e39f9a8fe6fae51c3bf20c604f60d5d4babd`, controller
`5fc50c9f0ccb094dec43c64742300a80e0bfed0b0a87a7ac567e7c08b4795653` (file `5b2960c7…`, gate canary), seat image
`a3fd531914a02593c39251374e0520a3df9d9ab6e5db3135a2402183cca85c6d` (manifest `16915d55…`), signers `SHA256:OiM1…r0D4`
(artifact) / `SHA256:Tglu…Z7g` (approval), token TTL 1500 s (13:05:53–13:30:53 UTC).

| Step (UTC) | Result |
|---|---|
| R0 13:05 | run-2 RAM residue: 78/78 entries verified, removed exactly |
| S0 13:05 | baseline snapshot: no Polyseat path except the flash directory, subuid/subgid empty, GPU on `nvidia`, only UbuntuServer VM running, MemAvailable 18.3 GiB |
| W1 | `allowed_signers` replaced (same artifact key + seat-image namespace, `4ec93ed2…`), token `b6a1e7fc…`; approval signer file unchanged `643f18bf…` |
| W2 | 8/8 hashes OK, controller/bundle/seat-image signatures good |
| W3/W4 | controller file `5b2960c7…`, gate canary once; preflight PASS; `gpu-preflight`: allowed |
| W5/W6 13:06:52 / 13:06:54 | stage + activate OK (activation smoke incl. `unsquashfs`) |
| **W7 13:07:00** | **`seat-image-stage`: `cp: No space left on device` writing `/var/run/polyseat-unraid/seat-image/.incoming`; controller removed the partial file and stopped (EX_SPACE)** |
| W9 13:07:30 | approved rollback: `uninstall` RC=0 (no daemon had started) |
| W10/S1 | no Incus state, slot, LXC path, cgroup or Polyseat process; flash dir: 4 small files; RAM: empty lock + 457 B log. Diff vs S0: only volatile host processes and one more UDP socket, owned by a host service (owner list recorded), plus the expected RAM paths |
| /tmp kits | owner-approved: 47 files (this run and run 2) removed exactly after hash listing |

**Root cause:** Unraid mounts `/run` (= `/var/run`) as its own **128 MiB** tmpfs (`noexec`), and the controller staged the
273 MB image below its run directory. The CI fixture did not model that mount, so the rehearsal could not see it.
The value was in the read-only snapshot and was missed.

**Fix (next candidate):** the seat image is staged in `/var/lib/polyseat-unraid-seat-image` (RAM root, ~37 GiB free),
`seat-image-stage` checks free space on the target filesystem, a test forbids staging below `/run`, and the desktop
rehearsal now mounts the fixture's `/var/run` as a 128 MiB `noexec` tmpfs like Tower. M2 did not run (condition: M1 PASS).
No retry, no host hotfix, no reboot.
