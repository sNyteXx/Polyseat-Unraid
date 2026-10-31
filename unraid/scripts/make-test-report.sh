#!/bin/sh
# WIP / DO NOT USE ON UNRAID. CI-only: assemble the CPU-only test artifact directory with a plain-text
# provenance report and SHA-256 checksums. Never signs, publishes or installs anything.
# usage: make-test-report.sh PROOF_DIR OUT_DIR
set -eu

die() { printf 'REPORT BLOCKED: %s\n' "$1" >&2; exit 2; }

[ "$#" -eq 2 ] || die 'usage: make-test-report.sh PROOF_DIR OUT_DIR'
proof=$1
out=$2
[ ! -e /etc/unraid-version ] || die 'never run on Unraid'
[ "${GITHUB_ACTIONS:-}" = true ] || die 'CI only'
[ -f "$proof/e2e-result.txt" ] || die 'the controller end-to-end did not pass; no test artifact is produced'
if [ ! -f "$proof/bundle.env" ] || [ ! -d "$proof/bundle" ]; then die 'bundle missing'; fi
[ ! -e "$out" ] || die 'output directory already exists'

# shellcheck disable=SC1091
. "$proof/bundle.env"
tick=$(printf '\140')
mkdir -p "$out/bundle" "$out/logs" "$out/canary"
cp "$proof/bundle/$BUNDLE_SHA256.tar.gz" "$proof/bundle/$BUNDLE_SHA256.manifest.json" "$out/bundle/"
cp "$proof/abi-report.json" "$proof/build-info.txt" "$proof/e2e-steps.txt" "$proof/e2e-result.txt" "$out/"
if [ -f "$proof/host-state-diff.txt" ]; then cp "$proof/host-state-diff.txt" "$out/"; fi
cp "$proof/SBOM-build-source-only.cdx.json" "$proof/SBOM-runtime.cdx.json" "$out/"
for optional in osv-report.json osv-report.txt SBOM-runtime-osv-triage.cdx.json govulncheck.txt govulncheck-cli.txt \
  incusd-go-buildinfo.txt incus-go-buildinfo.txt incusd-go-packages.txt incus-go-packages.txt \
  dependency-overrides.json busybox-image-provenance.json canary-rehearsal.json canary-e2e-result.txt \
  gobgp-regression.jsonl runtime-provenance.json; do
  if [ -f "$proof/$optional" ]; then cp "$proof/$optional" "$out/"; fi
done
if [ -f "$proof/canary-controller.txz" ]; then
  cp "$proof/canary-controller.txz" "$out/canary/"
fi
if [ -d "$proof/canary-e2e" ]; then
  cp -R "$proof/canary-e2e" "$out/logs/"
fi
if [ -d "$proof/e2e-logs" ]; then cp "$proof"/e2e-logs/* "$out/logs/" 2>/dev/null || true; fi
cp "$GITHUB_WORKSPACE"/polyseat-test-package/*.txz "$out/"
package=$(basename "$(ls "$out"/*.txz)")

{
  printf '# Polyseat for Unraid - CPU-only TEST build (NOT FOR UNRAID PRODUCTION)\n\n'
  printf 'This is a CI test artifact. It is **not** a release, **not** signed with a release key and does **not**\n'
  printf 'certify Unraid compatibility. The release gate is %s in this controller package.\n\n' 'test-host'
  printf '## Provenance\n\n'
  printf -- '- Commit: %s%s%s\n- Run: %s/%s/actions/runs/%s\n- Date (UTC): %s\n' "$tick" \
    "$GITHUB_SHA" "$tick" "${GITHUB_SERVER_URL:-https://github.com}" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '- Runner: %s, kernel %s (AppArmor active, systemd host)\n' \
    "$(awk -F'"' '/^PRETTY_NAME=/ {print $2}' /etc/os-release)" "$(uname -r)"
  printf '\n## Pinned inputs\n\n%s%s%s\n' "$tick" "$tick" "$tick"
  cat "$out/build-info.txt"
  printf '%s%s%s\n\n## Runtime bundle\n\n' "$tick" "$tick" "$tick"
  printf -- '- Archive SHA-256 (also its file name): %s%s%s\n' "$tick" "$BUNDLE_SHA256" "$tick"
  printf -- '- Manifest: %sbundle/%s.manifest.json%s (UNSIGNED: sign it yourself, see below)\n' "$tick" "$BUNDLE_SHA256" "$tick"
  printf -- '- Controller package (gate test-host): %s%s%s\n' "$tick" "$package" "$tick"
  printf -- '- ABI check against the recorded Unraid 7.3.3 baseline: ok=%s, max glibc %s\n' \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ok"])' "$out/abi-report.json")" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["glibc_max"])' "$out/abi-report.json")"
  printf '\n## Supply-chain reports (report only)\n\n'
  printf -- '- SBOM-runtime.cdx.json: exact bundle files with SHA-256, linked Go modules, pinned C sources, host-supplied libraries.\n'
  if [ -f "$out/osv-report.txt" ]; then sed 's/^/    /' "$out/osv-report.txt" | head -n 12; fi
  if [ -f "$out/govulncheck.txt" ]; then printf -- '- govulncheck (binary mode) output is in govulncheck.txt; reachability must be read by a human.\n'; fi
  if [ -f "$out/govulncheck-cli.txt" ]; then printf -- '- CLI binary scan: govulncheck-cli.txt; the runtime inventory covers daemon and CLI.\n'; fi
  if [ -f "$out/canary-rehearsal.json" ]; then
    printf '\n## Separate restricted canary rehearsal\n\n'
    printf 'canary-rehearsal.json binds the exact runtime, source commit and canary controller package.\n'
    printf 'The unsigned package in canary/ is not a host approval, an installer or an Unraid compatibility result.\n'
    printf 'See production-canary-plan.md and the separate artifact-signing/host-approval protocol.\n'
  fi
  printf '\n## Controller end-to-end on a real incusd (disposable Ubuntu runner)\n\n'
  sed 's/^/- PASSED: /' "$out/e2e-steps.txt"
  printf '\nResult line: %s%s%s\n' "$tick" "$(cat "$out/e2e-result.txt")" "$tick"
  if [ -f "$out/host-state-diff.txt" ]; then
    printf '\n## Host-state change measured during the run (runner, before vs after)\n\n'
    head -n 30 "$out/host-state-diff.txt" | sed 's/^/    /'
  fi
  cat <<'TEXT'

## What this does and does not prove

Proven here: signed-bundle staging, host-compat and ABI checks, real `incusd` 7.5.1 with LXC 6.0.6 started without
systemd, unprivileged CPU-only container create/exec/stop/restart, service restart keeping the container, update,
automatic rollback after a failing start, crash recovery, manual rollback, boot rebuild from flash state,
graceful stop, uninstall that keeps settings and data, fail-closed LSM gate.

NOT proven (open gates): Unraid kernel/cgroup2-unraid interplay, idmapped mounts on ZFS/XFS, Unraid boot order,
the Unraid plugin manager and `.plg` handling, the Unraid WebUI rendering, vfat flash behaviour, GPU/streaming.
Ubuntu 24.04 differs from Unraid (glibc 2.39 vs 2.44, systemd, AppArmor, ZFS).

## Using it on a separate, disposable test system only

See unraid/docs/cpu-only-test-plan.md. Sign the manifest with your own throw-away key
(`ssh-keygen -Y sign -n polyseat-unraid-bundle <manifest>`) and put its public line into `allowed_signers`.
TEXT
} > "$out/TEST-REPORT.md"

sums=$(mktemp)
( cd "$out" && find . -type f | sort | sed 's#^\./##' | xargs sha256sum ) > "$sums"
mv "$sums" "$out/SHA256SUMS"
( cd "$out" && sha256sum --check --quiet SHA256SUMS )
printf 'assembled %s\n' "$out"
