"""Independent server protocol and offline bundle boundary tests."""
from contextlib import nullcontext
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/installer/lib'))
import ci_state as service
from delivery import DeliveryError

spec = importlib.util.spec_from_file_location('server_control', ROOT/'distribution/installer/lib/ci_control.py')
control = importlib.util.module_from_spec(spec); spec.loader.exec_module(control)


def request():
    return {'schema_version': 1, 'repository': 'owner/repo', 'run_id': '123', 'attempt': '1',
            'commit': 'a'*40, 'version': '0.5.3', 'arch': 'amd64', 'artifact_id': 1,
            'archive_sha256': 'b'*64, 'archive_size': 20, 'installer_sha256': 'c'*64,
            'installer_size': 10, 'download_seconds': 30}


def envelope():
    return {'protocol': 1, 'request': request(), 'ticket': {'url': 'https://test.blob.core.windows.net/file?SECRET', 'revision': 1}}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)/'job'
        import os
        mask = os.umask(0o022); self.addCleanup(os.umask, mask)
        patches = [patch.object(service, 'config', return_value={'protocol': 1, 'repository': 'owner/repo', 'arch': 'amd64'}),
                   patch.object(service, 'stage', return_value=self.stage),
                   patch.object(service, 'locked', return_value=nullcontext()),
                   patch.object(service, 'trusted', side_effect=lambda p, **kw: p)]
        for item in patches: item.start(); self.addCleanup(item.stop)

    def test_start_uses_installed_code_and_writes_no_executable_source(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            control.control(envelope(), 'start')
        self.assertEqual({p.name for p in self.stage.iterdir()}, {'request.json', 'download.json', 'result.json'})
        command = run.call_args.args[0]
        self.assertIn(str(control.SCRIPTS/'ci_worker.py'), command)
        self.assertEqual(command[-3:], ['123', '1', 'a'*40])
        self.assertIn('-I', command)
        self.assertIn('--no-block', command)
        self.assertEqual((self.stage/'download.json').stat().st_mode & 0o077, 0)
        with self.assertRaises(FileExistsError): control.control(envelope(), 'start')

    def test_code_protocol_repository_arch_url_and_revision_rejected_before_start(self):
        values = []
        for change in ({'files': {'ci_worker.py': 'evil'}}, {'protocol': 2}, {'ticket': {'url': 'http://bad.invalid/x', 'revision': 1}}, {'ticket': {'url': envelope()['ticket']['url'], 'revision': 0}}):
            value = envelope(); value.update(change); values.append(value)
        for key, value in [('repository', 'other/repo'), ('arch', 'arm64')]:
            item = envelope(); item['request'][key] = value; values.append(item)
        with patch.object(control.subprocess, 'run') as run:
            for value in values:
                with self.subTest(value=value), self.assertRaises((ValueError, DeliveryError)):
                    control.control(value, 'start')
            run.assert_not_called()
        self.assertFalse(self.stage.exists())

    def test_launch_failure_removes_download_authorization(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(ValueError): control.control(envelope(), 'start')
        self.assertFalse((self.stage/'download.json').exists())
        self.assertEqual(json.loads((self.stage/'result.json').read_text())['error_code'], 'unit_start_failed')

    def test_refresh_requires_unchanged_request_and_increasing_ticket(self):
        with patch.object(control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            control.control(envelope(), 'start')
        (self.stage/'result.json').write_text('{"phase":"awaiting_url"}')
        next_ticket = envelope(); next_ticket['ticket']['revision'] = 2
        control.control(next_ticket, 'refresh')
        control.control(envelope(), 'refresh')
        self.assertEqual(json.loads((self.stage/'download.json').read_text())['revision'], 2)
        next_ticket['request']['commit'] = 'd'*40
        with self.assertRaises(ValueError): control.control(next_ticket, 'refresh')

    def test_status_filters_unknown_credential_fields(self):
        self.stage.mkdir()
        (self.stage/'result.json').write_text(json.dumps({'phase': 'downloading', 'url': 'SECRET', 'ticket': 'SECRET', 'downloaded_bytes': 1}))
        with patch.object(sys, 'argv', ['cloudctl', 'status', '123', '1']), patch.object(control.os, 'geteuid', return_value=0), patch.object(sys, 'stdout', new_callable=io.StringIO) as stdout:
            control.main()
            self.assertEqual(json.loads(stdout.getvalue()), {'phase': 'downloading', 'downloaded_bytes': 1})
