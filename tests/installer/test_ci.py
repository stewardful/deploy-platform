"""CI delivery policy, pinned releases and root wrapper failure reporting."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/ci'))
import version as ci_version
import release as ci_release
spec = importlib.util.spec_from_file_location('ci_remote',ROOT/'distribution/ci/remote-install.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
SHA = 'a'*40


class CIPolicyTests(unittest.TestCase):
    def test_closed_architecture_matrix_and_stable_source_version(self):
        version, targets = ci_version.build_metadata(ROOT, 'arm64')
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertEqual(targets, [{'arch':'arm64','runner':'ubuntu-24.04-arm'}])
        with self.assertRaises(ValueError): ci_version.build_metadata(ROOT, '$(id)')

    def test_existing_tag_must_match_main_event_commit(self):
        with patch.object(ci_release,'tag_commit',return_value='b'*40):
            with self.assertRaises(ci_release.ReleaseError): ci_release.check('owner/repo','1.0.0',SHA)

    def test_publish_pins_tag_to_sha_and_only_then_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            dist=Path(directory)
            for arch in ('amd64','arm64'):
                for suffix in ('.run','.tar.gz','.sha256'):
                    (dist/f'deploy-platform-1.0.0-linux-{arch}{suffix}').touch()
            with patch.object(ci_release,'tag_commit',side_effect=[None,None,SHA]), patch.object(ci_release,'api',return_value=None) as api, patch.object(ci_release.subprocess,'run') as run:
                ci_release.publish('owner/repo','1.0.0',SHA,dist)
            self.assertEqual(api.call_args_list[0].args,('owner/repo','git/refs',{'ref':'refs/tags/v1.0.0','sha':SHA}))
            args=run.call_args_list[0].args[0]
            self.assertEqual(args[args.index('--target')+1],SHA)
            self.assertIn('--draft',args)
            self.assertEqual(run.call_args_list[-1].args[0][-2:],['--draft=false','--latest'])

    def test_same_commit_rerun_never_overwrites_published_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            dist=Path(directory);assets=[]
            for arch in ('amd64','arm64'):
                for suffix in ('.run','.tar.gz','.sha256'):
                    name=f'deploy-platform-1.0.0-linux-{arch}{suffix}'
                    (dist/name).touch();assets.append({'name':name})
            with patch.object(ci_release,'tag_commit',return_value=SHA),patch.object(ci_release,'api',return_value={'draft':False,'assets':assets}),patch.object(ci_release.subprocess,'run') as run:
                ci_release.publish('owner/repo','1.0.0',SHA,dist)
            run.assert_not_called()

    def test_missing_architecture_never_creates_release(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ci_release,'tag_commit',return_value=None),patch.object(ci_release,'api') as api:
            with self.assertRaises(ci_release.ReleaseError):ci_release.publish('owner/repo','1.0.0',SHA,Path(directory))
            api.assert_not_called()

    def test_api_404_can_mean_missing_but_permissions_errors_cannot(self):
        for error,missing in [('gh: Not Found (HTTP 404)',True),('gh: Forbidden (HTTP 403)',False)]:
            with patch.object(ci_release.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'',error)):
                if missing:self.assertIsNone(ci_release.api('owner/repo','releases/tags/v1',missing=True))
                else:
                    with self.assertRaises(ci_release.ReleaseError):ci_release.api('owner/repo','releases/tags/v1',missing=True)


class RemoteWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.stage=Path(self.temp.name)
        (self.stage/'installer.run').write_bytes(b'installer bytes')
        digest=hashlib.sha256(b'installer bytes').hexdigest()
        (self.stage/'SHA256SUMS').write_text(digest+'  installer.run\n')

    def test_success_uses_existing_installer_and_records_commit(self):
        with patch.object(remote.subprocess,'run') as run:
            remote.install(self.stage,SHA)
        self.assertIn('--check',run.call_args_list[0].args[0])
        self.assertIn('--upgrade',run.call_args_list[1].args[0])
        result=json.loads((self.stage/'result.json').read_text())
        self.assertEqual((result['phase'],result['commit']),('succeeded',SHA))
        self.assertFalse((self.stage/'installer.run').exists())
        self.assertEqual((self.stage/'result.json').stat().st_mode&0o077,0)

    def test_failed_upgrade_records_failure_and_keeps_recovery_package(self):
        with patch.object(remote.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['installer'])):
            with self.assertRaises(subprocess.CalledProcessError):remote.install(self.stage,SHA)
        self.assertEqual(json.loads((self.stage/'result.json').read_text())['phase'],'failed')
        self.assertTrue((self.stage/'installer.run').exists())

    def test_corrupt_upload_never_executes_and_cannot_escape_checksum_path(self):
        for text in ['0'*64+'  installer.run\n','0'*64+'  ../../other\n']:
            (self.stage/'SHA256SUMS').write_text(text)
            with patch.object(remote.subprocess,'run') as run:
                with self.assertRaises(ValueError):remote.install(self.stage,SHA)
                run.assert_not_called()

    def test_linked_installer_is_rejected(self):
        (self.stage/'installer.run').unlink()
        (self.stage/'installer.run').symlink_to(self.stage/'SHA256SUMS')
        with self.assertRaises(ValueError):remote.verify(self.stage)


class SSHTransportTests(unittest.TestCase):
    def test_invalid_host_is_rejected_before_any_ssh_or_key_execution(self):
        env=dict(os.environ,TEST_SSH_PRIVATE_KEY='test-only',TEST_SSH_KNOWN_HOSTS='test-only',TEST_SERVER_HOST='example.com;touch /tmp/unsafe',TEST_SERVER_USER='ubuntu',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=SHA,RELEASE_VERSION='0.5.2')
        result=subprocess.run(['bash',str(ROOT/'distribution/ci/deploy-test.sh')],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('invalid TEST_SERVER_HOST',result.stderr)
        self.assertNotIn('test-only',result.stdout+result.stderr)

    def test_transport_pins_host_key_and_only_polls_after_independent_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bindir=root/'bin';bindir.mkdir()
            trace=root/'trace.jsonl';start=root/'start.sh'
            stub='''#!/usr/bin/env python3
import json,os,sys
assert 'TEST_SSH_PRIVATE_KEY' not in os.environ
with open(os.environ['CI_TEST_TRACE'],'a') as f:f.write(json.dumps([os.path.basename(sys.argv[0]),*sys.argv[1:]])+'\\n')
if os.path.basename(sys.argv[0])=='ssh':
    if 'cat' in sys.argv:print('{"phase":"succeeded"}')
    elif 'bash' in sys.argv:
        script=sys.stdin.read()
        if 'systemd-run' in script:open(os.environ['CI_TEST_START'],'w').write(script)
'''
            for name in ('ssh','scp','ssh-keygen'):
                (bindir/name).write_text(stub);(bindir/name).chmod(0o755)
            stem='deploy-platform-0.5.2-linux-amd64'
            (root/(stem+'.run')).write_bytes(b'test installer')
            (root/(stem+'.sha256')).write_text(hashlib.sha256(b'test installer').hexdigest()+'  '+stem+'.run\n')
            env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ['PATH'],CI_TEST_TRACE=str(trace),CI_TEST_START=str(start),CI_ARTIFACT_DIR=str(root),TEST_SSH_PRIVATE_KEY='test-only',TEST_SSH_KNOWN_HOSTS='test-only',TEST_SERVER_HOST='127.0.0.1',TEST_SERVER_USER='ubuntu',TEST_SERVER_ARCH='amd64',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',GITHUB_SHA=SHA,RELEASE_VERSION='0.5.2')
            result=subprocess.run(['bash',str(ROOT/'distribution/ci/deploy-test.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--no-block',start.read_text())
            commands=[json.loads(line) for line in trace.read_text().splitlines()]
            for args in commands:
                if args[0] in ('ssh','scp'):
                    self.assertIn('StrictHostKeyChecking=yes',args)
                    self.assertIn('IdentitiesOnly=yes',args)
            self.assertNotIn('test-only',trace.read_text()+result.stdout+result.stderr)
