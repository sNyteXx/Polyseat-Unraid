#!/bin/sh
# WIP / DO NOT USE. Renders the PLG template into a file OUTSIDE this repository.
# Never run by CI on pushes; reserved for a separately approved release job.
# usage: render-plg.sh VERSION BASE_URL PACKAGE.txz BUNDLE_SHA256 BUNDLE_DIR SIGNER_PUB OUTFILE
set -eu

die() { printf 'BLOCKED: %s\n' "$1" >&2; exit 2; }

[ ! -e /etc/unraid-version ] || die 'never render on Unraid'
[ "$#" -eq 7 ] || die 'usage: render-plg.sh VERSION BASE_URL PACKAGE.txz BUNDLE_SHA256 BUNDLE_DIR SIGNER_PUB OUTFILE'
version=$1 base=$2 package=$3 bundle=$4 bundle_dir=$5 signer=$6 out=$7

case "$version" in
  [0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]*) ;;
  *) die 'bad VERSION' ;;
esac
case "$version" in *[!0-9A-Za-z.]*) die 'bad VERSION characters' ;; esac
case "$base" in
  https://*) ;;
  *) die 'BASE_URL must be https' ;;
esac
case "$base" in
  *[!A-Za-z0-9._~:/%+-]*) die 'BASE_URL contains characters that are unsafe in the plugin file' ;;
esac
case "$bundle" in *[!0-9a-f]*) die 'bundle digest must be lowercase hex' ;; esac
[ "${#bundle}" -eq 64 ] || die 'bundle digest must be 64 hex characters'
for f in "$package" "$bundle_dir/$bundle.manifest.json" "$bundle_dir/$bundle.manifest.json.sig" "$signer"; do
  [ -f "$f" ] && [ ! -L "$f" ] || die "missing input file: $f"
done

# Never write an installable plugin into the working tree.
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || true)
out_dir=$(cd "$(dirname "$out")" && pwd -P) || die 'output directory does not exist'
if [ -n "$repo" ]; then
  case "$out_dir/" in
    "$repo"/*) die 'refusing to write a plugin file inside the repository' ;;
  esac
fi
[ ! -e "$out" ] || die 'refusing to overwrite the output file'

pkg_sha=$(sha256sum -- "$package" | awk '{print $1}')
man_sha=$(sha256sum -- "$bundle_dir/$bundle.manifest.json" | awk '{print $1}')
sig_sha=$(sha256sum -- "$bundle_dir/$bundle.manifest.json.sig" | awk '{print $1}')
signer_line=$(awk 'NF>=2 {printf "polyseat-unraid-release namespaces=\"polyseat-unraid-bundle\" %s %s", $1, $2; exit}' "$signer")
[ -n "$signer_line" ] || die 'signer public key is empty'

template="$(dirname "$0")/../plugin/template/polyseat-unraid.plg.template"
sed -e "s|@VERSION@|$version|g" \
    -e "s|@RELEASE_BASE_URL@|$base|g" \
    -e "s|@SUPPORT_URL@|$base|g" \
    -e "s|@PACKAGE_SHA256@|$pkg_sha|g" \
    -e "s|@BUNDLE_SHA256@|$bundle|g" \
    -e "s|@MANIFEST_SHA256@|$man_sha|g" \
    -e "s|@SIGNATURE_SHA256@|$sig_sha|g" \
    -e "s|@SIGNER_LINE@|$signer_line|g" \
    "$template" > "$out"
if grep -q '@[A-Z][A-Z0-9_]*@' "$out"; then
  die 'unreplaced placeholder left in the rendered plugin'
fi
printf 'rendered %s\n' "$out"
