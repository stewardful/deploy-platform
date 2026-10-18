"""Release delivery boundaries, no network/system services/host mutation."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'distribution/installer/lib'))
import update_worker as worker


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spool = patch.object(worker, 'SPOOL', self.root)
        self.spool.start()
        download_root=patch.object(worker,'DOWNLOAD_ROOT',self.root)
        download_root.start()
        self.addCleanup(download_root.stop)
        self.addCleanup(self.spool.stop)
        self.request = {'version': '0.5.2', 'repository': 'owner/repo', 'asset_id': 12,
                        'size': 4, 'digest': 'sha256:' + hashlib.sha256(b'test').hexdigest()}

    def test_revalidate_exact_approved_asset(self):
        asset = {'name': 'deploy-platform-0.5.2-linux-arm64.run', 'id': 12,
                 'size': 4, 'digest': self.request['digest']}
        release = {'tag_name': 'v0.5.2', 'draft': False, 'prerelease': False, 'assets': [asset]}
        def discover():
            with patch.object(worker, 'open_github', return_value=io.BytesIO(json.dumps(release).encode())):
                return worker.discover('owner/repo', 'private', self.request, 'arm64')
        self.assertEqual(discover()['id'], 12)
        asset['id'] = 13
        with self.assertRaises(ValueError): discover()
        asset['id'] = 12
        release['assets'].append(asset)
        with self.assertRaises(ValueError): discover()

    def test_bounded_streaming_download_hash_size_and_space(self):
        asset = {'id': 12, 'size': 4, 'digest': self.request['digest']}
        for content, succeeds in [(b'test', True), (b'bad!', False), (b'tes', False), (b'test!', False)]:
            target = self.root / 'installer.run'
            target.unlink(missing_ok=True)
            with patch.object(worker, 'open_github', return_value=io.BytesIO(content)), patch.object(worker.shutil, 'disk_usage', return_value=Mock(free=4*1024**3)):
                if succeeds: worker.download('owner/repo', 'private', asset, target)
                else:
                    with self.assertRaises(ValueError): worker.download('owner/repo', 'private', asset, target)
        with patch.object(worker.shutil, 'disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(ValueError): worker.download('owner/repo', '', asset, self.root/'new')

    def test_redirect_strips_token_and_rejects_untrusted_hosts(self):
        req = urllib.request.Request('https://api.github.com/repos/a/b/releases/assets/12', headers={'Authorization': 'Bearer secret'})
        handler = worker.AssetRedirect()
        redirected = handler.redirect_request(req, None, 302, '', {}, 'https://release-assets.githubusercontent.com/asset?signature=x')
        self.assertFalse(redirected.has_header('Authorization'))
        for url in ['http://github.com/a', 'https://evil.example/a', 'https://github.com.evil.example/a', 'https://user:pass@github.com/a', 'https://github.com:444/a']:
            with self.assertRaises(ValueError): handler.redirect_request(req, None, 302, '', {}, url)

    def test_failed_request_records_safe_failure_and_never_retries(self):
        (self.root/'request.json').write_text(json.dumps(self.request))
        (self.root/'request.json').chmod(0o600)
        with patch.object(worker, 'execute', side_effect=ValueError('secret-token/signed-url')) as execute:
            with self.assertRaises(RuntimeError): worker.main()
            worker.main()
            self.assertEqual(execute.call_count, 1)
        result = json.loads((self.root/'result.json').read_text())
        self.assertEqual(result['phase'], 'failed')
        self.assertNotIn('secret-token', json.dumps(result))
        self.assertFalse((self.root/'active.json').exists())

    def test_interrupted_request_is_not_replayed(self):
        (self.root/'active.json').write_text('{}')
        with patch.object(worker, 'execute') as execute:
            with self.assertRaises(ValueError): worker.main()
            execute.assert_not_called()

    def test_existing_installer_is_only_mutation_entry(self):
        current = self.root/'manifest.json'
        current.write_text('{"version":"0.5.1","arch":"arm64"}')
        def run(argv, **kwargs):
            if '--upgrade' in argv:
                current.write_text('{"version":"0.5.2","arch":"arm64"}')
        with patch.object(worker, 'CURRENT', current), patch.object(worker, 'TRANSACTION', self.root/'transaction'), patch.object(worker.platform, 'machine', return_value='aarch64'), patch.object(worker, 'settings', return_value=('owner/repo','token')), patch.object(worker, 'discover', return_value={}), patch.object(worker, 'download'), patch.object(worker.subprocess, 'run', side_effect=run) as runner:
            worker.execute(self.request)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.args[0][2:], ['--upgrade', '--skip-apt'])
        self.assertEqual(json.loads((self.root/'result.json').read_text())['phase'], 'succeeded')

    def test_private_token_rejects_symlink_and_public_mode(self):
        token=self.root/'token'
        token.write_text('secret')
        token.chmod(0o644)
        with self.assertRaises(ValueError): worker.private_read(token, 1024)
        token.chmod(0o600)
        link=self.root/'link'
        link.symlink_to(token)
        with self.assertRaises(ValueError): worker.private_read(link, 1024)
