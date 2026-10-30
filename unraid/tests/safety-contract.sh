#!/bin/sh
# CI guardrails: never let this WIP silently become an installable Unraid plugin.
set -eu

fail() {
  printf 'SAFETY CHECK FAILED: %s\n' "$1" >&2
  exit 1
}

[ ! -e unraid/plugin/polyseat-unraid.plg ] ||
  fail 'an installable .plg appeared before acceptance gates passed'
[ -f unraid/plugin/polyseat-unraid.plg.disabled ] ||
  fail 'the inert .plg placeholder is missing'

if grep -Eiq '<(INSTALL|REMOVE|EXEC|POSTINSTALL)([[:space:]]|>)' \
    unraid/plugin/polyseat-unraid.plg.disabled; then
  fail 'the disabled placeholder must not contain install/remove actions'
fi

# The build script must refuse to run outside a Linux GitHub Actions runner.
if env CI=false RUNNER_OS=Linux sh unraid/scripts/build-incus-artifact.sh >/dev/null 2>&1; then
  fail 'Incus build unexpectedly ran outside CI'
fi
if env CI=true RUNNER_OS=Windows sh unraid/scripts/build-incus-artifact.sh >/dev/null 2>&1; then
  fail 'Incus build unexpectedly accepted a non-Linux runner'
fi
if env CI=true GITHUB_ACTIONS=true RUNNER_OS=Linux RUNNER_ENVIRONMENT=self-hosted \
    sh unraid/scripts/build-incus-artifact.sh >/dev/null 2>&1; then
  fail 'Incus build unexpectedly accepted a self-hosted runner'
fi
writer_output=$(env CI=false RUNNER_OS=Linux \
  python3 unraid/scripts/make_proof_sbom.py --modules /dev/null \
  --output /tmp/not-a-proof-sbom 2>&1 || true)
printf '%s\n' "$writer_output" | grep -q 'only an isolated Linux CI build' ||
  fail 'proof inventory writer did not explicitly reject non-CI execution'

if env CI=false GITHUB_ACTIONS=false RUNNER_OS=Linux \
    sh unraid/scripts/incus-cpu-smoke.sh >/dev/null 2>&1; then
  fail 'CPU smoke unexpectedly ran outside isolated GitHub Actions'
fi

printf '%s\n' 'WIP install and build guardrails passed.'
