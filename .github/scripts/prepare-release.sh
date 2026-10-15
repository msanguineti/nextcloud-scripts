#!/bin/bash
# Manual main-only release preparation. Never writes commits or publishes releases.
set -euo pipefail
fail() { printf '%s\n' "$*" >&2; exit 1; }
if [[ $# != 1 || ( $1 != check && $1 != create ) ]]; then
    printf '%s\n' 'Usage: prepare-release.sh check|create (GitHub Actions only)' >&2
    exit 2
fi
mode=$1
[[ ${GITHUB_EVENT_NAME:-} == workflow_dispatch && ${GITHUB_REF:-} == refs/heads/main ]] || fail 'Run Prepare release manually from main.'
[[ ${GH_REPO:-} =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail 'GH_REPO must identify the release repository.'
[[ ${GITHUB_SHA:-} =~ ^[0-9a-f]{40}$ ]] || fail 'GITHUB_SHA must identify the tested commit.'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
commit=$(git rev-parse --verify HEAD)
[[ $commit == "$GITHUB_SHA" ]] || fail 'Checkout differs from the commit selected for this release.'
git diff --quiet HEAD -- nextcloud-upgrade.sh README.md LICENSE.md .github || fail 'Release files differ from the selected commit.'
version=$(sed -n "s/^readonly SCRIPT_VERSION='\([^']*\)'$/\1/p" nextcloud-upgrade.sh)
tag="v$version"
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]] || fail 'Invalid SCRIPT_VERSION.'

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
require_absent() {
    local endpoint=$1 label=$2 status
    if gh api --include --silent "$endpoint" > "$work/response" 2> "$work/error"; then
        fail "$label already exists; choose a new SCRIPT_VERSION."
    fi
    status=$(awk '/^HTTP\// {print $2; exit}' "$work/response")
    if [[ $status != 404 ]]; then
        cat "$work/error" >&2
        fail "Cannot check whether $label exists (HTTP ${status:-unknown}); no release was created."
    fi
}
# Repeat these checks in the write job, after tests. Only an explicit 404 is absence.
require_absent "repos/$GH_REPO/git/ref/tags/$tag" "Tag $tag"
# List releases too: a manually prepared draft can exist before its tag does.
if ! existing=$(gh api --paginate "repos/$GH_REPO/releases?per_page=100" \
    --jq ".[] | select(.tag_name == \"$tag\") | .tag_name"); then
    fail 'Cannot inspect existing releases; no release was created.'
fi
[[ -z $existing ]] || fail "Release $tag already exists; choose a new SCRIPT_VERSION."
if [[ $mode == check ]]; then
    if [[ -n ${GITHUB_OUTPUT:-} ]]; then
        printf 'commit=%s\n' "$commit" >> "$GITHUB_OUTPUT"
    fi
    printf 'Ready to prepare %s from %s.\n' "$tag" "$commit"
    exit 0
fi

# Build successfully before making any remote change. Create the ref atomically;
# GitHub rejects a concurrent attempt to create an already existing tag.
bash .github/scripts/package-release.sh "$tag" dist
gh api --method POST "repos/$GH_REPO/git/refs" \
    -f "ref=refs/tags/$tag" -f "sha=$commit" --silent
archive="dist/nextcloud-upgrade-$tag.tar.gz"
options=(--draft --verify-tag --title "$tag" --generate-notes)
if [[ $tag == *-* ]]; then options+=(--prerelease); fi
if ! gh release create "$tag" "$archive" "$archive.sha256" "${options[@]}"; then
    fail "Tag $tag was created, but draft preparation failed. Inspect the unpublished tag/draft before retrying."
fi
printf 'Draft %s is ready for review; publish it from GitHub Releases.\n' "$tag"
