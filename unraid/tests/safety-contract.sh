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

# No installable plugin file may exist anywhere in the repository; only the inert
# placeholder and the non-installable template are allowed.
stray=$(find . -name '*.plg' -not -path './.git/*' -print)
[ -z "$stray" ] || fail "an installable .plg exists in the repository: $stray"
template=unraid/plugin/template/polyseat-unraid.plg.template
[ -f "$template" ] || fail 'the PLG template is missing'
grep -q '@BUNDLE_SHA256@' "$template" ||
  fail 'the PLG template lost its placeholders and might be installable'
grep -q '^POLYSEAT_RELEASE_GATE="closed"' unraid/plugin/src/emhttp/scripts/polyseatctl ||
  fail 'the committed controller must keep its release gate closed'
if grep -Eq 'rm +(-[A-Za-z]*[rR]|--recursive)' unraid/plugin/src/emhttp/scripts/polyseatctl; then
  fail 'the controller must not use recursive deletion'
fi

# The controller must refuse every host mutation on a real root while the gate is closed.
for action in 'activate aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --approve' \
              'uninstall --approve' start stop restart boot 'rollback --approve' 'recover --approve'; do
  # shellcheck disable=SC2086  # the action string is intentionally word-split
  if env -u POLYSEAT_ROOT bash unraid/plugin/src/emhttp/scripts/polyseatctl $action >/dev/null 2>&1; then
    fail "the controller accepted a mutating action on a real root: $action"
  fi
done

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
