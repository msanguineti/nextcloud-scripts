#!/bin/bash
# Shared local/CI entrypoint; skipped tests must not turn a missing dependency green.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."

scripts=(nextcloud-upgrade.sh .github/scripts/*.sh)
for script in "${scripts[@]}"; do bash -n "$script"; done
shellcheck "${scripts[@]}"

php_bin=${TEST_PHP:-php}
"$php_bin" -r '
if (PHP_VERSION_ID < 80400 || !extension_loaded("SimpleXML")) {
    fwrite(STDERR, "Tests require PHP 8.4 or newer with SimpleXML.\n");
    exit(1);
}'
TEST_PHP="$php_bin" python3 -B tests/run.py
