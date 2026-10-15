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
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'nextcloud-upgrade.sh'
REAL_PHP = shutil.which(os.environ.get('TEST_PHP', 'php'))
REAL_TOOLS = {name: shutil.which(name) for name in ('stat', 'mv', 'cp', 'tar', 'gzip', 'df', 'rsync', 'sleep')}

MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
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
def cron_stopped():
    if os.environ.get('BACKGROUND_MODE', 'cron') != 'cron': return True
    path = root / 'cron'
    return not path.exists() or all(line.lstrip().startswith('#') or 'cron.php' not in line for line in path.read_text().splitlines())
if name == 'sudo':
    assert args[:3] == ['-u', os.environ.get('WEB_USER', 'www-data'), os.environ.get('PHP_BIN', 'php')], args
    if args[3] == '-r':
        assert args[-2] == 'build', args[-2:]
        event('build metadata as ' + args[1])
        os.execv(os.environ['REAL_PHP'], [os.environ['REAL_PHP'], *args[3:]])
    assert args[3] == './occ', args
    assert pathlib.Path.cwd() == root / 'nextcloud'
    a = args[4:]
    event('occ ' + ' '.join(a[:1]))
    s = load('status')
    if a[0] == 'status':
        if mode == 'postcheck' and s['version'] != '34.0.0.0': die(24)
        print(json.dumps(s))
    elif a[0] == 'config:system:get':
        values = {'version': '34.0.0.0', 'datadirectory': os.environ.get('DATA_DIRECTORY', str(root / 'storage')),
                  'dbtype': os.environ.get('DB_TYPE', 'mysql'), 'dbname': 'nextcloud',
                  'dbuser': 'nc', 'dbpassword': 'secret#"\\value',
                  'dbhost': os.environ.get('DB_HOST', 'localhost'), 'updater.release.channel': 'stable'}
        if a[1] not in values: die(1)
        print(values[a[1]])
    elif a[0] == 'config:app:get':
        assert a[1:] == ['core', 'backgroundjobs_mode', '--default-value=ajax'], a
        if mode == 'background-mode-read': die(26)
        value = os.environ.get('BACKGROUND_MODE', 'cron')
        print('ajax' if value == 'unset' else value)
    elif a[0] == 'config:list':
        extra_paths = json.loads(os.environ.get('EXTRA_APPS_PATHS', '["custom_apps"]'))
        system = {'apps_paths': [{'path': str(root / 'nextcloud/apps')}, *[{'path': str(root / 'nextcloud' / p)} for p in extra_paths]]}
        if mode == 'objectstore': system['objectstore'] = {'class': 'S3'}
        print(json.dumps({'system': system}))
    elif a[0] == 'maintenance:mode':
        if a[1] == '--on': fail_once('maintenance-on')
        if a[1] == '--off': fail_once('maintenance-off')
        s['maintenance'] = a[1] == '--on'; save('status', s)
    elif a[0] == 'upgrade':
        assert all(state == 'inactive' for state in load('services').values())
        assert cron_stopped()
        if os.environ.get('DATA_DIRECTORY'):
            assert (pathlib.Path(os.environ['DATA_DIRECTORY']) / 'sentinel').read_text() == 'Never modify or duplicate EFS data'
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
elif name in ('php', 'php-cli-alt'):
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
    assert svc in load('services'), svc
    if action == 'stop': fail_once('stop-' + svc)
    if action == 'start': fail_once('start-' + svc)
    if action == 'stop' and svc == 'nginx' and not (root / 'custom-source-changed').exists():
        (root / 'custom-source-changed').touch()
        custom = root / 'nextcloud/local.txt'
        if mode == 'custom-disappeared': custom.unlink()
        if mode == 'custom-symlink':
            custom.unlink(); custom.symlink_to(root / 'storage/sentinel')
        if mode == 'custom-mount': (root / 'mounted').touch()
        if mode == 'custom-app-conflict': (root / 'nextcloud/custom_apps/core').mkdir()
        if mode == 'data-replaced':
            data = pathlib.Path(os.environ.get('DATA_DIRECTORY', str(root / 'storage')))
            data.rename(data.with_name('original-data')); data.mkdir()
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
        print('not-found' if mode == 'missing-unit' or args[1] not in load('services') else 'loaded')
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
    assert args[:2] == ['-u', os.environ.get('WEB_USER', 'www-data')]
    if args[2] == '-l':
        event('cron read')
        if mode == 'cron-unreadable': die(1)
        if not (root / 'cron').exists(): print('no crontab for ' + args[1], file=sys.stderr); die(1)
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
    assert all(state == 'inactive' for state in load('services').values())
    assert cron_stopped()
    if mode == 'signal-backup': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGINT); time.sleep(.2); die(130)
    if mode == 'backup': print('partial sql'); die(27)
    dump = 'CREATE TABLE test(id int);\nINSERT INTO test VALUES(1);\n'
    if mode != 'missing-footer': dump += '-- Dump completed on 2026-10-14\n' if name != 'pg_dump' else '-- PostgreSQL database dump complete\n'
    if name == 'pg_dump': pathlib.Path(next(a.split('=', 1)[1] for a in args if a.startswith('--file='))).write_text(dump)
    else: sys.stdout.write(dump)
elif name == 'findmnt':
    if args[-2:] == ['--output', 'FSTYPE']:
        if mode == 'fstype-unavailable': die(1)
        print(os.environ.get('DATA_FSTYPE', 'ext4')); die(0)
    print('/')
    if mode == 'mount': print(str(root / 'nextcloud/data'))
    if (root / 'mounted').exists(): print(str(root / 'nextcloud/local.txt'))
    if mode == 'data-mount': print(os.environ['DATA_DIRECTORY'].replace(' ', r'\x20'))
elif name == 'pgrep':
    assert args[:2] == ['-u', os.environ.get('WEB_USER', 'www-data')]
    if mode == 'background': print('1234'); die(0)
    die(1)
elif name == 'chown':
    assert args[:2] == ['-hR', os.environ.get('WEB_USER', 'www-data') + ':' + (os.environ.get('WEB_GROUP') or os.environ.get('PRIMARY_GROUP', os.environ.get('WEB_USER', 'www-data')))]
    assert str(root / '.nextcloud-stage.') in args[-1], args
    assert not (pathlib.Path(args[-1]) / 'data/sentinel').exists(), 'Live data must not be present during ownership changes'
elif name == 'id':
    assert args[-1] == os.environ.get('WEB_USER', 'www-data'), args
    if mode == 'unknown-account': die(1)
    if args[0] == '-u': print('0' if args[-1] == 'root' else '33')
    else:
        assert args[0] == '-gn', args
        print(os.environ.get('PRIMARY_GROUP', args[-1]))
elif name == 'getent':
    assert args[0] == 'group', args
    if mode == 'unknown-group': die(2)
    print(args[1] + ':x:33:')
elif name == 'stat':
    if args[:2] == ['-c', '%u']: print('0')
    else: os.execv(os.environ['REAL_STAT'], [os.environ['REAL_STAT'], *args])
elif name == 'df':
    if mode == 'space': print('Filesystem 1-blocks Used Available Capacity Mounted on\nfixture 1 1 0 100% /')
    elif mode == 'inodes' and '-Pi' in args: print('Filesystem Inodes IUsed IFree IUse% Mounted on\nfixture 1 1 0 100% /')
    elif mode == 'data-space' and '-PB1' in args and args[-1] == str(root / 'backups'):
        print('Filesystem 1-blocks Used Available Capacity Mounted on\nfixture 999999999 1 105000000 1% /')
    else: os.execv(os.environ['REAL_DF'], [os.environ['REAL_DF'], *args])
elif name == 'mv':
    if args[-2].endswith('/data.partial'): fail_once('data-backup-rename')
    data = os.environ.get('DATA_DIRECTORY', str(root / 'storage'))
    if 'nextcloud.old.' in args[-2] and args[-1] == data:
        event('data move')
        fail_once('data-move')
        if mode in ('after-data-move', 'data-rollback', 'signal-after-data-move', 'kill-after-data-move'):
            import subprocess
            subprocess.run([os.environ['REAL_MV'], *args], check=True)
            if mode == 'signal-after-data-move': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGTERM); time.sleep(.2); die(143)
            if mode == 'kill-after-data-move': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGKILL); die(137)
            die(28)
    if mode == 'data-rollback' and args[-2] == data and 'nextcloud.old.' in args[-1]: die(29)
    if args[-2] == str(root / 'nextcloud'): fail_once('old-rename')
    if mode == 'rollback-rename' and 'nextcloud.old.' in args[-2] and args[-1] == str(root / 'nextcloud'): die(29)
    if '.nextcloud-stage.' in args[-2] and args[-2].endswith('/nextcloud'):
        if mode == 'kill-between-renames': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGKILL); die(137)
        if mode == 'rollback-rename': die(28)
        fail_once('new-rename')
    os.execv(os.environ['REAL_MV'], [os.environ['REAL_MV'], *args])
elif name == 'cp':
    if args[-2].endswith('/config') and '/backups/' in args[-1]: fail_once('config-backup')
    if args[-2] == str(root / 'nextcloud/local.txt') and '.nextcloud-stage.' in args[-1]: fail_once('custom-copy')
    os.execv(os.environ['REAL_CP'], [os.environ['REAL_CP'], *args])
elif name == 'tar':
    if '-xjf' in args: fail_once('extract')
    if '-cf' in args: fail_once('archive-create')
    if '-tf' in args and args[-1].endswith('/backup.tar.partial'): fail_once('archive-check')
    os.execv(os.environ['REAL_TAR'], [os.environ['REAL_TAR'], *args])
elif name == 'gzip':
    if '-c' in args: fail_once('compress')
    os.execv(os.environ['REAL_GZIP'], [os.environ['REAL_GZIP'], *args])
elif name == 'rsync':
    event('data verify' if '-aHAXnc' in args else 'data copy')
    assert load('status')['maintenance']
    assert all(state == 'inactive' for state in load('services').values())
    assert cron_stopped()
    assert pathlib.Path(args[-2]) == pathlib.Path(os.environ.get('DATA_DIRECTORY', str(root / 'storage')))
    assert '/backups/upgrade-' in args[-1] and args[-1].endswith('/data.partial/'), args
    if '-aHAXnc' in args:
        assert '--delete' in args and '--itemize-changes' in args
        if mode == 'data-verify': print('>fc........ sentinel'); die(0)
        fail_once('data-verify-error')
    else:
        (pathlib.Path(args[-1]) / 'partial-file').write_text('partial') if mode == 'data-copy' else None
        fail_once('data-copy')
        if mode == 'signal-data-copy': os.kill(int(os.environ['UPGRADE_PID']), signal.SIGTERM); time.sleep(.2); die(143)
    os.execv(os.environ['REAL_RSYNC'], [os.environ['REAL_RSYNC'], *args])
elif name == 'sleep':
    event('sleep ' + ' '.join(args))
    if args == [os.environ['WAIT_AFTER_MAINTENANCE']]:
        assert load('status')['maintenance']
        assert cron_stopped()
        assert all(state == 'active' for state in load('services').values())
        if mode == 'signal-settling':
            os.kill(int(os.environ['UPGRADE_PID']), signal.SIGINT); time.sleep(.2); die(130)
    os.execv(os.environ['REAL_SLEEP'], [os.environ['REAL_SLEEP'], *args])
elif name == 'ssh':
    assert args[:3] == ['-T', '-o', 'BatchMode=yes'], args
    assert 'StrictHostKeyChecking=yes' in args
    assert args[-2] == 'backup@backup-host', args
    command = args[-1]
    assert str(root / 'remote') in command, command
    assert str(root / 'nextcloud') not in command, command
    if command.startswith('command -v'):
        event('ssh check'); fail_once('ssh-connect')
    else:
        event('ssh copy')
        assert load('status')['maintenance']
        assert all(state == 'inactive' for state in load('services').values())
        fail_once('ssh-copy')
    os.chdir(root)
    os.execv('/bin/sh', ['/bin/sh', '-c', command])
else: raise AssertionError(name)
'''

@unittest.skipUnless(REAL_PHP, 'PHP with SimpleXML required; set TEST_PHP')
class UpgradeFixture:
    service_manager = 'monit'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nc-upgrade-test-')
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        r = self.root
        (r / 'bin').mkdir()
        # These short-lived mocks use only the standard library; skip site setup.
        mock = r / 'bin/mock'; mock.write_text(MOCK.replace('#!/usr/bin/env python3', '#!' + sys.executable + ' -S', 1)); mock.chmod(0o755)
        names = ('sudo php curl monit systemctl crontab mariadb mariadb-dump mysql mysqldump psql pg_dump '
                 'findmnt pgrep chown stat df mv cp tar gzip rsync ssh id getent').split()
        for name in names: (r / 'bin' / name).symlink_to(mock)
        # A closed PATH prevents missing doubles from reaching real host tools.
        tools = ('bash cat mktemp flock realpath readlink find du awk grep sort head tail wc dirname date cmp rm '
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
        (r / 'nextcloud/.htaccess').write_text('custom Apache settings')
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
                        WEB_USER='www-data', WEB_GROUP='', PHP_BIN='php',
                        NEXTCLOUD_PATH=str(r / 'nextcloud'), BACKUP_PATH=str(r / 'backups'),
                        WAIT_AFTER_MAINTENANCE='0', WAIT_BEFORE_BACKUP='0', WAIT_AFTER_SERVER_START='0')
        for name, tool in REAL_TOOLS.items():
            if tool: self.env['REAL_' + name.upper()] = tool

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
                name = str(p.relative_to(r / 'release'))
                if p.is_symlink():
                    info = zipfile.ZipInfo(name); info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, os.readlink(p))
                else:
                    archive.write(p, name + ('/' if p.is_dir() else ''))

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

    def preservation_list(self, text):
        path = self.root / 'preserve.txt'
        path.write_text(text)
        return {'PRESERVE_PATHS_FILE': str(path)}

    def assert_preservation_refused(self, extra, message):
        self.assertNotEqual(self.run_upgrade(extra=extra).returncode, 0, self.output)
        self.assertIn(message, self.output)
        self.assert_original()
        self.assertNotIn('occ maintenance:mode', (self.root / 'events').read_text())


class UpgradeScenarios(UpgradeFixture):
    # Manager-independent behavior is exercised once with systemd.
    def test_zip_success(self):
        self.assertEqual(self.run_upgrade(extra={'DOWNLOAD_FORMAT': 'zip'}).returncode, 0, self.output)

    def test_shipped_internal_file_links_work_in_tar_and_zip(self):
        for archive_format in ('tar.bz2', 'zip'):
            with self.subTest(archive_format=archive_format):
                f = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery'); f.setUp()
                try:
                    for base in (f.root / 'nextcloud', f.root / 'release/nextcloud'):
                        dist = base / 'dist'; dist.mkdir()
                        (dist / 'asset.js.license').write_text('license content')
                        (dist / 'asset.js.map.license').symlink_to('asset.js.license')
                        (dist / 'another.map.license').symlink_to('asset.js.map.license')
                    f.make_archive()
                    self.assertEqual(f.run_upgrade(extra={'DOWNLOAD_FORMAT': archive_format}).returncode, 0, f.output)
                    self.assertEqual(os.readlink(f.root / 'nextcloud/dist/asset.js.map.license'), 'asset.js.license')
                    self.assertEqual((f.root / 'nextcloud/dist/asset.js.map.license').read_text(), 'license content')
                finally: f.doCleanups()

    def test_archive_links_cannot_escape_or_point_to_directories(self):
        for archive_format in ('tar.bz2', 'zip'):
            for target in ('/etc/passwd', '../../outside', 'directory', 'loop'):
                with self.subTest(archive_format=archive_format, target=target):
                    dist = self.root / 'release/nextcloud/dist'; dist.mkdir(exist_ok=True)
                    (dist / 'directory').mkdir(exist_ok=True)
                    link = dist / 'loop'; link.symlink_to(target)
                    self.make_archive()
                    self.assertNotEqual(self.run_upgrade(extra={'DOWNLOAD_FORMAT': archive_format}).returncode, 0, self.output)
                    self.assert_original()
                    self.assertNotIn('occ maintenance:mode', (self.root / 'events').read_text())
                    link.unlink()

    def test_application_links_into_internal_data_are_rejected(self):
        data = self.root / 'nextcloud/data'; data.mkdir()
        (data / 'private').write_text('must never copy through an application link')
        (self.root / 'nextcloud/dist').mkdir()
        (self.root / 'nextcloud/dist/unsafe').symlink_to('../data/private')
        self.assertNotEqual(self.run_upgrade(extra={'DATA_DIRECTORY': str(data)}).returncode, 0, self.output)
        self.assertIn('Unsupported application link into user data', self.output)
        self.assert_original()

    def test_links_in_custom_app_are_copied_without_following_them(self):
        link = self.root / 'nextcloud/custom_apps/test/local.php'
        link.symlink_to('custom.php')
        self.assertEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertEqual(os.readlink(link), 'custom.php')
        recovery = next((self.root / 'backups').glob('upgrade-*/manifest')).parent
        self.assertEqual(os.readlink(recovery / 'custom/custom_apps/test/local.php'), 'custom.php')

    def test_installed_build_metadata_runs_as_web_account(self):
        self.assertEqual(self.run_upgrade(answer='n').returncode, 0, self.output)
        self.assertIn('build metadata as www-data', (self.root / 'events').read_text())
        self.assert_original()

    def test_custom_web_account_group_and_php_apply_to_success_and_recovery(self):
        for mode in ('', 'new-rename', 'migration'):
            with self.subTest(mode=mode):
                f = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery')
                f.setUp()
                try:
                    binary = f.root / 'bin/php-cli-alt'; binary.symlink_to(f.root / 'bin/mock')
                    extra = {'WEB_USER': 'apache', 'WEB_GROUP': 'wwwrun', 'PHP_BIN': str(binary)}
                    result = f.run_upgrade(mode, extra=extra)
                    self.assertEqual(result.returncode == 0, not mode, f.output)
                    if mode == 'new-rename': f.assert_original()
                    if mode == 'migration': f.assert_blocked()
                    recovery = next((f.root / 'backups').glob('upgrade-*/manifest')).parent
                    manifest = (recovery / 'manifest').read_text()
                    self.assertIn('web_user=apache\nweb_group=wwwrun\nphp_binary=' + str(binary), manifest)
                finally: f.doCleanups()

    def test_primary_group_is_used_when_web_group_is_unset(self):
        self.assertEqual(self.run_upgrade(extra={'WEB_USER': 'nginx', 'PRIMARY_GROUP': 'wwwrun'}).returncode, 0, self.output)
        recovery = next((self.root / 'backups').glob('upgrade-*'))
        self.assertIn('web_group=wwwrun', (recovery / 'manifest').read_text())

    def test_absent_account_crontab_is_allowed_and_remains_absent(self):
        (self.root / 'cron').unlink()
        self.assertEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertFalse((self.root / 'cron').exists())
        self.assertIn('Check that any systemd timers or external workers are paused', self.output)
        recovery = next((self.root / 'backups').glob('upgrade-*/manifest')).parent
        self.assertIn('account_crontab_present=no', (recovery / 'manifest').read_text())

    def test_absent_crontab_is_not_created_during_failure_recovery(self):
        for mode in ('backup', 'migration'):
            with self.subTest(mode=mode):
                f = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery'); f.setUp()
                try:
                    (f.root / 'cron').unlink()
                    self.assertNotEqual(f.run_upgrade(mode).returncode, 0, f.output)
                    self.assertFalse((f.root / 'cron').exists())
                    self.assertEqual(json.loads((f.root / 'status').read_text())['maintenance'], mode == 'migration')
                finally: f.doCleanups()

    def test_invalid_or_missing_accounts_and_groups_fail_before_changes(self):
        for mode, extra in [('', {'WEB_USER': 'root'}), ('', {'WEB_USER': '-root'}),
                            ('unknown-account', {}), ('unknown-group', {})]:
            with self.subTest(mode=mode, extra=extra):
                self.assertNotEqual(self.run_upgrade(mode, extra=extra).returncode, 0, self.output)
                self.assertEqual((self.root / 'events').read_text(), '')
                self.assert_original()

    def test_summary_shows_versions_paths_and_selected_backup_options(self):
        self.assertEqual(self.run_upgrade(extra={'BACKUP_ARCHIVE': 'yes'}).returncode, 0, self.output)
        self.assertIn('Preparation checks passed.', self.output)
        self.assertRegex(self.output, r'Data backup:\s+no')
        self.assertRegex(self.output, r'Local bundle:\s+yes')
        self.assertIn('Upgrade complete: 34.0.0 -> 34.0.1.', self.output)
        self.assertIn('Recovery directory:', self.output)
        self.assertIn('Restore bundle:', self.output)
        self.assertNotIn('secret', self.output)

    def test_extra_files_directories_and_literal_names_are_preserved(self):
        r = self.root
        custom = r / 'nextcloud/local assets'
        custom.mkdir(); (custom / '.hidden').write_text('hidden content')
        (custom / 'nested').mkdir(); (custom / 'nested/theme.css').write_text('css')
        (r / 'nextcloud/lib/local.txt').parent.mkdir(exist_ok=True)
        (r / 'nextcloud/lib/local.txt').write_text('nested file')
        literal = 'notes $(touch owned).txt'
        (r / 'nextcloud' / literal).write_text('literal name')
        extra = self.preservation_list('# Additional files\n\nlocal assets\nlib/local.txt\n' + literal)
        self.assertEqual(self.run_upgrade(extra=extra).returncode, 0, self.output)
        self.assertEqual((r / 'nextcloud/local assets/.hidden').read_text(), 'hidden content')
        self.assertEqual((r / 'nextcloud/local assets/nested/theme.css').read_text(), 'css')
        self.assertEqual((r / 'nextcloud/lib/local.txt').read_text(), 'nested file')
        self.assertEqual((r / 'nextcloud' / literal).read_text(), 'literal name')
        self.assertFalse((r / 'owned').exists())
        recovery = next((r / 'backups').glob('upgrade-*'))
        self.assertEqual((recovery / 'preserved-paths').read_text().splitlines(),
                         ['custom_apps', 'apps/extra', 'themes/custom', 'local assets', 'lib/local.txt', literal])
        self.assertEqual((r / 'storage/sentinel').read_text(), 'Never modify or duplicate EFS data')
        self.assertFalse(any((r / 'nextcloud').rglob('sentinel')))

    def test_unselected_custom_file_stays_only_in_retained_old_application(self):
        (self.root / 'nextcloud/local.txt').write_text('local content')
        self.assertEqual(self.run_upgrade(extra=self.preservation_list('# No extra paths\n\n')).returncode, 0, self.output)
        self.assertFalse((self.root / 'nextcloud/local.txt').exists())
        old = next(self.root.glob('nextcloud.old.*'))
        self.assertEqual((old / 'local.txt').read_text(), 'local content')

    def test_invalid_and_reserved_preservation_paths_fail_before_maintenance(self):
        for path, message in [('../storage', 'canonical installation-relative'),
                              ('/etc/passwd', 'Invalid preservation path'),
                              ('.', 'canonical installation-relative'),
                              ('local/../occ', 'canonical installation-relative'),
                              ('./occ', 'canonical installation-relative'),
                              ('lib//local.txt', 'canonical installation-relative'),
                              ('custom_apps/', 'Invalid preservation path'),
                              ('local\r', 'Invalid preservation path'),
                              ('missing', 'Missing preservation'),
                              ('data', 'reserved'), ('data/files', 'reserved'),
                              ('config', 'reserved'), ('config/config.php', 'reserved'),
                              ('.htaccess', 'reserved'), ('.user.ini', 'reserved')]:
            with self.subTest(path=path):
                self.assert_preservation_refused(self.preservation_list(path + '\n'), message)

    def test_missing_or_nonfile_preservation_list_is_rejected(self):
        for path in (self.root / 'missing.txt', self.root / 'storage'):
            with self.subTest(path=path):
                self.assert_preservation_refused({'PRESERVE_PATHS_FILE': str(path)}, 'readable regular file')

    def test_preservation_refuses_release_files_directories_and_file_parents(self):
        r = self.root
        (r / 'nextcloud/apps/core/custom.txt').write_text('old shipped app')
        (r / 'nextcloud/assets').mkdir(); (r / 'nextcloud/assets/local.txt').write_text('custom')
        (r / 'release/nextcloud/assets').write_text('new shipped file'); self.make_archive()
        for path, message in [('occ', 'overwrite a release'), ('apps/core', 'overwrite a release'),
                              ('assets/local.txt', 'file as its destination parent')]:
            with self.subTest(path=path):
                self.assert_preservation_refused(self.preservation_list(path), message)

    def test_duplicate_and_overlapping_preservation_paths_are_rejected(self):
        (self.root / 'nextcloud/local').mkdir()
        (self.root / 'nextcloud/local/note').write_text('note')
        for paths in ('local\nlocal', 'local\nlocal/note', 'local/note\nlocal',
                      'custom_apps/test', 'themes/custom/style.css', 'apps/extra'):
            with self.subTest(paths=paths):
                self.assert_preservation_refused(self.preservation_list(paths), 'Overlapping preservation paths')

    def test_automatic_custom_directories_cannot_overwrite_release_content(self):
        for path in ('custom_apps', 'themes/custom'):
            with self.subTest(path=path):
                destination = self.root / 'release/nextcloud' / path
                destination.mkdir(parents=True)
                (destination / 'shipped').write_text('release content'); self.make_archive()
                self.assert_preservation_refused({}, 'overwrite a release file or directory: ' + path)
                shutil.rmtree(destination)

    def test_custom_app_cannot_shadow_newly_shipped_app_from_another_directory(self):
        app = self.root / 'nextcloud/custom_apps/core'
        app.mkdir(); (app / 'custom.php').write_text('old custom implementation')
        self.assert_preservation_refused({}, 'Custom app conflicts with an app shipped by the release: custom_apps/core (apps/core)')

    def test_custom_symlinks_and_special_files_are_refused(self):
        local = self.root / 'nextcloud/local.txt'
        local.symlink_to(self.root / 'storage/sentinel')
        extra = self.preservation_list('local.txt')
        self.assert_preservation_refused(extra, 'Unsupported application link')
        local.unlink(); os.mkfifo(local)
        self.assert_preservation_refused(extra, 'Only regular files and directories')

    def test_changed_custom_sources_and_copy_failure_recover_original_installation(self):
        for mode, message in [('custom-disappeared', 'Missing preservation'),
                              ('custom-symlink', 'Unsupported application link'),
                              ('custom-mount', 'Mount inside application tree'),
                              ('custom-app-conflict', 'Custom app conflicts'),
                              ('custom-copy', 'failed at script line')]:
            with self.subTest(mode=mode):
                fixture = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery')
                fixture.setUp()
                try:
                    (fixture.root / 'nextcloud/local.txt').write_text('custom')
                    self.assertNotEqual(fixture.run_upgrade(mode, extra=fixture.preservation_list('local.txt')).returncode, 0, fixture.output)
                    self.assertIn(message, fixture.output)
                    fixture.assert_original()
                    self.assertNotIn('occ upgrade', (fixture.root / 'events').read_text())
                    recovery = next((fixture.root / 'backups').glob('upgrade-*'))
                    self.assertIn('local.txt', (recovery / 'preserved-paths').read_text())
                    if mode == 'custom-copy':
                        self.assertTrue((recovery / 'database.sql.gz').exists())
                        self.assertEqual((fixture.root / 'nextcloud/local.txt').read_text(), 'custom')
                finally: fixture.doCleanups()

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

    def http_jobs_fixture(self, mode, no_crontab=True):
        f = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.env['BACKGROUND_MODE'] = mode
        f.original_cron = '17 3 * * * echo unrelated\n'
        (f.root / 'cron').write_text(f.original_cron)
        if no_crontab:
            (f.root / 'bin/crontab').unlink()
        return f

    def test_http_background_modes_do_not_require_or_read_crontab(self):
        for mode, no_crontab in (('ajax', True), ('webcron', True), ('unset', True), ('none', True), ('ajax', False), ('webcron', False)):
            with self.subTest(mode=mode, no_crontab=no_crontab):
                f = self.http_jobs_fixture(mode, no_crontab)
                archive = 'yes' if mode == 'webcron' and no_crontab else 'no'
                self.assertEqual(f.run_upgrade('cron-unreadable', extra={'BACKUP_ARCHIVE': archive}).returncode, 0, f.output)
                self.assertFalse(json.loads((f.root / 'status').read_text())['maintenance'])
                self.assertTrue(all(state == 'active' for state in json.loads((f.root / 'services').read_text()).values()))
                self.assertEqual((f.root / 'cron').read_text(), f.original_cron)
                events = (f.root / 'events').read_text().splitlines()
                self.assertFalse(any(event.startswith('cron ') for event in events))
                recovery = next((f.root / 'backups').glob('upgrade-*/manifest')).parent
                manifest = (recovery / 'manifest').read_text()
                self.assertIn('background_jobs_mode=' + ('ajax' if mode == 'unset' else mode), manifest)
                self.assertIn('account_crontab_managed=no', manifest)
                self.assertFalse((recovery / 'cron.original').exists())
                if archive == 'yes':
                    with tarfile.open(recovery / 'backup.tar') as saved:
                        self.assertIn('database.sql.gz', saved.getnames())
                        self.assertNotIn('cron.original', saved.getnames())
                        self.assertNotIn('cron.disabled', saved.getnames())

    def test_http_background_recovery_never_touches_crontab(self):
        for mode in ('ajax', 'webcron'):
            for failure in ('backup', 'migration'):
                with self.subTest(mode=mode, failure=failure):
                    f = self.http_jobs_fixture(mode)
                    self.assertNotEqual(f.run_upgrade(failure).returncode, 0, f.output)
                    self.assertEqual((f.root / 'cron').read_text(), f.original_cron)
                    self.assertFalse(any(event.startswith('cron ') for event in (f.root / 'events').read_text().splitlines()))
                    self.assertNotIn('crontab: command not found', f.output)
                    if failure == 'backup':
                        f.assert_original()
                    else:
                        self.assertTrue(json.loads((f.root / 'status').read_text())['maintenance'])
                        self.assertTrue(all(state == 'inactive' for state in json.loads((f.root / 'services').read_text()).values()))
                        self.assertTrue((f.root / 'backups/upgrade-active').exists())

    def test_background_mode_errors_leave_production_unchanged(self):
        for mode, failure in (('unknown', ''), ('', ''), ('cron', 'background-mode-read')):
            with self.subTest(mode=mode, failure=failure):
                self.assertNotEqual(self.run_upgrade(failure, extra={'BACKGROUND_MODE': mode}).returncode, 0, self.output)
                self.assertNotIn('occ maintenance:mode', (self.root / 'events').read_text())
                self.assert_original()

    def test_cron_with_an_external_scheduler_needs_no_crontab_binary(self):
        f = self.http_jobs_fixture('cron')
        self.assertEqual(f.run_upgrade().returncode, 0, f.output)
        self.assertIn('crontab is unavailable', f.output)
        self.assertFalse(any(event.startswith('cron ') for event in (f.root / 'events').read_text().splitlines()))

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

    def test_invalid_maintenance_grace_is_rejected_before_changes(self):
        for value in ('-1', '1.5', 'invalid', '100000'):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_upgrade(extra={'WAIT_AFTER_MAINTENANCE': value}).returncode, 0, self.output)
                self.assertIn('Wait values must be nonnegative integers', self.output)
                self.assertEqual((self.root / 'events').read_text(), '')
                self.assert_original()

    def test_invalid_service_manager_is_rejected_before_changes(self):
        self.assertNotEqual(self.run_upgrade(extra={'SERVICE_MANAGER': 'unsupported'}).returncode, 0, self.output)
        self.assertIn('SERVICE_MANAGER must be monit or systemd', self.output)
        self.assertEqual((self.root / 'events').read_text(), '')
        self.assert_original()

    def test_missing_unit_is_rejected_before_changes(self):
        self.assertNotEqual(self.run_upgrade('missing-unit').returncode, 0, self.output)
        self.assertIn('Unknown systemd service', self.output)
        self.assertNotIn(self.service_command + ' stop', (self.root / 'events').read_text())
        self.assert_original()


class ServiceScenarios(UpgradeFixture):
    # Each service adapter must satisfy this same contract.
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
        self.assertRegex(recovery.name, r'^upgrade-34\.0\.0\.0-[0-9]{8}T[0-9]{6}Z\.[A-Za-z0-9]{8}$')
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

    def test_unverifiable_shutdown_requires_manual_recovery(self):
        self.assertNotEqual(self.run_upgrade('stop-timeout').returncode, 0, self.output)
        self.assertTrue((self.root / 'backups/upgrade-active').exists())
        self.assertTrue(json.loads((self.root / 'status').read_text())['maintenance'])
        self.assertEqual((self.root / 'nextcloud/occ').read_text(), 'old-code')
        self.assertNotIn('occ upgrade', (self.root / 'events').read_text())
        self.assertIn('Automatic recovery incomplete', self.output)

    def test_required_service_tool_is_checked_without_fallback(self):
        (self.root / 'bin' / self.service_command).unlink()
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Required command missing: ' + self.service_command, self.output)
        self.assert_original()

    def test_stopped_service_is_not_started_by_preflight(self):
        (self.root / 'services').write_text(json.dumps({'nginx': 'inactive', 'php8.4-fpm': 'active'}))
        self.assertNotEqual(self.run_upgrade().returncode, 0, self.output)
        self.assertIn('Service must already be running', self.output)
        self.assertEqual(json.loads((self.root / 'services').read_text()), {'nginx': 'inactive', 'php8.4-fpm': 'active'})
        self.assertEqual((self.root / 'cron').read_text(), self.original_cron)
        self.assertFalse((self.root / 'backups/upgrade-active').exists())

    def settling_sleep(self):
        (self.root / 'bin/sleep').unlink()
        (self.root / 'bin/sleep').symlink_to(self.root / 'bin/mock')
        return {'WAIT_AFTER_MAINTENANCE': '1'}

    def test_maintenance_grace_precedes_service_shutdown_and_backups(self):
        self.assertEqual(self.run_upgrade(extra=self.settling_sleep()).returncode, 0, self.output)
        events = (self.root / 'events').read_text().splitlines()
        self.assertLess(events.index('occ maintenance:mode'), events.index('sleep 1'))
        self.assertLess(events.index('sleep 1'), events.index(self.service_command + ' stop nginx'))
        self.assertLess(events.index(self.service_command + ' stop php8.4-fpm'), events.index('dump'))

    def test_interrupt_during_maintenance_grace_restores_original(self):
        self.assertNotEqual(self.run_upgrade('signal-settling', extra=self.settling_sleep()).returncode, 0, self.output)
        self.assert_original()
        self.assertNotIn('dump', (self.root / 'events').read_text().splitlines())

    def test_service_state_polling_waits_for_completion(self):
        self.assertEqual(self.run_upgrade(extra={'SERVICE_DELAY': 'yes', 'WAIT_AFTER_SERVER_START': '1'}).returncode, 0, self.output)
        self.assertEqual(json.loads((self.root / 'services').read_text()), {'nginx': 'active', 'php8.4-fpm': 'active'})


class MonitUpgradeTests(ServiceScenarios, unittest.TestCase):
    def test_service_command_failures_restore_or_block_access(self):
        for mode in ('stop-nginx', 'stop-php8.4-fpm', 'start-nginx', 'start-php8.4-fpm', 'start-timeout'):
            with self.subTest(mode=mode):
                f = type(self)('test_success_preserves_config_apps_themes_schedule_and_recovery')
                f.setUp()
                try:
                    self.assertNotEqual(f.run_upgrade(mode).returncode, 0, f.output)
                    if mode.startswith('stop-'):
                        f.assert_original()
                        self.assertNotIn('occ upgrade', (f.root / 'events').read_text())
                    else:
                        f.assert_blocked()
                        self.assertEqual((f.root / 'nextcloud/occ').read_text(), 'new-code')
                finally:
                    f.doCleanups()

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

class SystemdUpgradeTests(UpgradeScenarios, ServiceScenarios, unittest.TestCase):
    service_manager = 'systemd'

@unittest.skipUnless(REAL_PHP and REAL_TOOLS['rsync'], 'PHP with SimpleXML and rsync required')
class DataBackupTests(unittest.TestCase):
    def fixture(self, manager='systemd', relative=None):
        cls = MonitUpgradeTests if manager == 'monit' else SystemdUpgradeTests
        fixture = cls('test_success_preserves_config_apps_themes_schedule_and_recovery')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        data = fixture.root / 'storage'
        if relative:
            destination = fixture.root / 'nextcloud' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            data.rename(destination); data = destination
            fixture.env['DATA_DIRECTORY'] = str(data)
        fixture.data = data
        (data / '.ocdata').write_text('data marker')
        (data / 'sentinel').chmod(0o604)
        try: os.setxattr(data / 'sentinel', 'user.backup-test', b'preserve attributes')
        except OSError: pass  # Filesystems without user xattrs can still run other cases.
        (data / 'users').mkdir()
        (data / 'users/.hidden').write_text('private content')
        (data / 'link').symlink_to('/does/not/exist')
        return fixture

    def assert_data_untouched(self, fixture):
        self.assertEqual((fixture.data / 'sentinel').read_text(), 'Never modify or duplicate EFS data')
        self.assertEqual((fixture.data / 'sentinel').stat().st_mode & 0o777, 0o604)
        self.assertEqual(os.readlink(fixture.data / 'link'), '/does/not/exist')
        self.assertEqual((fixture.data / 'users/.hidden').read_text(), 'private content')

    def assert_recovered(self, fixture):
        self.assertEqual((fixture.root / 'nextcloud/occ').read_text(), 'old-code', fixture.output)
        self.assertFalse(json.loads((fixture.root / 'status').read_text())['maintenance'], fixture.output)
        self.assertEqual(json.loads((fixture.root / 'services').read_text()), {'nginx': 'active', 'php8.4-fpm': 'active'})
        self.assertEqual((fixture.root / 'cron').read_text(), fixture.original_cron)
        self.assertFalse((fixture.root / 'backups/upgrade-active').exists(), fixture.output)
        self.assertNotIn('occ upgrade', (fixture.root / 'events').read_text())
        self.assert_data_untouched(fixture)

    def test_internal_and_external_data_with_backup_on_and_off(self):
        for relative in (None, 'data', 'private/user files'):
            for enabled in ('no', 'yes'):
                with self.subTest(relative=relative, enabled=enabled):
                    f = self.fixture(relative=relative)
                    if relative == 'data':
                        skeleton = f.root / 'release/nextcloud/data'
                        skeleton.mkdir(); (skeleton / '.htaccess').write_text('new skeleton')
                        f.make_archive()
                    self.assertEqual(f.run_upgrade(extra={'BACKUP_DATA': enabled}).returncode, 0, f.output)
                    self.assert_data_untouched(f)
                    recovery = next((f.root / 'backups').glob('upgrade-*'))
                    self.assertEqual((recovery / 'data').exists(), enabled == 'yes')
                    self.assertFalse((recovery / 'data.partial').exists())
                    manifest = (recovery / 'manifest').read_text()
                    self.assertIn('data_directory=' + str(f.data), manifest)
                    self.assertIn('backup_data=' + enabled, manifest)
                    self.assertIn('internal_data_relative=' + (relative or ''), manifest)
                    self.assertFalse((recovery / 'backup.tar').exists())
                    events = (f.root / 'events').read_text()
                    if enabled == 'yes':
                        self.assertEqual((recovery / 'data/users/.hidden').read_text(), 'private content')
                        self.assertEqual(os.readlink(recovery / 'data/link'), '/does/not/exist')
                        self.assertEqual((recovery / 'data.verify.log').read_text(), '')
                        self.assertLess(events.index('dump'), events.index('data copy'))
                        self.assertLess(events.index('data verify'), events.index('occ upgrade'))
                    else:
                        self.assertNotIn('data copy', events)
                    if relative:
                        old = next(f.root.glob('nextcloud.old.*'))
                        self.assertFalse((old / relative).exists())
                        self.assertFalse(any((f.root / 'nextcloud').rglob('.nextcloud-stage.*')))

    def test_disabled_data_backup_needs_no_rsync_and_local_backups_are_allowed(self):
        f = self.fixture(relative='data'); (f.root / 'bin/rsync').unlink()
        self.assertEqual(f.run_upgrade().returncode, 0, f.output)
        self.assertIn('Note: recovery backups share the data filesystem', f.output)
        self.assert_data_untouched(f)

    def test_data_backup_failures_never_start_migration(self):
        for mode in ('data-copy', 'data-verify', 'data-verify-error', 'data-backup-rename', 'signal-data-copy'):
            with self.subTest(mode=mode):
                f = self.fixture(relative='data')
                self.assertNotEqual(f.run_upgrade(mode, extra={'BACKUP_DATA': 'yes'}).returncode, 0, f.output)
                self.assert_recovered(f)
                recovery = next((f.root / 'backups').glob('upgrade-*'))
                self.assertTrue((recovery / 'database.sql.gz').exists())
                self.assertTrue((recovery / 'data.partial').exists())
                self.assertFalse((recovery / 'data').exists())

    def test_internal_data_rename_failures_restore_data_and_code(self):
        for mode in ('old-rename', 'new-rename', 'data-move', 'after-data-move', 'signal-after-data-move'):
            with self.subTest(mode=mode):
                f = self.fixture(relative='data')
                self.assertNotEqual(f.run_upgrade(mode).returncode, 0, f.output)
                self.assert_recovered(f)

    def test_failed_data_rollback_and_sigkill_keep_live_data_and_block_retry(self):
        for mode in ('data-rollback', 'kill-after-data-move', 'migration'):
            with self.subTest(mode=mode):
                f = self.fixture(relative='data')
                self.assertNotEqual(f.run_upgrade(mode).returncode, 0, f.output)
                self.assertTrue((f.root / 'backups/upgrade-active').exists(), f.output)
                self.assert_data_untouched(f)
                self.assertNotEqual(f.run_upgrade().returncode, 0, f.output)
                self.assertIn('Interrupted attempt', f.output)

    def test_data_capacity_is_checked_only_when_copying_and_before_maintenance(self):
        f = self.fixture(relative='data')
        with (f.data / 'large').open('wb') as data: data.truncate(4 * 1024 * 1024)
        self.assertEqual(f.run_upgrade('data-space', answer='n').returncode, 0, f.output)
        self.assertNotEqual(f.run_upgrade('data-space', extra={'BACKUP_DATA': 'yes'}).returncode, 0, f.output)
        self.assertIn('Insufficient space', f.output)
        self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())
        self.assert_data_untouched(f)

    def test_unsafe_data_layouts_and_primary_object_storage_are_rejected(self):
        for name, extra, mode in [('missing', {'DATA_DIRECTORY': '/no-such-data'}, ''),
                                  ('application', {'DATA_DIRECTORY': 'INSTALLATION'}, ''),
                                  ('objectstore', {'BACKUP_DATA': 'yes'}, 'objectstore'),
                                  ('invalid option', {'BACKUP_DATA': 'maybe'}, '')]:
            with self.subTest(name=name):
                f = self.fixture()
                if extra.get('DATA_DIRECTORY') == 'INSTALLATION': extra['DATA_DIRECTORY'] = str(f.root / 'nextcloud')
                self.assertNotEqual(f.run_upgrade(mode, extra=extra).returncode, 0, f.output)
                self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())
                self.assert_data_untouched(f)
        f = self.fixture()
        inside = f.data / 'backups'; inside.mkdir(); inside.chmod(0o700)
        self.assertNotEqual(f.run_upgrade(extra={'BACKUP_PATH': str(inside)}).returncode, 0, f.output)
        self.assertIn('outside the user-data directory', f.output)

    def test_custom_file_preservation_cannot_copy_internal_data_or_its_parent(self):
        for path in ('private', 'private/user files', 'private/user files/sentinel'):
            with self.subTest(path=path):
                f = self.fixture(relative='private/user files')
                self.assertNotEqual(f.run_upgrade(extra=f.preservation_list(path)).returncode, 0, f.output)
                self.assertIn('User data is handled separately', f.output)
                self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())

    def test_optional_archive_contains_restore_components_and_checksum(self):
        for enabled in ('no', 'yes'):
            with self.subTest(data=enabled):
                f = self.fixture(relative='data')
                self.assertEqual(f.run_upgrade(extra={'BACKUP_DATA': enabled, 'BACKUP_ARCHIVE': 'yes'}).returncode, 0, f.output)
                recovery = next((f.root / 'backups').glob('upgrade-*'))
                archive = recovery / 'backup.tar'
                with tarfile.open(archive) as saved:
                    names = saved.getnames()
                    self.assertIn('config/config.php', names)
                    self.assertIn('database.sql.gz', names)
                    self.assertEqual(saved.extractfile('.htaccess').read(), b'custom Apache settings')
                    self.assertEqual(saved.extractfile('.user.ini').read(), b'custom ini')
                    self.assertIn('custom/apps/extra/custom.php', names)
                    self.assertIn('custom/themes/custom/style.css', names)
                    self.assertEqual('data/sentinel' in names, enabled == 'yes')
                    self.assertNotIn('backup.tar', names)
                    self.assertEqual(saved.extractfile('phase').read(), b'backups-ready\n')
                self.assertEqual((recovery / 'backup.tar.sha256').read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())
                self.assertFalse((recovery / 'backup.tar.partial').exists())
                restored = f.root / 'restore'; restored.mkdir()
                subprocess.run([REAL_TOOLS['tar'], '--acls', '--xattrs', '--numeric-owner', '-xpf', str(archive), '-C', str(restored)], check=True, capture_output=True)
                self.assertEqual((restored / 'config/config.php').read_text(), 'configuration')
                self.assertEqual((restored / '.htaccess').read_text(), 'custom Apache settings')
                self.assertEqual((restored / '.user.ini').read_text(), 'custom ini')
                self.assertEqual((restored / 'custom/apps/extra/custom.php').read_text(), 'custom app')
                if enabled == 'yes':
                    self.assertEqual((restored / 'data/sentinel').stat().st_mode & 0o777, 0o604)
                    self.assertEqual(os.readlink(restored / 'data/link'), '/does/not/exist')
                    if 'user.backup-test' in os.listxattr(f.data / 'sentinel'):
                        self.assertEqual(os.getxattr(restored / 'data/sentinel', 'user.backup-test'), b'preserve attributes')

    def test_archive_works_without_custom_components(self):
        f = self.fixture()
        for path in ('custom_apps', 'apps/extra', 'themes/custom'): shutil.rmtree(f.root / 'nextcloud' / path)
        self.assertEqual(f.run_upgrade(extra={'BACKUP_ARCHIVE': 'yes', 'EXTRA_APPS_PATHS': '[]'}).returncode, 0, f.output)
        recovery = next((f.root / 'backups').glob('upgrade-*'))
        with tarfile.open(recovery / 'backup.tar') as saved:
            self.assertIn('config/config.php', saved.getnames())
            self.assertNotIn('custom', saved.getnames())
            self.assertNotIn('data', saved.getnames())

    def test_archive_failures_keep_directory_backup_and_restore_original(self):
        for mode in ('archive-create', 'archive-check'):
            with self.subTest(mode=mode):
                f = self.fixture(relative='data')
                self.assertNotEqual(f.run_upgrade(mode, extra={'BACKUP_DATA': 'yes', 'BACKUP_ARCHIVE': 'yes'}).returncode, 0, f.output)
                self.assert_recovered(f)
                recovery = next((f.root / 'backups').glob('upgrade-*'))
                self.assertTrue((recovery / 'database.sql.gz').exists())
                self.assertTrue((recovery / 'data/sentinel').exists())
                self.assertTrue((recovery / 'backup.tar.partial').exists())
                self.assertFalse((recovery / 'backup.tar').exists())

    def test_changed_data_directory_requires_manual_recovery(self):
        f = self.fixture()
        self.assertNotEqual(f.run_upgrade('data-replaced', extra={'BACKUP_DATA': 'yes'}).returncode, 0, f.output)
        self.assertIn('Configured data directory or its mount changed', f.output)
        self.assertTrue(json.loads((f.root / 'status').read_text())['maintenance'])
        self.assertEqual(json.loads((f.root / 'services').read_text()), {'nginx': 'inactive', 'php8.4-fpm': 'inactive'})
        self.assertTrue((f.root / 'backups/upgrade-active').exists())
        self.assertEqual((f.root / 'original-data/sentinel').read_text(), 'Never modify or duplicate EFS data')
        self.assertNotIn('occ upgrade', (f.root / 'events').read_text())

    def test_internal_data_mount_with_spaces_is_detected_before_maintenance(self):
        f = self.fixture(relative='private/user files')
        self.assertNotEqual(f.run_upgrade('data-mount').returncode, 0, f.output)
        self.assertIn('Mount inside application tree', f.output)
        self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())

    def test_invalid_backup_options_are_rejected_before_changes(self):
        f = self.fixture()
        for extra in ({'BACKUP_ARCHIVE': 'maybe'}, {'BACKUP_SSH_DEST': '-option:/backups'},
                      {'BACKUP_SSH_DEST': 'backup-host:relative'}, {'BACKUP_SSH_DEST': 'backup-host:/'}):
            with self.subTest(extra=extra):
                self.assertNotEqual(f.run_upgrade(extra=extra).returncode, 0, f.output)
                self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())

    def test_ssh_sends_valid_archive_with_safe_remote_path_before_migration(self):
        for archive in ('no', 'yes'):
            with self.subTest(archive=archive):
                f = self.fixture(relative='data')
                remote = f.root / "remote store's $(touch escaped)"; remote.mkdir()
                extra = {'BACKUP_DATA': 'yes', 'BACKUP_ARCHIVE': archive, 'BACKUP_SSH_DEST': 'backup@backup-host:' + str(remote)}
                self.assertEqual(f.run_upgrade(extra=extra).returncode, 0, f.output)
                saved_archive = next(remote.glob('upgrade-*.tar'))
                self.assertEqual(saved_archive.stat().st_mode & 0o777, 0o600)
                with tarfile.open(saved_archive) as saved:
                    self.assertEqual(saved.extractfile('.htaccess').read(), b'custom Apache settings')
                    self.assertEqual(saved.extractfile('.user.ini').read(), b'custom ini')
                    self.assertEqual(saved.extractfile('data/users/.hidden').read(), b'private content')
                    self.assertEqual(saved.extractfile('phase').read(), b'backups-ready\n')
                events = (f.root / 'events').read_text()
                self.assertLess(events.index('ssh check'), events.index('occ maintenance:mode'))
                self.assertLess(events.index('data verify'), events.index('ssh copy'))
                self.assertLess(events.index('ssh copy'), events.index('occ upgrade'))
                self.assertFalse((f.root / 'escaped').exists())

    def test_ssh_failures_prevent_migration_and_preserve_local_backup(self):
        for mode in ('ssh-connect', 'ssh-copy'):
            with self.subTest(mode=mode):
                f = self.fixture(); remote = f.root / 'remote'; remote.mkdir()
                self.assertNotEqual(f.run_upgrade(mode, extra={'BACKUP_SSH_DEST': 'backup@backup-host:' + str(remote)}).returncode, 0, f.output)
                self.assert_recovered(f)
                if mode == 'ssh-connect':
                    self.assertNotIn('occ maintenance:mode', (f.root / 'events').read_text())
                else:
                    recovery = next((f.root / 'backups').glob('upgrade-*'))
                    self.assertTrue((recovery / 'database.sql.gz').exists())

@unittest.skipUnless(REAL_PHP, 'PHP with SimpleXML required; set TEST_PHP')
class WebServerTests(unittest.TestCase):
    def fixture(self, manager, web='apache2', php='php8.4-fpm'):
        cls = MonitUpgradeTests if manager == 'monit' else SystemdUpgradeTests
        fixture = cls('test_success_preserves_config_apps_themes_schedule_and_recovery')
        fixture.setUp()
        fixture.env.update(WEB_SERVICE=web, PHP_SERVICE=php)
        services = {web: 'active'}
        if php != 'none': services[php] = 'active'
        (fixture.root / 'services').write_text(json.dumps(services))
        self.addCleanup(fixture.doCleanups)
        return fixture, services

    def test_apache_service_names_and_php_layouts(self):
        for manager, web, php in (
            ('monit', 'apache2', 'php8.4-fpm'),
            ('monit', 'httpd', 'none'),
            ('systemd', 'apache2', 'none'),
            ('systemd', 'httpd', 'php8.4-fpm'),
        ):
            with self.subTest(manager=manager, web=web, php=php):
                fixture, original = self.fixture(manager, web, php)
                self.assertEqual(fixture.run_upgrade().returncode, 0, fixture.output)
                self.assertEqual(json.loads((fixture.root / 'services').read_text()), original)
                self.assertFalse(json.loads((fixture.root / 'status').read_text())['maintenance'])
                self.assertEqual((fixture.root / 'cron').read_text(), fixture.original_cron)
                events = (fixture.root / 'events').read_text().splitlines()
                actions = [line for line in events if line.startswith(('monit start ', 'monit stop ', 'systemctl start ', 'systemctl stop '))]
                stop = [web] + ([php] if php != 'none' else [])
                expected = [f'{fixture.service_command} stop {service}' for service in stop]
                expected += [f'{fixture.service_command} start {service}' for service in reversed(stop)]
                self.assertEqual(actions, expected)
                manifest = next((fixture.root / 'backups').glob('upgrade-*/manifest')).read_text()
                self.assertIn('web_service=' + web + '\n', manifest)
                self.assertIn('php_service=' + php + '\n', manifest)

    def test_apache_failures_recover_or_remain_blocked(self):
        for manager in ('monit', 'systemd'):
            for mode in ('backup', 'new-rename', 'migration', 'start-apache2', 'stop-timeout'):
                with self.subTest(manager=manager, mode=mode):
                    fixture, original = self.fixture(manager, php='none')
                    self.assertNotEqual(fixture.run_upgrade(mode).returncode, 0, fixture.output)
                    status = json.loads((fixture.root / 'status').read_text())
                    services = json.loads((fixture.root / 'services').read_text())
                    marker = fixture.root / 'backups/upgrade-active'
                    if mode in ('backup', 'new-rename'):
                        self.assertEqual((fixture.root / 'nextcloud/occ').read_text(), 'old-code')
                        self.assertEqual(services, original)
                        self.assertFalse(status['maintenance'])
                        self.assertFalse(marker.exists())
                        self.assertEqual((fixture.root / 'cron').read_text(), fixture.original_cron)
                    else:
                        self.assertTrue(status['maintenance'], fixture.output)
                        self.assertTrue(marker.exists(), fixture.output)
                        self.assertIn('# nextcloud-upgrade:', (fixture.root / 'cron').read_text())
                        if mode != 'stop-timeout':
                            self.assertTrue(all(state == 'inactive' for state in services.values()))
                            self.assertEqual((fixture.root / 'nextcloud/occ').read_text(), 'new-code')
                    self.assertEqual((fixture.root / 'storage/sentinel').read_text(), 'Never modify or duplicate EFS data')
                    self.assertNotIn('php8.4-fpm', (fixture.root / 'events').read_text())

    def test_nginx_without_separate_php_is_rejected(self):
        for manager in ('monit', 'systemd'):
            with self.subTest(manager=manager):
                fixture, services = self.fixture(manager, web='nginx', php='none')
                self.assertNotEqual(fixture.run_upgrade().returncode, 0, fixture.output)
                self.assertIn('Nginx requires a separate PHP-FPM service', fixture.output)
                self.assertEqual(json.loads((fixture.root / 'services').read_text()), services)
                self.assertFalse((fixture.root / 'backups/upgrade-active').exists())

    def test_same_web_and_php_unit_is_rejected(self):
        for php in ('apache2', 'apache2.service'):
            with self.subTest(php=php):
                fixture, services = self.fixture('systemd', php=php)
                self.assertNotEqual(fixture.run_upgrade().returncode, 0, fixture.output)
                self.assertIn('WEB_SERVICE and PHP_SERVICE must be different', fixture.output)
                self.assertEqual(json.loads((fixture.root / 'services').read_text()), services)
                self.assertFalse((fixture.root / 'backups/upgrade-active').exists())

if __name__ == '__main__': unittest.main(verbosity=2)
