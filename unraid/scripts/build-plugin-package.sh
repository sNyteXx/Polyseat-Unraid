#!/bin/sh
# WIP / DO NOT USE. Builds the controller/WebUI/event package as a deterministic
# Slackware-style .txz. Build host only: refuses on Unraid and never installs anything.
# usage: build-plugin-package.sh VERSION OUTDIR GATE   (GATE: closed | test-host)
set -eu

die() { printf 'BLOCKED: %s\n' "$1" >&2; exit 2; }

[ ! -e /etc/unraid-version ] || die 'never run the package build on Unraid'
[ "$#" -eq 3 ] || die 'usage: build-plugin-package.sh VERSION OUTDIR GATE'
version=$1
outdir=$2
gate=$3

case "$version" in
  [0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]*) ;;
  *) die 'VERSION must look like YYYY.MM.DD[suffix]' ;;
esac
case "$version" in
  *[!0-9A-Za-z.]*) die 'VERSION may contain only digits, letters and dots' ;;
esac
case "$gate" in
  closed | test-host) ;;
  *) die 'GATE must be closed or test-host; "open" is reserved for a separately approved release job' ;;
esac
if [ ! -d "$outdir" ] || [ -L "$outdir" ]; then die 'OUTDIR must be an existing real directory'; fi

here=$(cd "$(dirname "$0")" && pwd)
src="$here/../plugin/src/emhttp"
[ -f "$src/scripts/polyseatctl" ] || die 'plugin sources not found'

package="polyseat-unraid-$version-noarch-1.txz"
[ ! -e "$outdir/$package" ] || die "refusing to overwrite $package"

stage=$(mktemp -d "${TMPDIR:-/tmp}/polyseat-pkg.XXXXXXXX")
case "$stage" in
  */polyseat-pkg.*) ;;
  *) die 'unexpected staging directory' ;;
esac
dest="$stage/usr/local/emhttp/plugins/polyseat-unraid"
mkdir -p "$dest" "$stage/install"
cp -R "$src/." "$dest/"

ctl="$dest/scripts/polyseatctl"
[ "$(grep -c '^POLYSEAT_RELEASE_GATE="closed"' "$ctl")" -eq 1 ] || die 'release gate line not found exactly once'
sed -i "s/^POLYSEAT_RELEASE_GATE=\"closed\"/POLYSEAT_RELEASE_GATE=\"$gate\"/" "$ctl"
grep -q "^POLYSEAT_RELEASE_GATE=\"$gate\"" "$ctl" || die 'could not set the release gate'

find "$dest" -type d -exec chmod 0755 {} +
find "$dest" -type f -exec chmod 0644 {} +
chmod 0755 "$dest/scripts/polyseatctl" "$dest/event/disks_mounted" "$dest/event/stopping_svcs"
printf '%s\n' \
  'polyseat-unraid: Polyseat/Incus runtime controller (WIP, gated)' \
  'polyseat-unraid:' \
  'polyseat-unraid: Experimental. Not for production use.' > "$stage/install/slack-desc"
chmod 0644 "$stage/install/slack-desc"

(
  cd "$stage"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' --format=gnu \
    -cf - install usr | xz -9e -T1 > "$outdir/$package"
)
# Remove only the staging tree created above, after the path guard.
rm -rf "$stage"

sha256sum -- "$outdir/$package" | awk '{print $1 "  '"$package"'"}'
