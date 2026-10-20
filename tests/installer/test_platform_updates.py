"""Real queues and archives in temp directories; fake GitHub, installer and node only."""
import contextlib
import copy
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/vendor/deploy-delivery/scripts'))
sys.path.insert(0, str(ROOT/'distribution/installer/lib'))
import dev_update as dev
import platform_updates as cli
import update_worker as worker


class PlatformUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('SPOOL', self.root), ('DOWNLOAD_ROOT', self.root), ('CURRENT', self.root/'manifest.json'), ('TRANSACTION', self.root/'transaction')]:
            ctx = patch.object(worker, name, value); ctx.start(); self.addCleanup(ctx.stop)
        worker.CURRENT.write_text(json.dumps({'version': '0.5.3', 'arch': 'amd64'}))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(worker, 'settings', return_value=('owner/repo', 'private-token')))
        self.stack.enter_context(patch.object(dev.platform, 'machine', return_value='x86_64'))
        self.stack.enter_context(patch.object(worker, 'ACTIVE_DETAILS', {}))
        self.run = {'id': 42, 'path': dev.WORKFLOW, 'event': 'push', 'head_branch': 'dev', 'head_sha': 'a'*40, 'status': 'completed', 'conclusion': 'failure', 'repository': {'full_name': 'owner/repo', 'id': 1}, 'head_repository': {'full_name': 'owner/repo'}}
        self.jobs = [{'name': 'build / test', 'conclusion': 'success'}, {'name': 'build / package (ubuntu-24.04, amd64)', 'conclusion': 'success'}]
        self.artifact = {'name': 'deploy-platform-linux-amd64', 'id': 12, 'size_in_bytes': 100, 'digest': 'sha256:'+'b'*64, 'expired': False,
                         'workflow_run': {'id': 42, 'head_branch': 'dev', 'head_sha': 'a'*40, 'repository_id': 1, 'head_repository_id': 1}}
        def meta(repo, token, path):
            self.assertEqual((repo, token), ('owner/repo', 'private-token'))
            if '/jobs?' in path: return {'jobs': self.jobs}
            if '/artifacts?' in path: return {'artifacts': [self.artifact]}
            return self.run
        self.metadata = self.stack.enter_context(patch.object(dev, 'metadata', side_effect=meta))

    def test_dev_accepts_completed_build_when_only_ssh_deployment_failed(self):
        request = dev.discover('42')
        self.assertEqual(request['channel'], 'dev'); self.assertEqual(request['commit'], 'a'*40)
        self.assertNotIn('private-token', json.dumps(request))

    def test_dev_rejects_other_sources_forks_and_unfinished_or_failed_builds(self):
        original = copy.deepcopy(self.run)
        for changes in ({'path': '.github/workflows/other.yml'}, {'event': 'pull_request'}, {'head_branch': 'main'},
                        {'head_repository': {'full_name': 'fork/repo'}}, {'status': 'in_progress'}, {'conclusion': 'cancelled'}, {'head_sha': 'bad'}):
            self.run.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): dev.discover('42')
            self.run.clear(); self.run.update(copy.deepcopy(original))
        self.jobs[0]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): dev.discover('42')

    def test_dev_rejects_artifact_substitution_expiry_digest_and_architecture(self):
        original = copy.deepcopy(self.artifact)
        for changes in ({'expired': True}, {'digest': ''}, {'name': 'deploy-platform-linux-arm64'}, {'size_in_bytes': worker.MAX_INSTALLER+1},
                        {'workflow_run': dict(self.artifact['workflow_run'], head_sha='c'*40)}):
            self.artifact.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): dev.discover('42')
            self.artifact.clear(); self.artifact.update(copy.deepcopy(original))

    def test_dev_inspection_is_read_only_and_yes_queues_private_request(self):
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()):
            cli.main(['upgrade-dev', '--run-id', '42'])
            self.assertFalse((self.root/'request.json').exists())
            cli.main(['upgrade-dev', '--run-id', '42', '--yes'])
        path = self.root/'request.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['run_id'], '42')
        with self.assertRaises(ValueError): cli.queue(dev.discover('42'))
        path.unlink(); (self.root/'active.json').write_text('{}')
        with self.assertRaises(ValueError): cli.queue(dev.discover('42'))

    def test_stable_commands_use_real_existing_api_paths_and_exact_approved_version(self):
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'api', return_value={'enabled': True}) as api, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['update', 'check']); api.assert_called_with('/check', {})
            cli.main(['upgrade', '--version', '0.5.4', '--yes']); api.assert_called_with('/upgrade', {'version': '0.5.4'})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): cli.main(['upgrade', '--version', '0.5.4'])
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'api', return_value={'enabled': True, 'error': 'unavailable'}), self.assertRaises(ValueError):
            cli.main(['update', 'check'])

    def test_status_survives_control_outage_and_detects_interruption(self):
        (self.root/'active.json').write_text(json.dumps(dev.discover('42'))); (self.root/'active.json').chmod(0o600)
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'api', side_effect=AssertionError('must not depend on control')), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(['update', 'status'])
        self.assertEqual(json.loads(output.getvalue())['execution']['phase'], 'interrupted')

    def test_dev_redirect_strips_credential_and_rejects_untrusted_hosts(self):
        req = urllib.request.Request('https://api.github.com/repos/owner/repo/actions/artifacts/12/zip', headers={'Authorization': 'Bearer private-token'})
        target = dev.Redirect().redirect_request(req, None, 302, '', {}, 'https://example.blob.core.windows.net/blob?signature=private')
        self.assertFalse(target.has_header('Authorization'))
        for url in ('https://evil.example/a', 'http://github.com/a', 'https://user:pass@example.blob.core.windows.net/a'):
            with self.assertRaises(Exception): dev.Redirect().redirect_request(req, None, 302, '', {}, url)

    def make_zip(self, extra=False, wrong_checksum=False):
        source = self.root/'artifact.zip'
        stem = 'deploy-platform-0.5.3-linux-amd64'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr(stem+'.run', b'fake installer')
            archive.writestr(stem+'.sha256', ('0'*64 if wrong_checksum else hashlib.sha256(b'fake installer').hexdigest())+'  '+stem+'.run'+'\n')
            if extra: archive.writestr('../escape', 'bad')
        return source

    def test_closed_zip_extractor_is_reused(self):
        self.assertEqual(dev.extract(self.make_zip(), self.root, dev.discover('42')), '0.5.3')
        self.assertEqual((self.root/'installer.run').read_bytes(), b'fake installer')
        (self.root/'installer.run').unlink()
        with self.assertRaises(Exception): dev.extract(self.make_zip(wrong_checksum=True), self.root, dev.discover('42'))
        with self.assertRaises(Exception): dev.extract(self.make_zip(extra=True), self.root, dev.discover('42'))
        self.assertFalse((self.root.parent/'escape').exists())

    def test_changed_artifact_or_failed_download_never_invokes_installer(self):
        request = dev.discover('42'); self.artifact['id'] = 13
        with patch.object(dev.subprocess, 'run') as run:
            with self.assertRaises(ValueError): dev.execute(request)
            run.assert_not_called()
        self.artifact['id'] = 12
        with patch.object(worker, 'download', side_effect=ValueError('bad digest')), patch.object(dev.subprocess, 'run') as run:
            with self.assertRaises(ValueError): dev.execute(request)
            run.assert_not_called()

    def test_dev_worker_reuses_installer_and_verifies_committed_result(self):
        request = dev.discover('42')
        candidate = {'version': '0.5.3', 'arch': 'amd64', 'build': {'channel': 'dev', 'commit': 'a'*40}}
        def install(argv, **kwargs):
            if '--upgrade' in argv: worker.CURRENT.write_text(json.dumps(candidate))
        with patch.object(worker, 'download'), patch.object(dev, 'extract', return_value='0.5.3'), patch('archive.installer_manifest', return_value=candidate), patch.object(dev.subprocess, 'run', side_effect=install) as run:
            dev.execute(request)
            self.assertEqual(run.call_args_list[0].args[0][-1], '--check')
            self.assertEqual(run.call_args_list[1].args[0][-2:], ['--upgrade', '--skip-apt'])
            self.assertEqual(run.call_count, 2)
        self.assertEqual(json.loads((self.root/'result.json').read_text())['phase'], 'succeeded')
        candidate['build']['commit'] = 'c'*40
        with patch.object(worker, 'download'), patch.object(dev, 'extract', return_value='0.5.3'), patch('archive.installer_manifest', return_value=candidate), patch.object(dev.subprocess, 'run') as run:
            with self.assertRaises(ValueError): dev.execute(request)
            run.assert_not_called()

    def test_dev_failed_installer_is_not_retried_and_records_failure(self):
        request = dev.discover('42'); cli.queue(request)
        with patch.object(dev, 'execute', side_effect=ValueError('secret-token')) as execute:
            with self.assertRaises(RuntimeError): worker.main()
            worker.main(); self.assertEqual(execute.call_count, 1)
        result = json.loads((self.root/'result.json').read_text())
        self.assertEqual(result['phase'], 'failed'); self.assertEqual(result['channel'], 'dev')
        self.assertEqual(result['run_id'], '42'); self.assertNotIn('secret-token', json.dumps(result))

    def test_installer_failure_and_uncommitted_transaction_never_report_success(self):
        import subprocess
        request = dev.discover('42')
        candidate = {'version': '0.5.3', 'arch': 'amd64', 'build': {'channel': 'dev', 'commit': 'a'*40}}
        for failure in ('check', 'upgrade', 'transaction', 'no_activation'):
            def install(argv, **kwargs):
                if failure == 'check' and '--check' in argv: raise subprocess.CalledProcessError(1, argv)
                if failure == 'upgrade' and '--upgrade' in argv: raise subprocess.CalledProcessError(1, argv)
                if failure == 'transaction' and '--upgrade' in argv: worker.TRANSACTION.write_text('{}')
            with self.subTest(failure=failure), patch.object(worker, 'download'), patch.object(dev, 'extract', return_value='0.5.3'), patch('archive.installer_manifest', return_value=candidate), patch.object(dev.subprocess, 'run', side_effect=install) as run:
                with self.assertRaises((ValueError, subprocess.CalledProcessError)): dev.execute(request)
                self.assertEqual(run.call_count, 1 if failure == 'check' else 2)
                self.assertNotEqual(json.loads((self.root/'result.json').read_text())['phase'], 'succeeded')
            worker.TRANSACTION.unlink(missing_ok=True)

    def test_preview_is_checked_not_queued_and_new_request_clears_old_success(self):
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(['upgrade-dev', '--run-id', '42'])
        self.assertEqual(json.loads(output.getvalue())['phase'], 'checked')
        (self.root/'result.json').write_text('{"phase":"succeeded"}')
        cli.queue(dev.discover('42'))
        self.assertFalse((self.root/'result.json').exists())
        self.assertEqual(json.loads((self.root/'request.json').read_text())['phase'], 'queued')
