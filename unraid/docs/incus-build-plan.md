# 🚧 WIP — DO NOT USE — Incus build and verification plan

## Principle
Build outside the Unraid host on a disposable Linux runner. Never install distribution packages or run upstream host preparation on Tower. A build is not proof of host compatibility.

## Artifact pipeline
1. Select and pin a specific upstream Incus source tag and immutable commit.
2. Build with documented Go/C toolchain in an isolated CI environment.
3. Produce a versioned archive, SHA-256 manifest, SBOM, and provenance metadata.
4. Scan dependency vulnerabilities and validate artifact contents for symlinks/path traversal.
5. Run isolated Incus unit tests and smoke tests on a disposable compatible Linux VM.
6. Test exact bundle on a disposable Unraid-compatible environment before any host install.
7. Require a manual operator approval for any runtime deployment; no auto-updates.

## Security boundary
Incus is a privileged container manager. A compromised management socket can mean host compromise. Use Unix-socket-only administration with strict permissions; never expose the management API to the LAN by default. Do not use privileged host networking or mount the Docker socket.

## Unraid-specific blockers
Systemd is absent; service supervision must be explicit. Kernel config, idmap mounts, LSM profile availability, GPU device cgroup rules, bridge firewall integration, and persistent storage require functional validation. Existing br0, Docker and libvirt must remain untouched.

## Rollback gate
Before enabling runtime, test clean shutdown, boot recovery, state preservation and removal without deleting any game library or seat. Abort on unknown state.
