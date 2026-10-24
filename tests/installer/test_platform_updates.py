"""Anonymous Release discovery and real streaming downloads; shared queue/installer boundary."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/installer/lib'))
import dev_update as dev
import platform_updates as cli
import update_worker as worker
from dev_manifest import validate


def manifest(run='42'):
    data = b'verified installer'
    return {'schema_version': 1, 'repository': 'owner/repo', 'run_id': run, 'commit': 'a'*40,
            'version': '0.5.3', 'cli': {'name': 'cloudctl', 'size': 10, 'sha256': 'b'*64},
            'installers': {arch: {'name': f'deploy-platform-0.5.3-linux-{arch}.run',
                                 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for arch in ('amd64', 'arm64')}}


class PlatformUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name, value in [('SPOOL', self.root), ('DOWNLOAD_ROOT', self.root), ('CURRENT', self.root/'manifest.json'), ('TRANSACTION', self.root/'transaction')]:
            self.stack.enter_context(patch.object(worker, name, value))
        worker.CURRENT.write_text(json.dumps({'version': '0.5.3', 'arch': 'amd64'}))
        self.settings = self.stack.enter_context(patch.object(worker, 'settings', return_value=('owner/repo', '')))
        self.stack.enter_context(patch.object(dev.platform, 'machine', return_value='x86_64'))
        self.stack.enter_context(patch.object(worker, 'ACTIVE_DETAILS', {}))
        self.value = manifest()
        self.metadata = self.stack.enter_context(patch.object(dev, 'metadata', side_effect=lambda repo, tag: copy.deepcopy(self.value)))

    def test_default_pins_latest_to_fixed_release_without_reading_token(self):
        result = dev.discover()
        self.assertEqual((result['run_id'], result['commit']), ('42', 'a'*40))
        self.assertEqual([c.args[1] for c in self.metadata.call_args_list], ['dev-latest', 'dev-42'])
        self.settings.assert_called_once_with(read_token=False)
        self.assertNotIn('token', json.dumps(result))

    def test_fixed_run_does_not_consult_latest_and_invalid_runs_fail_closed(self):
        self.assertEqual(dev.discover('42')['run_id'], '42')
        self.metadata.assert_called_once_with('owner/repo', 'dev-42')
        for value in ('', '0', '-1', 'abc', '../42', True):
            with self.subTest(value=value), self.assertRaises(ValueError): dev.discover(value)
        self.value['run_id'] = '43'
        with self.assertRaises(ValueError): dev.discover('42')

    def test_latest_mismatch_or_unavailable_never_silently_falls_back(self):
        self.metadata.side_effect = [manifest(), manifest('43')]
        with self.assertRaises(ValueError): dev.discover()
        self.metadata.side_effect = OSError('unavailable')
        with self.assertRaises(OSError): dev.discover()

    def test_initial_install_and_architecture_selection(self):
        worker.CURRENT.unlink()
        with self.assertRaises(FileNotFoundError): dev.discover()
        self.assertEqual(dev.discover(initial=True)['arch'], 'amd64')
        with patch.object(dev.platform, 'machine', return_value='aarch64'):
            self.assertEqual(dev.discover(initial=True)['arch'], 'arm64')
        worker.CURRENT.write_text(json.dumps({'version': '0.5.3', 'arch': 'arm64'}))
        with self.assertRaises(ValueError): dev.discover()

    def test_schema_source_architecture_and_asset_boundaries(self):
        changes = [{'schema_version': True}, {'schema_version': 2}, {'repository': 'fork/repo'}, {'commit': 'bad'},
                   {'version': '../bad'}, {'installers': {}}, {'run_id': '0'}, {'extra': 'field'}]
        for change in changes:
            value = dict(manifest(), **change)
            with self.subTest(change=change), self.assertRaises(ValueError): validate(value, 'owner/repo')
        for change in ({'name': '../installer.run'}, {'size': True}, {'size': 0}, {'size': worker.MAX_INSTALLER+1}, {'sha256': ''}, {'url': 'https://evil.example'}):
            value = manifest(); value['installers']['amd64'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError): validate(value, 'owner/repo')

    def test_public_manifest_is_bounded_and_validated(self):
        # Call the actual function despite setUp's transport seam.
        import importlib.util
        spec = importlib.util.spec_from_file_location('public_dev_transport', ROOT/'distribution/installer/lib/dev_update.py')
        transport = importlib.util.module_from_spec(spec); spec.loader.exec_module(transport)
        for data, ok in [(json.dumps(manifest()).encode(), True), (b'x'*16385, False), (b'{}', False)]:
            with patch.object(transport, 'open_public', return_value=io.BytesIO(data)):
                if ok: self.assertEqual(transport.metadata('owner/repo', 'dev-42'), manifest())
                else:
                    with self.assertRaises(ValueError): transport.metadata('owner/repo', 'dev-42')

    def test_anonymous_transport_and_redirect_security(self):
        with patch.object(urllib.request, 'build_opener') as opener:
            dev.open_public(dev.asset_url('owner/repo', 'dev-42', 'dev.json'))
        request = opener.return_value.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))
        target = worker.AssetRedirect().redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/file')
        self.assertFalse(target.has_header('Authorization'))
        for url in ('https://evil.example/a', 'http://github.com/a', 'https://user:pass@github.com/a'):
            with self.assertRaises(ValueError): dev.open_public(url)
        for repo, tag, name in [('owner/../repo','dev-42','a'), ('owner/repo','main','a'), ('owner/repo','dev-42','../a')]:
            with self.assertRaises(ValueError): dev.asset_url(repo, tag, name)

    def test_preview_and_queue_pin_identity_clear_old_result_and_refuse_overlap(self):
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(['upgrade-dev'])
        checked = json.loads(out.getvalue())
        self.assertEqual(checked['phase'], 'checked'); self.assertFalse((self.root/'request.json').exists())
        (self.root/'result.json').write_text('{"phase":"succeeded"}')
        with patch.object(cli.os, 'geteuid', return_value=0), contextlib.redirect_stdout(io.StringIO()): cli.main(['upgrade-dev', '--yes'])
        self.assertFalse((self.root/'result.json').exists())
        self.assertEqual((self.root/'request.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): cli.queue(dev.discover('42'))
        (self.root/'request.json').unlink(); (self.root/'active.json').write_text('{}')
        with self.assertRaises(ValueError): cli.queue(dev.discover('42'))

    def candidate(self):
        return {'package_kind': 'platform-update', 'version': '0.5.3', 'arch': 'amd64', 'build': {'channel': 'dev', 'commit': 'a'*40}}

    def test_real_download_and_shared_installer_first_install_and_upgrade(self):
        for initial in (False, True):
            if initial: worker.CURRENT.unlink()
            request = dict(dev.discover('42', initial=initial), **({'operation': 'install'} if initial else {}))
            def install(argv, **kwargs):
                if '--check' not in argv: worker.CURRENT.write_text(json.dumps(self.candidate()))
            self.metadata.reset_mock()
            with patch.object(dev, 'open_public', side_effect=lambda url: io.BytesIO(b'verified installer')) as opened, patch('archive.installer_manifest', return_value=self.candidate()), patch.object(worker.subprocess, 'run', side_effect=install) as run:
                dev.execute(request)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][-1], '--check')
            self.assertEqual('--upgrade' in run.call_args.args[0], not initial)
            self.assertEqual('--skip-apt' in run.call_args.args[0], not initial)
            self.assertEqual(opened.call_args.args[0], 'https://github.com/owner/repo/releases/download/dev-42/'+request['name'])
            self.assertEqual([c.args[1] for c in self.metadata.call_args_list], ['dev-42'])
            self.assertEqual(json.loads((self.root/'result.json').read_text())['phase'], 'succeeded')

    def test_changed_approved_release_or_corrupt_download_never_executes(self):
        request = dev.discover('42'); self.value['commit'] = 'c'*40
        with patch.object(worker.subprocess, 'run') as run:
            with self.assertRaises(ValueError): dev.execute(request)
            run.assert_not_called()
        self.value = manifest()
        for data in (b'bad', b'x'*100):
            with patch.object(dev, 'open_public', return_value=io.BytesIO(data)), patch.object(worker.subprocess, 'run') as run:
                with self.assertRaises(ValueError): dev.execute(request)
                run.assert_not_called()

    def test_package_provenance_or_transaction_cannot_report_success(self):
        for fault in ('commit', 'arch', 'version', 'package_kind', 'transaction', 'install', 'no_activation'):
            candidate = self.candidate(); request = dev.discover('42')
            if fault in ('arch', 'version', 'package_kind'): candidate[fault] = 'bad'
            if fault == 'commit': candidate['build']['commit'] = 'c'*40
            def install(argv, **kwargs):
                if '--check' not in argv:
                    if fault == 'transaction': worker.TRANSACTION.write_text('{}')
                    if fault == 'install': raise subprocess.CalledProcessError(1, argv)
            with self.subTest(fault=fault), patch.object(dev, 'open_public', return_value=io.BytesIO(b'verified installer')), patch('archive.installer_manifest', return_value=candidate), patch.object(worker.subprocess, 'run', side_effect=install):
                with self.assertRaises((ValueError, subprocess.CalledProcessError)): dev.execute(request)
            worker.TRANSACTION.unlink(missing_ok=True)

    def test_failed_worker_records_no_credentials_and_never_retries(self):
        cli.queue(dev.discover('42'))
        with patch.object(dev, 'execute', side_effect=ValueError('private-token')) as execute:
            with self.assertRaises(RuntimeError): worker.main()
            worker.main(); self.assertEqual(execute.call_count, 1)
        text = (self.root/'result.json').read_text()
        self.assertNotIn('private-token', text); self.assertEqual(json.loads(text)['phase'], 'failed')

    def test_stable_uses_existing_api_and_status_survives_control_outage(self):
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'api', return_value={'enabled': True}) as api, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['update', 'check']); api.assert_called_with('/check', {})
            cli.main(['upgrade', '--version', '0.5.4', '--yes']); api.assert_called_with('/upgrade', {'version': '0.5.4'})
        (self.root/'active.json').write_text(json.dumps(dev.discover('42'))); (self.root/'active.json').chmod(0o600)
        with patch.object(cli.os, 'geteuid', return_value=0), patch.object(cli, 'api', side_effect=AssertionError('control unavailable')), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(['update', 'status'])
        self.assertEqual(json.loads(out.getvalue())['execution']['phase'], 'interrupted')
