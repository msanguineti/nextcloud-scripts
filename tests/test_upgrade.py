#!/usr/bin/env python3
"""End-to-end failure injection. No root, network, services or real database used.

The script copy changes only the root check, global lock and workspace paths. All production
interfaces are guarded doubles; archive/filesystem operations and PHP helpers
are real. Set TEST_PHP to a PHP executable with SimpleXML (defaults to php).
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'nextcloud-upgrade.sh'
REAL_PHP = shutil.which(os.environ.get('TEST_PHP', 'php'))
REAL_TOOLS = {name: shutil.which(name) for name in ('stat', 'mv', 'cp', 'tar', 'gzip', 'df')}

MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(os.environ['TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
mode = os.environ.get('FAIL_AT', '')
# Everything this double reads/writes is scoped to the isolated fixture.
def load(key): return json.loads((root / key).read_text())
def save(key, val): (root / key).write_text(json.dumps(val))
def event(action):
    with (root / 'events').open('a') as f: f.write(action + '\n')
def die(code=1): sys.exit(code)
def fail_once(point):
    if mode == point and not (root / ('failed-' + point)).exists():
        (root / ('failed-' + point)).touch()
        die(23)
if name == 'sudo':
    assert args[:3] == ['-u', 'www-data', 'php'], args
    assert args[3] == './occ', args
    assert pathlib.Path.cwd() == root / 'nextcloud'
    a = args[4:]
    event('occ ' + ' '.join(a[:1]))
    s = load('status')
    if a[0] == 'status':
        if mode == 'postcheck' and s['version'] != '34.0.0.0': die(24)
        print(json.dumps(s))
    elif a[0] == 'config:system:get':
        values = {'version': '34.0.0.0', 'datadirectory': str(root / 'storage'),
                  'dbtype': os.environ.get('DB_TYPE', 'mysql'), 'dbname': 'nextcloud',
                  'dbuser': 'nc', 'dbpassword': 'secret#"\\value',
                  'dbhost': os.environ.get('DB_HOST', 'localhost'), 'updater.release.channel': 'stable'}
        if a[1] not in values: die(1)
        print(values[a[1]])
    elif a[0] == 'config:list':
        print(json.dumps({'system': {'apps_paths': [{'path': str(root / 'nextcloud/apps')}, {'path': str(root / 'nextcloud/custom_apps')}]}}))
    elif a[0] == 'maintenance:mode':
        if a[1] == '--on': fail_once('maintenance-on')
        if a[1] == '--off': fail_once('maintenance-off')
        s['maintenance'] = a[1] == '--on'; save('status', s)
    elif a[0] == 'upgrade':
        assert load('services') == {'nginx': 'inactive', 'php8.4-fpm': 'inactive'}
        assert '# nextcloud-upgrade:' in (root / 'cron').read_text()
        s['version'] = '34.0.1.0'
        if mode in ('migration', 'signal-migration', 'hup-migration', 'kill-migration'):
            s['needsDbUpgrade'] = True
        save('status', s)
        if mode == 'signal-migration': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGTERM); time.sleep(.2); die(143)
        if mode == 'hup-migration': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGHUP); time.sleep(.2); die(129)
        if mode == 'kill-migration': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGKILL); die(137)
        if mode == 'migration': print('migration failed'); die(42)
        if mode == 'external-cron': (root / 'cron').write_text((root / 'cron').read_text() + '* * * * * echo new-job\n')
        print('Upgrade successful')
    else: raise AssertionError(a)
elif name == 'php':
    os.execv(os.environ['REAL_PHP'], [os.environ['REAL_PHP'], *args])
elif name == 'curl':
    event('curl')
    if mode == 'download': die(22)
    url = next(a for a in args if a.startswith('https:'))
    if 'updater_server' in url:
        if mode == 'malformed-xml': print('<html>bad</html>'); die(0)
        if mode == 'empty-xml': die(0)
        if mode == 'no-update': print('<nextcloud/>'); die(0)
        if mode == 'manual-only': print('<nextcloud><version>34.0.1.0</version><autoupdater>0</autoupdater></nextcloud>'); die(0)
        if mode == 'skip-major': print('<nextcloud><version>36.0.0.0</version></nextcloud>'); die(0)
        print('<nextcloud><version>34.0.1.0</version><autoupdater>1</autoupdater></nextcloud>')
    else:
        dest = pathlib.Path(args[args.index('-o') + 1])
        assert str(dest).startswith(str(root / 'var-tmp/nextcloud-upgrade.')), dest
        suffix = url.rsplit('/', 1)[1]
        if suffix.endswith(('.sha256', '.sha512', '.md5')):
            archive = suffix.rsplit('.', 1)[0]
            alg = suffix.rsplit('.', 1)[1]
            import hashlib
            data = (root / archive).read_bytes()
            digest = hashlib.new(alg, data).hexdigest()
            if mode == 'checksum': digest = '0' * len(digest)
            dest.write_text(digest + '  ' + archive + '\n')
        else:
            import shutil; shutil.copyfile(root / suffix, dest)
elif name == 'monit' or (name == 'systemctl' and args[0] in ('start', 'stop')):
    if name == 'monit':
        assert os.environ.get('SERVICE_MANAGER', 'monit') == 'monit', 'Wrong service backend'
        if args[0] == '-B':
            event('monit status ' + args[-1])
            print('Monitoring status ' + ('Not monitored' if mode == 'unmonitored' else 'Monitored'))
            die(0)
    else:
        assert os.environ.get('SERVICE_MANAGER') == 'systemd', 'Wrong service backend'
    action, svc = args
    event(name + ' ' + action + ' ' + svc)
    assert svc in ('nginx', 'php8.4-fpm')
    if action == 'stop': fail_once('stop-' + svc)
    if action == 'start': fail_once('start-' + svc)
    state = load('services')
    expected = 'active' if action == 'start' else 'inactive'
    state[svc] = expected
    if name == 'monit' and os.environ.get('MONIT_DEPENDENCY') == 'yes':
        if action == 'stop' and svc == 'nginx':
            state['php8.4-fpm'] = 'inactive'; event('dependency stop php8.4-fpm')
        if action == 'start' and svc == 'php8.4-fpm':
            state['nginx'] = 'active'; event('dependency start nginx')
    if mode == 'stop-timeout' and action == 'stop': state[svc] = 'active'
    if mode == 'start-timeout' and action == 'start': state[svc] = 'inactive'
    if os.environ.get('SERVICE_DELAY') == 'yes':
        state[svc] = 'activating' if action == 'start' else 'deactivating'
        save('pending-' + svc, [expected, 1])
    save('services', state)
elif name == 'systemctl':
    assert args[0] == 'show'
    if '--property=LoadState' in args:
        print('not-found' if mode == 'missing-unit' else 'loaded')
    else:
        svc = args[1]
        pending = root / ('pending-' + svc)
        if pending.exists():
            target, remaining = load('pending-' + svc)
            if remaining == 0:
                state = load('services'); state[svc] = target; save('services', state)
                pending.unlink()
            else: save('pending-' + svc, [target, remaining - 1])
        print(load('services')[svc])
elif name == 'crontab':
    assert args[:2] == ['-u', 'www-data']
    if args[2] == '-l':
        if mode == 'cron-unreadable': die(1)
        sys.stdout.write((root / 'cron').read_text())
    else:
        src = pathlib.Path(args[2]); assert str(src).startswith((str(root / 'var-tmp/nextcloud-upgrade.'), str(root / 'backups')))
        event('cron write')
        fail_once('cron-write')
        (root / 'cron').write_bytes(src.read_bytes())
elif name in ('mariadb', 'mysql', 'psql'):
    event('db-connect')
    if mode == 'db-connect': die(25)
    if name != 'psql':
        cred = pathlib.Path(args[0].split('=', 1)[1])
        assert cred.stat().st_mode & 0o777 == 0o600
        text = cred.read_text()
        assert 'password="secret#\\"\\\\value"' in text, text
        expected = os.environ.get('EXPECT_DB_OPTION')
        if expected: assert expected in text, text
    print(1000)
elif name in ('mariadb-dump', 'mysqldump', 'pg_dump'):
    event('dump')
    assert load('status')['maintenance']
    assert load('services') == {'nginx': 'inactive', 'php8.4-fpm': 'inactive'}
    assert '# nextcloud-upgrade:' in (root / 'cron').read_text()
    if mode == 'signal-backup': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGINT); time.sleep(.2); die(130)
    if mode == 'backup': print('partial sql'); die(27)
    dump = 'CREATE TABLE test(id int);\nINSERT INTO test VALUES(1);\n'
    if mode != 'missing-footer': dump += '-- Dump completed on 2026-10-14\n' if name != 'pg_dump' else '-- PostgreSQL database dump complete\n'
    if name == 'pg_dump': pathlib.Path(next(a.split('=', 1)[1] for a in args if a.startswith('--file='))).write_text(dump)
    else: sys.stdout.write(dump)
elif name == 'findmnt':
    print('/')
    if mode == 'mount': print(str(root / 'nextcloud/data'))
elif name == 'pgrep':
    if mode == 'background': print('1234'); die(0)
    die(1)
elif name == 'chown':
    assert str(root / '.nextcloud-stage.') in args[-1], args
elif name == 'stat':
    if args[:2] == ['-c', '%u']: print('0')
    else: os.execv(os.environ['REAL_STAT'], [os.environ['REAL_STAT'], *args])
elif name == 'df':
    if mode == 'space': print('Filesystem 1-blocks Used Available Capacity Mounted on\nfixture 1 1 0 100% /')
    elif mode == 'inodes' and '-Pi' in args: print('Filesystem Inodes IUsed IFree IUse% Mounted on\nfixture 1 1 0 100% /')
    else: os.execv(os.environ['REAL_DF'], [os.environ['REAL_DF'], *args])
elif name == 'mv':
    if args[-2] == str(root / 'nextcloud'): fail_once('old-rename')
    if mode == 'rollback-rename' and 'nextcloud.old.' in args[-2] and args[-1] == str(root / 'nextcloud'): die(29)
    if '.nextcloud-stage.' in args[-2] and args[-2].endswith('/nextcloud'):
        if mode == 'kill-between-renames': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGKILL); die(137)
        if mode == 'rollback-rename': die(28)
        fail_once('new-rename')
    os.execv(os.environ['REAL_MV'], [os.environ['REAL_MV'], *args])
elif name == 'cp':
    if args[-2].endswith('/config') and '/backups/' in args[-1]: fail_once('config-backup')
    os.execv(os.environ['REAL_CP'], [os.environ['REAL_CP'], *args])
elif name == 'tar':
    if '-xjf' in args: fail_once('extract')
    os.execv(os.environ['REAL_TAR'], [os.environ['REAL_TAR'], *args])
elif name == 'gzip':
    if '-c' in args: fail_once('compress')
    os.execv(os.environ['REAL_GZIP'], [os.environ['REAL_GZIP'], *args])
else: raise AssertionError(name)
'''

@unittest.skipUnless(REAL_PHP, 'PHP with SimpleXML required; set TEST_PHP')
class UpgradeScenarios:
    service_manager = 'monit'
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nc-upgrade-test-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        r = self.root
        (r / 'bin').mkdir()
        mock = r / 'bin/mock'; mock.write_text(MOCK.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1)); mock.chmod(0o755)
        names = ('sudo php curl monit systemctl crontab mariadb mariadb-dump mysql mysqldump psql pg_dump '
                 'findmnt pgrep chown stat df mv cp tar gzip').split()
        for name in names: (r / 'bin' / name).symlink_to(mock)
        # A closed PATH prevents missing doubles from reaching real host tools.
        tools = ('bash cat mktemp flock realpath find du awk grep sort head tail wc dirname date cmp rm '
                 'chmod mkdir tee sync tar bzip2 unzip zipinfo sha256sum sha512sum md5sum sleep').split()
        for name in tools:
            if not (r / 'bin' / name).exists():
                tool = shutil.which(name)
                if not tool: self.skipTest('Required test utility missing: ' + name)
                (r / 'bin' / name).symlink_to(tool)
        if self.service_manager == 'systemd':
            (r / 'bin/monit').unlink()
        for name in ('nextcloud/config', 'nextcloud/apps/core', 'nextcloud/apps/extra', 'nextcloud/custom_apps/test',
                     'nextcloud/themes/custom', 'storage', 'backups', 'var-tmp'):
            (r / name).mkdir(parents=True, exist_ok=True)
        (r / 'backups').chmod(0o700)
        (r / 'nextcloud/occ').write_text('old-code')
        (r / 'nextcloud/config/config.php').write_text('configuration')
        (r / 'nextcloud/config/extra.config.php').write_text('extra config')
        (r / 'nextcloud/apps/extra/custom.php').write_text('custom app')
        (r / 'nextcloud/custom_apps/test/custom.php').write_text('custom path app')
        (r / 'nextcloud/themes/custom/style.css').write_text('custom theme')
        (r / 'nextcloud/.user.ini').write_text('custom ini')
        (r / 'storage/sentinel').write_text('Never modify or duplicate EFS data')
        (r / 'nextcloud/version.php').write_text("<?php $OC_Build='2026-10-14';")
        (r / 'status').write_text(json.dumps(dict(installed=True, maintenance=False, needsDbUpgrade=False, version='34.0.0.0')))
        (r / 'services').write_text(json.dumps({'nginx': 'active', 'php8.4-fpm': 'active'}))
        self.original_cron = (f'*/5 * * * * php -f {r}/nextcloud/cron.php\n'
                              f'# */15 * * * * php -f {r}/nextcloud/cron.php\n'
                              '17 3 * * * echo unrelated\n')
        (r / 'cron').write_text(self.original_cron)
        (r / 'events').touch()
        release = r / 'release/nextcloud'
        for name in ('config', 'apps/core', 'themes', 'lib'): (release / name).mkdir(parents=True, exist_ok=True)
        for name in ('occ', 'index.php', 'status.php'): (release / name).write_text('new-code')
        (release / 'version.php').write_text("<?php $OC_Version=[34,0,1,0]; $OC_Channel='stable';")
        (release / 'lib/versioncheck.php').write_text('<?php if (PHP_VERSION_ID < 80400) exit(1);')
        self.make_archive()
        s = SCRIPT.read_text()
        assert '[[ $EUID == 0 ]]' in s
        s = s.replace('[[ $EUID == 0 ]]', '[[ 0 == 0 ]]', 1)
        s = s.replace('/run/lock/nextcloud-upgrade.lock', str(r / 'upgrade.lock'))
        s = s.replace('/var/tmp', str(r / 'var-tmp'))
        self.script = r / 'script'; self.script.write_text(s)
        self.env = dict(os.environ, PATH=str(r / 'bin'), TEST_ROOT=str(r), REAL_PHP=REAL_PHP,
                        SERVICE_MANAGER=self.service_manager,
                        NEXTCLOUD_PATH=str(r / 'nextcloud'), BACKUP_PATH=str(r / 'backups'),
                        WAIT_BEFORE_BACKUP='0', WAIT_AFTER_SERVER_START='0')
        for name, tool in REAL_TOOLS.items(): self.env['REAL_' + name.upper()] = tool

    @property
    def service_command(self):
        return 'monit' if self.service_manager == 'monit' else 'systemctl'

    def make_archive(self, unsafe=False):
        r = self.root
        with tarfile.open(r / 'nextcloud-34.0.1.tar.bz2', 'w:bz2') as archive:
            archive.add(r / 'release/nextcloud', arcname='nextcloud')
            if unsafe:
                import io
                info = tarfile.TarInfo('../escape'); info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
        with zipfile.ZipFile(r / 'nextcloud-34.0.1.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for p in (r / 'release').rglob('*'):
                archive.write(p, str(p.relative_to(r / 'release')) + ('/' if p.is_dir() else ''))

    def run_upgrade(self, mode='', args=(), answer='y', extra=None):
        env = dict(self.env, FAIL_AT=mode, **(extra or {}))
        # Supply the shell's own PID to doubles so signal delivery is precise.
        p = subprocess.run(['bash', '-c', 'export UPGRADE_PID=$$; exec bash "$@"', 'test', str(self.script), *args],
                           env=env, input=answer + '\n', capture_output=True, text=True, timeout=15)
        self.output = p.stdout + p.stderr
        # Failure workspaces are deliberately preserved by the script; remove
        # only paths whose manifest identifies this isolated fixture.
        for manifest in (self.root / 'backups').glob('upgrade-*/manifest'):
            fields = dict(line.split('=', 1) for line in manifest.read_text().splitlines())
            workspace = fields['workspace']
            self.addCleanup(shutil.rmtree, workspace, True)
        return p

    def assert_original(self):
        r = self.root
        self.assertEqual((r / 'nextcloud/occ').read_text(), 'old-code', self.output)
        self.assertFalse(json.loads((r / 'status').read_text())['maintenance'], self.output)
        self.assertEqual(json.loads((r / 'services').read_text()), {'nginx': 'active', 'php8.4-fpm': 'active'}, self.output)
        self.assertEqual((r / 'cron').read_text(), self.original_cron, self.output)
        self.assertFalse((r / 'backups/upgrade-active').exists(), self.output)
        self.assertEqual((r / 'storage/sentinel').read_text(), 'Never modify or duplicate EFS data')

    def assert_blocked(self):
        r = self.root
        self.assertTrue(json.loads((r / 'status').read_text())['maintenance'], self.output)
        self.assertEqual(json.loads((r / 'services').read_text()), {'nginx': 'inactive', 'php8.4-fpm': 'inactive'}, self.output)
        self.assertTrue((r / 'backups/upgrade-active').exists(), self.output)
        self.assertIn('# nextcloud-upgrade:', (r / 'cron').read_text())
        self.assertEqual((r / 'storage/sentinel').read_text(), 'Never modify or duplicate EFS data')

    def test_success_preserves_config_apps_themes_schedule_and_recovery(self):
        p = self.run_upgrade()
        self.assertEqual(p.returncode, 0, self.output)
        r = self.root
        self.assertEqual((r / 'cron').read_text(), self.original_cron)
        self.assertFalse(json.loads((r / 'status').read_text())['maintenance'])
        self.assertFalse((r / 'backups/upgrade-active').exists())
        self.assertEqual(len(list(r.glob('nextcloud.old.*'))), 1)
        self.assertTrue((r / 'nextcloud/config/extra.config.php').exists())
        self.assertTrue((r / 'nextcloud/apps/extra/custom.php').exists())
        self.assertTrue((r / 'nextcloud/custom_apps/test/custom.php').exists())
        self.assertTrue((r / 'nextcloud/themes/custom/style.css').exists())
        self.assertEqual((r / 'nextcloud/.user.ini').read_text(), 'custom ini')
        recovery = next((r / 'backups').glob('upgrade-*'))
        self.assertTrue((recovery / 'database.sql.gz').exists())
        self.assertEqual((recovery / 'phase').read_text(), 'complete\n')
        self.assertNotIn('secret', self.output)
        self.assertEqual((r / 'storage/sentinel').read_text(), 'Never modify or duplicate EFS data')
        self.assertFalse(any((r / 'nextcloud').rglob('sentinel')))
        fields = dict(line.split('=', 1) for line in (recovery / 'manifest').read_text().splitlines())
        self.assertFalse(Path(fields['workspace']).exists())
        self.assertFalse(Path(fields['staging']).exists())
        self.assertEqual(fields['service_manager'], self.service_manager)
        events = (r / 'events').read_text().splitlines()
        actions = [line for line in events if line.startswith(('monit start ', 'monit stop ', 'systemctl start ', 'systemctl stop '))]
        expected = [f'{self.service_command} {action} {svc}' for action, svc in
                    [('stop', 'nginx'), ('stop', 'php8.4-fpm'), ('start', 'php8.4-fpm'), ('start', 'nginx')]]
        self.assertEqual(actions, expected)
        if self.service_manager == 'systemd':
            self.assertFalse(any(line.startswith('monit ') for line in events))

    def test_zip_success(self):
        self.assertEqual(self.run_upgrade(extra={'DOWNLOAD_FORMAT': 'zip'}).returncode, 0, self.output)

    def test_explicit_same_major_target(self):
        self.assertEqual(self.run_upgrade(args=('34.0.1',)).returncode, 0, self.output)

    def test_sha512(self):
        self.assertEqual(self.run_upgrade(extra={'CHECKSUM_TYPE': 'sha512'}).returncode, 0, self.output)

    def test_port_and_socket_credentials(self):
        for host, option in [('localhost:3307', 'port=3307'), ('localhost:/var/run/mysql.sock', 'socket="/var/run/mysql.sock"')]:
            with self.subTest(host=host):
                # Cancellation still runs credential parsing and connectivity preflight.
                p = self.run_upgrade(answer='n', extra={'DB_HOST': host, 'EXPECT_DB_OPTION': option})
                self.assertEqual(p.returncode, 0, self.output)
                self.assert_original()

    def test_postgresql_success(self):
        self.assertEqual(self.run_upgrade(extra={'DB_TYPE': 'pgsql'}).returncode, 0, self.output)

    def test_preflight_failures_leave_production_unchanged(self):
        for mode in ('download', 'checksum', 'malformed-xml', 'manual-only', 'skip-major', 'extract', 'space', 'inodes', 'db-connect', 'cron-unreadable', 'mount'):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.run_upgrade(mode).returncode, 0, self.output)
                self.assert_original()
                self.assertNotIn(self.service_command + ' stop', (self.root / 'events').read_text())

    def test_pre_migration_failures_restore_original(self):
        for mode in ('maintenance-on', 'cron-write', 'stop-nginx', 'stop-php8.4-fpm', 'background', 'backup', 'missing-footer', 'compress', 'config-backup', 'old-rename', 'new-rename', 'signal-backup'):
            with self.subTest(mode=mode):
                self.assertNotEqual(self.run_upgrade(mode).returncode, 0, self.output)
                self.assert_original()
                self.assertNotIn('occ upgrade', (self.root / 'events').read_text())
                if mode not in ('background', 'backup', 'missing-footer', 'signal-backup'):
                    self.assertTrue((self.root / ('failed-' + mode)).exists(), self.output)

    def test_migration_failure_keeps_code_and_blocks_access(self):
        self.assertNotEqual(self.run_upgrade('migration').returncode, 0, self.output)
        self.assert_blocked()
        self.assertEqual((self.root / 'nextcloud/occ').read_text(), 'new-code')
        self.assertTrue(list(self.root.glob('nextcloud.old.*')))
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Interrupted attempt', self.output)

    def test_restoration_and_verification_failures_keep_new_code_blocked(self):
        for mode in ('start-nginx', 'start-php8.4-fpm', 'start-timeout', 'postcheck', 'maintenance-off'):
            with self.subTest(mode=mode):
                # Each must run independently because an unsafe attempt blocks reruns.
                with tempfile.TemporaryDirectory():
                    fixture = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery')
                    fixture.setUp()
                    try:
                        p = fixture.run_upgrade(mode)
                        self.assertNotEqual(p.returncode, 0, fixture.output)
                        fixture.assert_blocked()
                        self.assertEqual((fixture.root / 'nextcloud/occ').read_text(), 'new-code')
                    finally: fixture.doCleanups()

    def test_external_cron_edit_is_not_overwritten(self):
        self.assertNotEqual(self.run_upgrade('external-cron').returncode, 0, self.output)
        self.assertIn('echo new-job', (self.root / 'cron').read_text())
        self.assert_blocked()

    def test_term_and_hup_during_migration_do_not_rollback(self):
        for mode in ('signal-migration', 'hup-migration'):
            fixture = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery'); fixture.setUp()
            try:
                self.assertNotEqual(fixture.run_upgrade(mode).returncode, 0, fixture.output)
                fixture.assert_blocked()
            finally: fixture.doCleanups()

    def test_sigkill_leaves_persistent_marker_and_refuses_rerun(self):
        self.assertNotEqual(self.run_upgrade('kill-migration').returncode, 0, self.output)
        self.assertTrue((self.root / 'backups/upgrade-active').exists())
        self.assertEqual((self.root / 'nextcloud/occ').read_text(), 'new-code')
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Interrupted attempt', self.output)

    def test_kill_between_renames_refuses_rerun_with_missing_installation(self):
        self.assertNotEqual(self.run_upgrade('kill-between-renames').returncode, 0, self.output)
        self.assertFalse((self.root / 'nextcloud').exists())
        self.assertTrue(list(self.root.glob('nextcloud.old.*')))
        self.assertTrue((self.root / 'backups/upgrade-active').exists())
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Interrupted attempt', self.output)

    def test_unverifiable_shutdown_requires_manual_recovery(self):
        self.assertNotEqual(self.run_upgrade('stop-timeout').returncode, 0, self.output)
        self.assertTrue((self.root / 'backups/upgrade-active').exists())
        self.assertTrue(json.loads((self.root / 'status').read_text())['maintenance'])
        self.assertEqual((self.root / 'nextcloud/occ').read_text(), 'old-code')
        self.assertNotIn('occ upgrade', (self.root / 'events').read_text())
        self.assertIn('Automatic recovery incomplete', self.output)

    def test_rollback_rename_failure_is_not_reported_as_recovered(self):
        self.assertNotEqual(self.run_upgrade('rollback-rename').returncode, 0, self.output)
        self.assertFalse((self.root / 'nextcloud').exists())
        self.assertTrue(list(self.root.glob('nextcloud.old.*')))
        self.assert_blocked()
        self.assertIn('Automatic recovery incomplete', self.output)

    def test_previous_recovery_not_overwritten(self):
        previous = self.root / 'backups/upgrade-20261014T000000Z.previous'
        previous.mkdir(); (previous / 'database.sql.gz').write_bytes(b'previous backup')
        old = self.root / 'nextcloud.old.previous'; old.mkdir(); (old / 'occ').write_text('previous old code')
        self.assertEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertEqual((previous / 'database.sql.gz').read_bytes(), b'previous backup')
        self.assertEqual((old / 'occ').read_text(), 'previous old code')

    def test_removed_shipped_app_is_not_copied(self):
        app = self.root / 'nextcloud/apps/retired/appinfo'; app.mkdir(parents=True)
        (app / 'info.xml').write_text('<info><shipped>true</shipped></info>')
        self.assertEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertFalse((self.root / 'nextcloud/apps/retired').exists())
        self.assertTrue(next(self.root.glob('nextcloud.old.*')).joinpath('apps/retired').exists())

    def test_wrong_release_or_php_requirement_refused(self):
        (self.root / 'release/nextcloud/version.php').write_text("<?php $OC_Version=[34,0,2,0]; $OC_Channel='stable';")
        self.make_archive()
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assert_original()
        self.assertIn('does not match', self.output)
        (self.root / 'release/nextcloud/version.php').write_text("<?php $OC_Version=[34,0,1,0]; $OC_Channel='stable';")
        (self.root / 'release/nextcloud/lib/versioncheck.php').write_text('<?php exit(1);')
        self.make_archive()
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assert_original()

    def test_unsafe_archive_fails_before_maintenance(self):
        self.make_archive(unsafe=True)
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assert_original()
        self.assertIn('Unsafe archive', self.output)

    def test_existing_maintenance_and_pending_migration_refused(self):
        for key in ('maintenance', 'needsDbUpgrade'):
            s = json.loads((self.root / 'status').read_text()); s[key] = True
            (self.root / 'status').write_text(json.dumps(s))
            self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
            self.assertNotIn(self.service_command + ' stop', (self.root / 'events').read_text())
            s[key] = False; (self.root / 'status').write_text(json.dumps(s))

    def test_cancel_and_no_update(self):
        for mode in ('no-update', 'empty-xml'):
            self.assertEqual(self.run_upgrade(mode).returncode, 0, self.output)
            self.assert_original()
        self.assertEqual(self.run_upgrade(answer='n').returncode, 0, self.output)
        self.assert_original()

    def test_downgrade_skip_major_and_invalid_target(self):
        for target in ('33.0.9', '36.0.0', '35.0.0', '../../bad', '34.0.1;echo bad'):
            self.assertNotEqual(self.run_upgrade(args=(target,)).returncode, 0, self.output)
            self.assert_original()

    def test_legacy_old_and_symlink_refused(self):
        old = self.root / 'nextcloud.old'; old.mkdir()
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertTrue(old.exists()); old.rmdir()
        (self.root / 'nextcloud/data').symlink_to(self.root / 'storage', target_is_directory=True)
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assert_original()

    def test_concurrent_execution_refused(self):
        lock = subprocess.Popen(['flock', str(self.root / 'upgrade.lock'), 'sleep', '10'])
        self.addCleanup(lock.wait); self.addCleanup(lock.terminate)
        time.sleep(.1)
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('already running', self.output)
        self.assert_original()

    def test_invalid_service_manager_is_rejected_before_changes(self):
        self.assertNotEqual(self.run_upgrade(extra={'SERVICE_MANAGER': 'unsupported'}).returncode, 0, self.output)
        self.assertIn('SERVICE_MANAGER must be monit or systemd', self.output)
        self.assertEqual((self.root / 'events').read_text(), '')
        self.assert_original()

    def test_required_service_tool_is_checked_without_fallback(self):
        (self.root / 'bin' / self.service_command).unlink()
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Required command missing: ' + self.service_command, self.output)
        self.assert_original()

    def test_missing_unit_is_rejected_before_changes(self):
        self.assertNotEqual(self.run_upgrade('missing-unit').returncode, 0, self.output)
        self.assertIn('Unknown systemd service', self.output)
        self.assertNotIn(self.service_command + ' stop', (self.root / 'events').read_text())
        self.assert_original()

    def test_stopped_service_is_not_started_by_preflight(self):
        (self.root / 'services').write_text(json.dumps({'nginx': 'inactive', 'php8.4-fpm': 'active'}))
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Service must already be running', self.output)
        self.assertEqual(json.loads((self.root / 'services').read_text()), {'nginx': 'inactive', 'php8.4-fpm': 'active'})
        self.assertEqual((self.root / 'cron').read_text(), self.original_cron)
        self.assertFalse((self.root / 'backups/upgrade-active').exists())

    def test_service_state_polling_waits_for_completion(self):
        self.assertEqual(self.run_upgrade(extra={'SERVICE_DELAY': 'yes', 'WAIT_AFTER_SERVER_START': '1'}).returncode, 0, self.output)
        self.assertEqual(json.loads((self.root / 'services').read_text()), {'nginx': 'active', 'php8.4-fpm': 'active'})


class MonitUpgradeTests(UpgradeScenarios, unittest.TestCase):
    def test_unmonitored_service_is_rejected_in_monit_mode(self):
        self.assertNotEqual(self.run_upgrade('unmonitored').returncode, 0, self.output)
        self.assertIn('Service must already be monitored', self.output)
        self.assert_original()

    def test_monit_dependency_cascade_keeps_migration_isolated(self):
        self.assertEqual(self.run_upgrade(extra={'MONIT_DEPENDENCY': 'yes'}).returncode, 0, self.output)
        events = (self.root / 'events').read_text().splitlines()
        self.assertLess(events.index('dependency stop php8.4-fpm'), events.index('dump'))
        self.assertLess(events.index('occ upgrade'), events.index('dependency start nginx'))
        self.assertEqual(json.loads((self.root / 'services').read_text()), {'nginx': 'active', 'php8.4-fpm': 'active'})
        self.assertEqual((self.root / 'cron').read_text(), self.original_cron)

    def test_default_manager_remains_monit(self):
        self.env.pop('SERVICE_MANAGER')
        self.assertEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('monit stop nginx', (self.root / 'events').read_text())

class SystemdUpgradeTests(UpgradeScenarios, unittest.TestCase):
    service_manager = 'systemd'

if __name__ == '__main__': unittest.main(verbosity=2)
