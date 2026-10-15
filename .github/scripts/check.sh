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
TEST_PHP="$php_bin" python3 -B - <<'PY'
import sys
import unittest

suite = unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print('Skipped tests are a failure: install the missing test dependencies.', file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not result.skipped and result.testsRun else 1)
PY
