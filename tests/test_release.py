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
Path(os.environ['GH_CALL']).write_text(json.dumps(sys.argv[1:]))
sys.exit(int(os.environ.get('GH_TEST_EXIT', '0')))
''')
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'], GH_CALL=str(root / 'gh-call.json'))
        return root, scripts / 'prepare-release.sh', env

    def test_draft_uploads_exact_assets_and_never_publishes(self):
        for prerelease in (False, True):
            with self.subTest(prerelease=prerelease), tempfile.TemporaryDirectory(prefix='nc-draft-test-') as directory:
                version = VERSION + '-rc.1' if prerelease else VERSION
                tag = 'v' + version
                root, helper, env = self.draft_fixture(directory, version)
                # A previous build must not become an extra release asset.
                (root / 'dist').mkdir()
                (root / 'dist/unrelated.tar.gz').write_text('leave out')
                result = subprocess.run(['/bin/bash', str(helper), tag], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                archive = f'dist/nextcloud-upgrade-{tag}.tar.gz'
                args = json.loads((root / 'gh-call.json').read_text())
                expected = ['release', 'create', tag, archive, archive + '.sha256', '--draft', '--verify-tag', '--title', tag, '--generate-notes']
                if prerelease: expected.append('--prerelease')
                self.assertEqual(args, expected)
                subprocess.run(['sha256sum', '-c', Path(archive).name + '.sha256'], cwd=root / 'dist', check=True, capture_output=True)

    def test_invalid_tag_prevents_any_github_call(self):
        with tempfile.TemporaryDirectory(prefix='nc-draft-invalid-') as directory:
            root, helper, env = self.draft_fixture(directory)
            result = subprocess.run(['/bin/bash', str(helper), TAG + '-mismatch'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'gh-call.json').exists())

    def test_draft_creation_failure_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='nc-draft-failure-') as directory:
            root, helper, env = self.draft_fixture(directory)
            result = subprocess.run(['/bin/bash', str(helper), TAG], env=dict(env, GH_TEST_EXIT='23'), capture_output=True, text=True)
            self.assertEqual(result.returncode, 23)
            args = json.loads((root / 'gh-call.json').read_text())
            self.assertIn('--draft', args)


if __name__ == '__main__':
    unittest.main(verbosity=2)
