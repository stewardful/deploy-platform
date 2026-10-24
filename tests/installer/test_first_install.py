"""Standalone CLI and first-install tests; all mutations are confined to temp directories."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/installer/lib'))
import first_install as first
import update_worker as worker
spec = importlib.util.spec_from_file_location('cli_package', ROOT/'distribution/cli/package.py')
package = importlib.util.module_from_spec(spec); spec.loader.exec_module(package)


class FirstInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for key, value in [('CONFIG', self.root/'etc/updates.json'), ('SPOOL', self.root/'updates'),
                           ('CURRENT', self.root/'installed/manifest.json'), ('TRANSACTION', self.root/'transaction')]:
            self.stack.enter_context(patch.object(worker, key, value))
        self.stack.enter_context(patch.object(first, 'CLI_ROOT', self.root))
        self.preflight = self.stack.enter_context(patch.object(first, 'preflight'))
        self.stack.enter_context(patch.object(first.os, 'geteuid', return_value=0))
        self.stack.enter_context(patch.object(first, 'directory', side_effect=lambda p: p.mkdir(parents=True, exist_ok=True, mode=0o700)))
        self.stack.enter_context(patch.object(first.platform, 'machine', return_value='x86_64'))
        self.launch = self.stack.enter_context(patch.object(first.subprocess, 'run'))
        self.stack.enter_context(patch.object(worker, 'ACTIVE_DETAILS', {}))
        import os
        mask = os.umask(0o022); self.addCleanup(os.umask, mask)

    def test_dev_install_pins_candidate_and_launches_existing_worker_without_token_in_request(self):
        request = {'channel': 'dev', 'repository': 'owner/repo', 'run_id': '42', 'commit': 'a'*40, 'arch': 'amd64'}
        with patch('dev_update.discover', return_value=request) as discover:
            first.main(['--channel', 'dev', '--repository', 'owner/repo', '--no-wait'])
        discover.assert_called_once_with(None, initial=True)
        self.assertEqual(json.loads(worker.CONFIG.read_text())['token_file'], '')
        value = json.loads((worker.SPOOL/'request.json').read_text())
        self.assertEqual(value['operation'], 'install')
        self.assertEqual(value['commit'], 'a'*40)
        self.assertNotIn('private-token', json.dumps(value))
        command = self.launch.call_args.args[0]
        self.assertIn('--no-block', command)
        self.assertIn(str(ROOT/'distribution/installer/lib/update_worker.py'), command)
        self.assertEqual((worker.SPOOL/'request.json').stat().st_mode & 0o077, 0)

    def test_public_dev_install_rejects_unnecessary_private_token_before_host_mutation(self):
        with self.assertRaisesRegex(first.InstallError, 'only used for stable'):
            first.main(['--channel','dev','--token-file',str(self.root/'missing-token')])
        self.preflight.assert_not_called(); self.launch.assert_not_called()
        self.assertFalse(worker.CONFIG.exists())

    def test_stable_first_install_pins_and_revalidates_latest_release_asset(self):
        release = {'tag_name': 'v0.5.3', 'draft': False, 'prerelease': False,
                   'assets': [{'name': 'deploy-platform-0.5.3-linux-amd64.run', 'id': 12, 'size': 100, 'digest': 'sha256:'+'a'*64}]}
        with patch.object(worker, 'open_github', side_effect=lambda *a, **k: io.BytesIO(json.dumps(release).encode())) as github:
            first.main(['--channel', 'stable', '--repository', 'owner/repo', '--no-wait'])
        value = json.loads((worker.SPOOL/'request.json').read_text())
        self.assertEqual((value['version'], value['asset_id'], value['operation']), ('0.5.3', 12, 'install'))
        self.assertTrue(github.call_args_list[0].args[0].endswith('/latest'))
        self.assertTrue(github.call_args_list[1].args[0].endswith('/tags/v0.5.3'))

    def test_installed_node_is_rejected_without_preflight_or_queue(self):
        worker.CURRENT.parent.mkdir(); worker.CURRENT.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'already installed'):
            first.main(['--channel', 'dev', '--no-wait'])
        self.preflight.assert_not_called(); self.launch.assert_not_called()
        self.assertFalse(worker.CONFIG.exists())

    def test_bad_inputs_and_public_token_do_not_queue_or_launch(self):
        token = self.root/'token'; token.write_text('not-private'); token.chmod(0o644)
        for args in (['--channel', 'stable', '--run-id', '42'], ['--channel', 'dev', '--version', '1.0.0'],
                     ['--file', '/missing', '--token-file', str(token)], ['--channel', 'dev', '--token-file', str(token)]):
            with self.subTest(args=args), self.assertRaises(ValueError): first.main(args)
        self.assertFalse(worker.SPOOL.exists()); self.launch.assert_not_called()

    def test_offline_digest_failure_never_executes_installer(self):
        worker.SPOOL.mkdir(); source = worker.SPOOL/'first-install.run'; source.write_bytes(b'tampered'); source.chmod(0o600)
        with patch.object(first, 'trusted'), patch.object(worker, 'install_payload') as install:
            with self.assertRaisesRegex(ValueError, 'changed'): first.execute_offline({'digest': '0'*64})
            install.assert_not_called()

    def test_offline_install_requires_commit_before_reporting_success(self):
        worker.SPOOL.mkdir(); source = worker.SPOOL/'first-install.run'; source.write_bytes(b'payload'); source.chmod(0o600)
        request = {'digest': hashlib.sha256(b'payload').hexdigest(), 'version': '0.5.3', 'arch': 'amd64', 'build': None}
        def install(*args, **kwargs):
            worker.CURRENT.parent.mkdir(exist_ok=True); worker.CURRENT.write_text(json.dumps({'version': '0.5.3', 'arch': 'amd64'}))
        with patch.object(first, 'trusted'), patch.object(worker, 'install_payload', side_effect=install) as run:
            first.execute_offline(request)
            run.assert_called_once_with(source, '0.5.3', initial=True)
        self.assertEqual(json.loads((worker.SPOOL/'result.json').read_text())['phase'], 'succeeded')
        self.assertFalse(source.exists())

    def test_first_and_upgrade_use_same_installer_with_explicit_modes(self):
        with patch.object(worker, 'report'), patch('archive.installer_manifest', return_value={'version': '0.5.3'}):
            worker.install_payload('/installer.run', '0.5.3', initial=True)
            self.assertEqual(self.launch.call_args.args[0], ['/usr/bin/bash', '/installer.run'])
            worker.install_payload('/installer.run', '0.5.3')
            self.assertEqual(self.launch.call_args.args[0][-2:], ['--upgrade', '--skip-apt'])


class StandaloneCliTests(unittest.TestCase):
    def test_standalone_help_version_and_install_help_work_without_platform_or_extra_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); cli = package.build(root)
            for args, expected in ((['--help'], 'install --channel'), (['install', '--help'], '--token-file'), (['version'], '"installed": false')):
                result = subprocess.run([sys.executable, '-I', '-B', str(cli), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
            poison = root/'poison'; poison.mkdir()
            marker = root/'injected'
            (poison/'sitecustomize.py').write_text('open('+repr(str(marker))+', \"w\").write(\"bad\")')
            import os
            direct = subprocess.run([str(cli), '--help'], env=dict(os.environ, PYTHONPATH=str(poison)), capture_output=True, text=True)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertFalse(marker.exists())
            self.assertLess(cli.stat().st_size, 256*1024)
            self.assertEqual((root/'cloudctl.sha256').read_text().split()[0], hashlib.sha256(cli.read_bytes()).hexdigest())

    def test_tampered_library_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            cli = package.build(Path(temp))
            with zipfile.ZipFile(cli, 'a') as archive:
                data = json.loads(archive.read('cli.json'))
                # Keep a valid inventory shape but make the library hash false.
                data['files']['lib/node_cli.py'] = '0'*64
                items = {name: archive.read(name) for name in archive.namelist() if name != 'cli.json'}
            cli.write_bytes(b'#!/usr/bin/python3\n')
            with zipfile.ZipFile(cli, 'a') as archive:
                for name, value in items.items(): archive.writestr(name, value)
                archive.writestr('cli.json', json.dumps(data))
            result = subprocess.run([sys.executable, '-I', '-B', str(cli), '--help'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('cloudctl —', result.stdout)
