#!/bin/bash
# Called only after CI passes; publication remains a separate human action.
set -euo pipefail
if [[ $# != 1 ]]; then
    printf '%s\n' 'Usage: prepare-release.sh vX.Y.Z' >&2
    exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
tag=$1
# This validates the tag against SCRIPT_VERSION before any GitHub API call.
bash .github/scripts/package-release.sh "$tag" dist
archive="dist/nextcloud-upgrade-$tag.tar.gz"
options=(--draft --verify-tag --title "$tag" --generate-notes)
if [[ $tag == *-* ]]; then options+=(--prerelease); fi
gh release create "$tag" "$archive" "$archive.sha256" "${options[@]}"
