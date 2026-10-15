#!/bin/bash
# Package an explicit allowlist; development files never enter the download.
set -euo pipefail
umask 022
if [[ $# -lt 1 || $# -gt 2 ]]; then
    printf '%s\n' 'Usage: package-release.sh vX.Y.Z [output-directory]' >&2
    exit 2
fi
tag=$1
[[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$ ]] || {
    printf '%s\n' 'Use a release tag like v0.1.0 or v0.1.0-rc.1.' >&2
    exit 2
}
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
version=$(sed -n "s/^readonly SCRIPT_VERSION='\([^']*\)'$/\1/p" "$source_root/nextcloud-upgrade.sh")
[[ $tag == "v$version" ]] || {
    printf 'Release tag %s does not match script version %s.\n' "$tag" "$version" >&2
    exit 1
}
output=${2:-dist}
package="nextcloud-upgrade-$tag"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -- "$work/$package"
install -m 0755 -- "$source_root/nextcloud-upgrade.sh" "$work/$package/nextcloud-upgrade.sh"
install -m 0644 -- "$source_root/README.md" "$source_root/LICENSE.md" "$work/$package/"
# Stable metadata makes rebuilding the same release produce the same checksum.
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
    -czf "$work/$package.tar.gz" -C "$work" -- "$package"
mkdir -p -- "$output"
cp -- "$work/$package.tar.gz" "$output/$package.tar.gz"
(cd -- "$output" && sha256sum "$package.tar.gz" > "$package.tar.gz.sha256")
printf 'Created %s and its SHA256 checksum in %s\n' "$package.tar.gz" "$output"
