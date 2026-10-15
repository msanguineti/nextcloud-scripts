#!/usr/bin/env python3
"""Exercise the actual download, version guard and installation without root/network."""
import os
from pathlib import Path
import re
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGER = ROOT / '.github/scripts/package-release.sh'
VERSION = re.search(r"^readonly SCRIPT_VERSION='([^']+)'$", (ROOT / 'nextcloud-upgrade.sh').read_text(), re.M).group(1)
TAG = 'v' + VERSION


class ReleaseTests(unittest.TestCase):
    def test_version_and_help_do_not_prepare_an_upgrade(self):
        env = dict(os.environ, PATH='/no-tools', SERVICE_MANAGER='invalid')
        script = ROOT / 'nextcloud-upgrade.sh'
        result = subprocess.run(['/bin/bash', str(script), '--version'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(VERSION, r'^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$')
        self.assertEqual(result.stdout, f'nextcloud-upgrade {VERSION}\n')
        help_result = subprocess.run(['/bin/bash', str(script), '--help'], env=env, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(f'Nextcloud Upgrade Script {VERSION}', help_result.stdout)
        invalid = subprocess.run(['/bin/bash', str(script), '--version', 'extra'], env=env, capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)

    def test_package_is_complete_installable_and_reproducible(self):
        with tempfile.TemporaryDirectory(prefix='nc-release-test-') as directory:
            work = Path(directory)
            output = work / 'downloads with spaces'
            result = subprocess.run(['/bin/bash', str(PACKAGER), TAG, str(output)], cwd=work, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            archive = output / f'nextcloud-upgrade-{TAG}.tar.gz'
            first_build = archive.read_bytes()
            subprocess.run(['sha256sum', '-c', archive.name + '.sha256'], cwd=output, check=True, capture_output=True)
            prefix = 'nextcloud-upgrade-' + TAG
            with tarfile.open(archive) as saved:
                self.assertEqual(set(saved.getnames()), {prefix, *(prefix + '/' + name for name in ('nextcloud-upgrade.sh', 'README.md', 'LICENSE.md'))})
                for name in ('nextcloud-upgrade.sh', 'README.md', 'LICENSE.md'):
                    entry = saved.getmember(prefix + '/' + name)
                    self.assertTrue(entry.isfile())
                    self.assertEqual(entry.mode, 0o755 if name.endswith('.sh') else 0o644)
                    self.assertEqual(saved.extractfile(entry).read(), (ROOT / name).read_bytes())
            restored = work / 'unpacked'; restored.mkdir()
            subprocess.run(['tar', '-xzf', str(archive), '-C', str(restored)], check=True, capture_output=True)
            installed = work / 'nextcloud-upgrade'
            subprocess.run(['install', '-m', '0755', str(restored / prefix / 'nextcloud-upgrade.sh'), str(installed)], check=True)
            version = subprocess.run([str(installed), '--version'], check=True, capture_output=True, text=True)
            self.assertEqual(version.stdout, f'nextcloud-upgrade {VERSION}\n')
            subprocess.run(['/bin/bash', str(PACKAGER), TAG, str(output)], check=True, capture_output=True)
            self.assertEqual(archive.read_bytes(), first_build)

    def test_bad_or_mismatched_tags_do_not_create_a_package(self):
        with tempfile.TemporaryDirectory(prefix='nc-release-invalid-') as directory:
            output = Path(directory) / 'dist'
            for tag in ('../escape', '-option', TAG + '/path', TAG + ';echo injected', TAG + '-mismatch'):
                with self.subTest(tag=tag):
                    result = subprocess.run(['/bin/bash', str(PACKAGER), tag, str(output)], capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())

    def draft_fixture(self, directory, version=VERSION):
        root = Path(directory)
        scripts = root / '.github/scripts'
        scripts.mkdir(parents=True)
        for name in ('nextcloud-upgrade.sh', 'README.md', 'LICENSE.md'):
            shutil.copyfile(ROOT / name, root / name)
        script = root / 'nextcloud-upgrade.sh'
        script.write_text(script.read_text().replace(f"SCRIPT_VERSION='{VERSION}'", f"SCRIPT_VERSION='{version}'"))
        for name in ('package-release.sh', 'prepare-release.sh'):
            shutil.copyfile(PACKAGER.parent / name, scripts / name)
        binary = root / 'bin'; binary.mkdir()
        gh = binary / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(os.environ['GH_CALL'])
calls = json.loads(path.read_text()) if path.exists() else []
calls.append(args); path.write_text(json.dumps(calls))
root = path.parent
if args[:3] == ['api', '--include', '--silent']:
    assert args[3].endswith('/git/ref/tags/' + os.environ['GH_EXPECTED_TAG']), args
    status = os.environ.get('GH_TAG_STATUS', '404')
    if status != 'network': print('HTTP/2.0 ' + status + ' response\\n')
    sys.exit(0 if status == '200' else 1)
elif args[:2] == ['api', '--paginate']:
    assert args[2].endswith('/releases?per_page=100'), args
    assert args[3:] == ['--jq', '.[] | select(.tag_name == "' + os.environ['GH_EXPECTED_TAG'] + '") | .tag_name'], args
    if os.environ.get('GH_RELEASE_ERROR'): sys.exit(1)
    if os.environ.get('GH_RELEASE_EXISTS'): print(os.environ['GH_EXPECTED_TAG'])
elif args[:3] == ['api', '--method', 'POST']:
    if os.environ.get('GH_POST_EXIT'): sys.exit(int(os.environ['GH_POST_EXIT']))
    assert args[3] == 'repos/example/nextcloud/git/refs', args
    assert args[4:] == ['-f', 'ref=refs/tags/' + os.environ['GH_EXPECTED_TAG'], '-f', 'sha=' + os.environ['GITHUB_SHA'], '--silent'], args
    (root / 'tag-created.json').write_text(json.dumps({'tag': os.environ['GH_EXPECTED_TAG'], 'sha': os.environ['GITHUB_SHA']}))
elif args[:2] == ['release', 'create']:
    assert (root / 'tag-created.json').exists(), 'Tag must be created first'
    sys.exit(int(os.environ.get('GH_TEST_EXIT', '0')))
else: raise AssertionError(args)
''')
        gh.chmod(0o755)
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '--quiet', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'add', 'nextcloud-upgrade.sh', 'README.md', 'LICENSE.md', '.github'], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'user.name=Release test', '-c', 'user.email=release@example.invalid',
                        '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '--quiet', '-m', 'Release fixture'], check=True)
        commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'], GH_CALL=str(root / 'gh-call.json'),
                   GH_REPO='example/nextcloud', GH_EXPECTED_TAG='v' + version,
                   GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main', GITHUB_SHA=commit,
                   GITHUB_OUTPUT=str(root / 'job-output'))
        return root, scripts / 'prepare-release.sh', env

    def gh_calls(self, root):
        path = root / 'gh-call.json'
        return json.loads(path.read_text()) if path.exists() else []

    def assert_no_remote_writes(self, root):
        calls = self.gh_calls(root)
        self.assertFalse(any(call[:3] == ['api', '--method', 'POST'] or call[:2] == ['release', 'create'] for call in calls), calls)

    def test_draft_uploads_exact_assets_and_never_publishes(self):
        for prerelease in (False, True):
            with self.subTest(prerelease=prerelease), tempfile.TemporaryDirectory(prefix='nc-draft-test-') as directory:
                version = VERSION + '-rc.1' if prerelease else VERSION
                tag = 'v' + version
                root, helper, env = self.draft_fixture(directory, version)
                # A previous build must not become an extra release asset.
                (root / 'dist').mkdir()
                (root / 'dist/unrelated.tar.gz').write_text('leave out')
                result = subprocess.run(['/bin/bash', str(helper), 'create'], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                archive = f'dist/nextcloud-upgrade-{tag}.tar.gz'
                calls = self.gh_calls(root)
                self.assertEqual(len(calls), 4, calls)
                args = calls[-1]
                expected = ['release', 'create', tag, archive, archive + '.sha256', '--draft', '--verify-tag', '--title', tag, '--generate-notes']
                if prerelease: expected.append('--prerelease')
                self.assertEqual(args, expected)
                self.assertEqual(json.loads((root / 'tag-created.json').read_text()), {'tag': tag, 'sha': env['GITHUB_SHA']})
                subprocess.run(['sha256sum', '-c', Path(archive).name + '.sha256'], cwd=root / 'dist', check=True, capture_output=True)

    def test_invalid_script_version_prevents_any_github_call(self):
        with tempfile.TemporaryDirectory(prefix='nc-draft-invalid-') as directory:
            root, helper, env = self.draft_fixture(directory, '../invalid')
            result = subprocess.run(['/bin/bash', str(helper), 'create'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'gh-call.json').exists())

    def test_release_requires_manual_main_and_matching_clean_commit(self):
        cases = ({'GITHUB_EVENT_NAME': 'push'}, {'GITHUB_REF': 'refs/heads/feature'},
                 {'GITHUB_REF': 'refs/tags/' + TAG}, {'GITHUB_SHA': '0' * 40}, {'GITHUB_SHA': ''})
        for extra in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory(prefix='nc-release-guard-') as directory:
                root, helper, env = self.draft_fixture(directory)
                result = subprocess.run(['/bin/bash', str(helper), 'create'], env=dict(env, **extra), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.gh_calls(root), [])
        with tempfile.TemporaryDirectory(prefix='nc-release-dirty-') as directory:
            root, helper, env = self.draft_fixture(directory)
            (root / 'README.md').write_text('changed after checkout')
            result = subprocess.run(['/bin/bash', str(helper), 'create'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.gh_calls(root), [])

    def test_preflight_is_read_only_and_records_the_selected_commit(self):
        with tempfile.TemporaryDirectory(prefix='nc-release-check-') as directory:
            root, helper, env = self.draft_fixture(directory)
            result = subprocess.run(['/bin/bash', str(helper), 'check'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'job-output').read_text(), 'commit=' + env['GITHUB_SHA'] + '\n')
            self.assertFalse((root / 'dist').exists())
            self.assertEqual(len(self.gh_calls(root)), 2)
            self.assert_no_remote_writes(root)

    def test_existing_versions_and_api_errors_prevent_creation(self):
        cases = ({'GH_TAG_STATUS': '200'}, {'GH_RELEASE_EXISTS': '1'}, {'GH_TAG_STATUS': '403'},
                 {'GH_TAG_STATUS': '500'}, {'GH_TAG_STATUS': 'network'}, {'GH_RELEASE_ERROR': '1'})
        for extra in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory(prefix='nc-release-existing-') as directory:
                root, helper, env = self.draft_fixture(directory)
                result = subprocess.run(['/bin/bash', str(helper), 'create'], env=dict(env, **extra), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assert_no_remote_writes(root)
                self.assertFalse((root / 'dist').exists())

    def test_version_is_rechecked_after_preflight(self):
        with tempfile.TemporaryDirectory(prefix='nc-release-race-') as directory:
            root, helper, env = self.draft_fixture(directory)
            subprocess.run(['/bin/bash', str(helper), 'check'], env=env, check=True, capture_output=True)
            result = subprocess.run(['/bin/bash', str(helper), 'create'], env=dict(env, GH_TAG_STATUS='200'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assert_no_remote_writes(root)

    def test_tag_creation_conflict_never_creates_a_draft(self):
        with tempfile.TemporaryDirectory(prefix='nc-release-tag-race-') as directory:
            root, helper, env = self.draft_fixture(directory)
            result = subprocess.run(['/bin/bash', str(helper), 'create'], env=dict(env, GH_POST_EXIT='23'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(call[:2] == ['release', 'create'] for call in self.gh_calls(root)))

    def test_draft_creation_failure_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='nc-draft-failure-') as directory:
            root, helper, env = self.draft_fixture(directory)
            result = subprocess.run(['/bin/bash', str(helper), 'create'], env=dict(env, GH_TEST_EXIT='23'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Tag ' + TAG + ' was created, but draft preparation failed', result.stderr)
            args = self.gh_calls(root)[-1]
            self.assertIn('--draft', args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
