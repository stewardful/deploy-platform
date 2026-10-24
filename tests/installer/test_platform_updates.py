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
        self.jobs = [{'name': 'build / test', 'conclusion': 'success'}, {'name': 'build / package (ubuntu-24.04, amd64)', 'conclusion': 'success'}, {'name': 'build / cli', 'conclusion': 'success'}]
        self.artifact = {'name': 'deploy-platform-linux-amd64', 'id': 12, 'size_in_bytes': 100, 'digest': 'sha256:'+'b'*64, 'expired': False,
                         'workflow_run': {'id': 42, 'head_branch': 'dev', 'head_sha': 'a'*40, 'repository_id': 1, 'head_repository_id': 1}}
        def meta(repo, token, path):
            self.assertEqual((repo, token), ('owner/repo', 'private-token'))
            if '/jobs?' in path: return {'jobs': self.jobs}
            if '/artifacts?' in path: return {'artifacts': [self.artifact]}
            return self.run
        self.metadata = self.stack.enter_context(patch.object(dev, 'metadata', side_effect=meta))

    def test_first_install_can_select_dev_without_an_installed_manifest(self):
        worker.CURRENT.unlink()
        with self.assertRaises(FileNotFoundError): dev.discover('42')
        self.assertEqual(dev.discover('42', initial=True)['commit'], 'a'*40)

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

    def test_dev_first_install_downloads_before_install_and_does_not_pass_upgrade_flags(self):
        request = dict(dev.discover('42'), operation='install')
        worker.CURRENT.unlink()
        candidate = {'version': '0.5.3', 'arch': 'amd64', 'build': {'channel': 'dev', 'commit': 'a'*40}}
        def install(argv, **kwargs):
            if '--check' not in argv: worker.CURRENT.write_text(json.dumps(candidate))
        with patch.object(worker, 'download'), patch.object(dev, 'extract', return_value='0.5.3'), patch('archive.installer_manifest', return_value=candidate), patch.object(dev.subprocess, 'run', side_effect=install) as run:
            dev.execute(request)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn('--upgrade', run.call_args.args[0])
        self.assertNotIn('--skip-apt', run.call_args.args[0])
        self.assertEqual(json.loads((self.root/'result.json').read_text())['phase'], 'succeeded')

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

    def recent_runs(self, numbers):
        runs = {}
        for number in numbers:
            run = copy.deepcopy(self.run)
            run.update(id=number, run_number=number, head_sha=f'{number:040x}')
            artifact = copy.deepcopy(self.artifact)
            artifact.update(id=1000+number)
            artifact['workflow_run'].update(id=number, head_sha=run['head_sha'])
            runs[number] = {'run': run, 'artifacts': [artifact], 'jobs': copy.deepcopy(self.jobs)}
        def meta(repo, token, path):
            self.assertEqual((repo, token), ('owner/repo', 'private-token'))
            if path.startswith('actions/workflows/'):
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(path).query)
                self.assertEqual(query['branch'], ['dev']); self.assertEqual(query['status'], ['completed'])
                self.assertEqual(query['per_page'], ['20'])
                page = int(query['page'][0])
                values = [value['run'] for value in runs.values()]
                return {'workflow_runs': values[(page-1)*20:page*20]}
            parts = path.split('/')
            item = runs[int(parts[2])]
            if '/artifacts?' in path: return {'artifacts': item['artifacts']}
            if '/jobs?' in path: return {'jobs': item['jobs']}
            return item['run']
        self.metadata.side_effect = meta
        return runs

    def test_latest_orders_by_source_build_not_completion_or_rerun_time(self):
        runs = self.recent_runs([40, 42, 41])
        runs[40]['run']['updated_at'] = '2099-01-01T00:00:00Z'
        request = dev.discover()
        self.assertEqual(request['run_id'], '42')
        self.assertEqual(request['commit'], f'{42:040x}')
        self.assertEqual(request['artifact_id'], 1042)
        # Stop immediately once the newest usable build is found.
        self.assertFalse(any('runs/40/' in call.args[2] for call in self.metadata.call_args_list))

    def test_latest_skips_failed_tests_missing_expired_and_wrong_arch_artifacts(self):
        runs = self.recent_runs([46, 45, 44, 43, 42, 41])
        runs[46]['jobs'][1]['conclusion'] = 'failure'
        runs[45]['jobs'][0]['conclusion'] = 'failure'
        runs[44]['artifacts'] = []
        runs[43]['artifacts'][0]['expired'] = True
        runs[42]['artifacts'][0]['name'] = 'deploy-platform-linux-arm64'
        self.assertEqual(dev.discover()['run_id'], '41')
        self.assertFalse(any('runs/44/jobs?' in call.args[2] for call in self.metadata.call_args_list))

    def test_latest_skips_forks_prs_other_workflows_cancelled_and_in_progress(self):
        runs = self.recent_runs([47, 46, 45, 44, 43, 42, 41])
        runs[47]['run']['head_repository']['full_name'] = 'fork/repo'
        runs[46]['run']['event'] = 'pull_request'
        runs[45]['run']['path'] = '.github/workflows/other.yml'
        runs[44]['run']['conclusion'] = 'cancelled'
        runs[43]['run']['status'] = 'in_progress'
        runs[42]['run']['head_branch'] = 'main'
        self.assertEqual(dev.discover()['run_id'], '41')

    def test_explicit_run_never_lists_or_falls_back_to_latest(self):
        runs = self.recent_runs([42, 41])
        self.assertEqual(dev.discover('41')['run_id'], '41')
        runs[41]['artifacts'][0]['expired'] = True
        with self.assertRaises(dev.CandidateUnavailable): dev.discover('41')
        self.assertFalse(any('actions/workflows/' in call.args[2] for call in self.metadata.call_args_list))

    def test_latest_no_usable_build_does_not_queue_or_install(self):
        runs = self.recent_runs([42, 41])
        for item in runs.values(): item['artifacts'] = []
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(dev.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'no installable dev build'):
                cli.main(['upgrade-dev', '--yes'])
            run.assert_not_called()
        self.assertFalse((self.root/'request.json').exists())

    def test_latest_transport_auth_rate_limit_and_corrupt_metadata_abort_without_fallback(self):
        import urllib.error
        for error in (urllib.error.HTTPError('https://api.github.com', 401, 'Unauthorized', {}, None),
                      urllib.error.HTTPError('https://api.github.com', 403, 'Forbidden', {}, None),
                      urllib.error.HTTPError('https://api.github.com', 429, 'Rate limit', {}, None),
                      urllib.error.HTTPError('https://api.github.com', 500, 'Server error', {}, None),
                      OSError('network failed'), ValueError('bad metadata')):
            self.recent_runs([42, 41])
            original = self.metadata.side_effect
            def meta(repo, token, path):
                if 'runs/42/artifacts?' in path: raise error
                return original(repo, token, path)
            self.metadata.side_effect = meta
            self.metadata.reset_mock()
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)): dev.discover()
            self.assertFalse(any('runs/41/' in call.args[2] for call in self.metadata.call_args_list))

    def test_latest_deleted_candidate_can_fall_back(self):
        import urllib.error
        for code in (404, 410):
            self.recent_runs([42, 41]); original = self.metadata.side_effect
            def meta(repo, token, path):
                if 'runs/42/artifacts?' in path: raise urllib.error.HTTPError('https://api.github.com', code, 'Gone', {}, None)
                return original(repo, token, path)
            self.metadata.side_effect = meta
            self.assertEqual(dev.discover()['run_id'], '41')

    def test_latest_empty_and_malformed_listing_fail_closed(self):
        for value in ([], {'workflow_runs': []}, {'workflow_runs': None}, {'workflow_runs': [{}]},
                      {'workflow_runs': [dict(self.run, run_number=True)]},
                      {'workflow_runs': [dict(self.run, run_number=1)]*21}):
            self.metadata.side_effect = None; self.metadata.return_value = value
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError): dev.discover()

    def test_default_cli_previews_and_queues_concrete_identity(self):
        self.recent_runs([42, 41])
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(['upgrade-dev'])
        checked = json.loads(output.getvalue())
        self.assertEqual(checked['run_id'], '42'); self.assertEqual(checked['phase'], 'checked')
        self.assertFalse((self.root/'request.json').exists())
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()):
            cli.main(['upgrade-dev', '--yes'])
        queued = json.loads((self.root/'request.json').read_text())
        self.assertEqual(queued, dict(checked, phase='queued'))
        # A later run may appear; the worker must revalidate the accepted run,
        # not perform another latest search or silently switch to the new build.
        self.recent_runs([43, 42, 41]); self.metadata.reset_mock()
        with patch.object(worker, 'download', side_effect=RuntimeError('stop before download')):
            with self.assertRaises(RuntimeError): dev.execute(queued)
        self.assertTrue(any(call.args[2] == 'actions/runs/42' for call in self.metadata.call_args_list))
        self.assertFalse(any('actions/workflows/' in call.args[2] or 'runs/43' in call.args[2] for call in self.metadata.call_args_list))

    def test_invalid_explicit_run_does_not_mean_latest(self):
        for value in ('', '0', '-1', 'abc'):
            with patch.object(cli.os, 'geteuid', return_value=0), self.assertRaises(ValueError):
                cli.main(['upgrade-dev', '--run-id', value, '--yes'])
        self.metadata.assert_not_called()

    def test_latest_selects_arm_artifact_and_arm_build_job_for_arm_node(self):
        runs = self.recent_runs([42, 41])
        runs[41]['artifacts'][0]['name'] = 'deploy-platform-linux-arm64'
        runs[41]['jobs'][1]['name'] = 'build / package (ubuntu-24.04-arm, arm64)'
        worker.CURRENT.write_text(json.dumps({'version': '0.5.3', 'arch': 'arm64'}))
        with patch.object(dev.platform, 'machine', return_value='aarch64'):
            request = dev.discover()
        self.assertEqual((request['run_id'], request['arch']), ('41', 'arm64'))

    def test_latest_searches_next_page_and_stops_at_100_runs(self):
        runs = self.recent_runs(range(150, 129, -1))
        for number in range(150, 130, -1): runs[number]['artifacts'] = []
        self.assertEqual(dev.discover()['run_id'], '130')
        self.assertEqual(sum('actions/workflows/' in call.args[2] for call in self.metadata.call_args_list), 2)
        runs = self.recent_runs(range(150, 49, -1)); self.metadata.reset_mock()
        for number in range(150, 50, -1): runs[number]['artifacts'] = []
        with self.assertRaisesRegex(ValueError, 'latest 100'): dev.discover()
        self.assertEqual(sum('actions/workflows/' in call.args[2] for call in self.metadata.call_args_list), 5)
        self.assertFalse(any('runs/50/' in call.args[2] for call in self.metadata.call_args_list))
