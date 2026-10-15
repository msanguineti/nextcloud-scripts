#!/usr/bin/env python3
"""Check progress output and cleanup without running an upgrade."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'nextcloud-upgrade.sh'


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nc-progress-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = SCRIPT.read_text()
        helpers = source[source.index('log() {'):source.index('fail() {')]
        # Exercise several updates quickly, without adding a runtime test option.
        helpers = helpers.replace('sleep 15 &', 'sleep 0.02 &')
        self.prefix = ('set -Eeuo pipefail\nphase=preflight\nprogress_pid=\'\'\n' + helpers
                       + '\ntrap stop_progress EXIT\ntrap \'exit 143\' TERM\ntrap \'exit 130\' INT\n')

    def test_command_output_and_exit_status_are_preserved(self):
        for code in (0, 7):
            with self.subTest(code=code):
                command = self.prefix + '''
with_progress 'Reading archive' bash -c 'printf payload; sleep 0.12; exit "$1"' test "$2" > "$1/output"
'''
                result = subprocess.run(['/bin/bash', '-c', command, 'test', str(self.root), str(code)],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual((self.root / 'output').read_text(), 'payload')
                self.assertEqual(result.stdout, '')
                self.assertIn('[i] [preflight] Reading archive', result.stderr)
                self.assertIn('elapsed; still running', result.stderr)

    def test_interrupt_stops_the_display_and_its_timer(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                state = self.root / 'state'
                state.unlink(missing_ok=True)
                command = self.prefix + '''
start_progress 'Waiting'
printf '%s' "$progress_pid" > "$1/state"
wait "$progress_pid"
'''
                process = subprocess.Popen(['/bin/bash', '-c', command, 'test', str(self.root)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 2
                    while not state.exists() or not state.read_text():
                        if time.monotonic() > deadline:
                            self.fail('Progress process did not start')
                        time.sleep(0.01)
                    pid = int(state.read_text())
                    process.send_signal(sig)
                    _, errors = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 128 + sig, errors)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
