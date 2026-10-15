#!/usr/bin/env python3
"""Check progress output and cleanup without running an upgrade."""
import errno
import os
from pathlib import Path
import pty
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
                self.assertNotIn('\r', result.stderr)
                self.assertNotIn('\x1b', result.stderr)

    def run_terminal(self, command, no_color=False, interrupt=None, term='xterm'):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = dict(os.environ, TERM=term)
        env.pop('NO_COLOR', None)
        if no_color:
            env['NO_COLOR'] = '1'
        process = subprocess.Popen(['/bin/bash', '-c', self.prefix + command, 'test', str(self.root)],
                                   stdout=subprocess.PIPE, stderr=slave, text=True, env=env,
                                   start_new_session=True)
        os.close(slave)
        try:
            if interrupt:
                state = self.root / 'state'
                deadline = time.monotonic() + 2
                while not state.exists() or not state.read_text():
                    if time.monotonic() > deadline:
                        self.fail('Progress process did not start')
                    time.sleep(0.01)
                # Let the first frame appear before interrupting the display.
                time.sleep(0.15)
                process.send_signal(interrupt)
            output, _ = process.communicate(timeout=3)
            chunks = []
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return process.returncode, output, b''.join(chunks).decode()
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=3)

    def test_terminal_spinner_preserves_output_and_clears_on_success_or_failure(self):
        for code in (0, 7):
            for no_color in (False, True):
                with self.subTest(code=code, no_color=no_color):
                    command = f'''
with_progress 'Reading archive' bash -c 'printf payload; sleep 0.25; exit {code}' > "$1/output"
printf 'Next step\\n' >&2
'''
                    status, output, display = self.run_terminal(command, no_color)
                    self.assertEqual(status, code, display)
                    self.assertEqual(output, '')
                    self.assertEqual((self.root / 'output').read_text(), 'payload')
                    self.assertIn('[|]', display)
                    self.assertIn('[/]', display)
                    self.assertIn('s elapsed', display)
                    self.assertNotIn('still running', display)
                    self.assertEqual('\x1b[' in display, not no_color)
                    if code == 0:
                        self.assertRegex(display, r'\r +\rNext step\r?\n$')
                    else:
                        self.assertRegex(display, r'\r +\r$')

    def test_dumb_terminal_uses_plain_status_lines(self):
        status, output, display = self.run_terminal('''
with_progress 'Reading archive' sleep 0.12
''', term='dumb')
        self.assertEqual(status, 0, display)
        self.assertEqual(output, '')
        self.assertIn('elapsed; still running', display)
        self.assertNotIn('\x1b', display)
        self.assertNotIn('[|]', display)

    def test_terminal_interrupt_clears_spinner_and_stops_display(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                state = self.root / 'state'
                state.unlink(missing_ok=True)
                status, output, display = self.run_terminal('''
start_progress 'Waiting'
printf '%s' "$progress_pid" > "$1/state"
wait "$progress_pid"
''', no_color=True, interrupt=sig)
                self.assertEqual(status, 128 + sig, display)
                self.assertEqual(output, '')
                self.assertIn('[|]', display)
                self.assertRegex(display, r'\r +\r$')
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(state.read_text()), 0)

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
