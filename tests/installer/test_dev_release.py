"""Publisher transaction boundaries: no server, fixed assets and monotonic latest pointer."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/ci'))
spec = importlib.util.spec_from_file_location('dev_release', ROOT/'distribution/ci/dev-release.py')
release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)
SHA = 'a'*40


class DevReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory/'cloudctl').write_bytes(b'cli')
        (self.directory/'cloudctl.sha256').write_text(hashlib.sha256(b'cli').hexdigest()+'  cloudctl\n')
        for arch in ('amd64', 'arm64'):
            name = f'deploy-platform-0.5.3-linux-{arch}'
            (self.directory/(name+'.run')).write_bytes(arch.encode())
            (self.directory/(name+'.sha256')).write_text(hashlib.sha256(arch.encode()).hexdigest()+'  '+name+'.run\n')
        def installer(path):
            return {'package_kind':'platform-update', 'version':'0.5.3', 'arch':'arm64' if 'arm64' in path.name else 'amd64', 'build':{'channel':'dev','commit':SHA}}
        self.manifest = patch.object(release,'installer_manifest',side_effect=installer)
        self.manifest.start(); self.addCleanup(self.manifest.stop)

    def published(self):
        value, paths = release.inventory('owner/repo','42',SHA,'0.5.3',self.directory)
        (self.directory/'dev.json').write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')
        return {'draft':False,'prerelease':True,'assets':[dict(release.asset(p), digest='sha256:'+release.asset(p)['sha256']) for p in paths+[self.directory/'dev.json']]}

    def invoke(self): release.publish('owner/repo','42',SHA,'0.5.3',self.directory)

    def test_complete_inventory_both_architectures_and_checksums_required(self):
        value, paths = release.inventory('owner/repo','42',SHA,'0.5.3',self.directory)
        self.assertEqual(set(value['installers']), {'amd64','arm64'}); self.assertEqual(len(paths),6)
        (self.directory/'cloudctl.sha256').write_text('bad')
        with self.assertRaises(ValueError): self.invoke()

    def test_missing_architecture_or_wrong_provenance_never_creates_tag(self):
        with patch.object(release,'api') as api:
            self.manifest.stop()
            with patch.object(release,'installer_manifest',return_value={'version':'0.5.3','arch':'amd64','build':{'channel':'dev','commit':'b'*40}}):
                with self.assertRaises(ValueError): self.invoke()
            self.manifest.start()
            (self.directory/'deploy-platform-0.5.3-linux-arm64.run').unlink()
            with self.assertRaises((ValueError, OSError)): self.invoke()
            api.assert_not_called()

    def test_publishes_fixed_draft_before_latest_and_never_calls_server(self):
        def api(repo,path,*args,**kwargs):
            if path == 'git/ref/heads/dev': return {'object':{'sha':SHA}}
            return None
        with patch.object(release,'api',side_effect=api),patch.object(release,'tag_commit',side_effect=[None,SHA,None]),patch.object(release,'gh') as gh:
            self.invoke()
        calls = [c.args for c in gh.call_args_list]
        self.assertIn('--draft',calls[0]); self.assertIn('--prerelease',calls[0])
        self.assertEqual(calls[2][:3],('release','edit','dev-42'))
        self.assertEqual(calls[-2][:3],('release','upload','dev-latest'))
        self.assertIn(str(self.directory/'dev.json'),calls[-2])
        self.assertEqual(calls[-1][:3],('release','edit','dev-latest'))
        self.assertNotIn('ssh',json.dumps(calls)); self.assertNotIn('--latest',calls[-1])

    def test_rerun_never_overwrites_fixed_published_assets(self):
        published = self.published()
        def api(repo,path,*args,**kwargs):
            if path == 'git/ref/heads/dev': return {'object':{'sha':'b'*40}}
            return published
        with patch.object(release,'api',side_effect=api),patch.object(release,'tag_commit',return_value=SHA),patch.object(release,'gh') as gh:
            self.invoke(); gh.assert_not_called()
        published['assets'][0]['digest'] = 'sha256:'+'f'*64
        with patch.object(release,'api',return_value=published),patch.object(release,'tag_commit',return_value=SHA),patch.object(release,'gh') as gh:
            with self.assertRaises(ValueError): self.invoke()
            gh.assert_not_called()

    def test_newer_promoted_run_cannot_be_replaced_by_older_rerun(self):
        published = self.published()
        def api(repo,path,*args,**kwargs):
            if path == 'git/ref/heads/dev': return {'object':{'sha':SHA}}
            if path == 'releases/tags/dev-latest': return {'name':'Dev latest (43)'}
            return published
        with patch.object(release,'api',side_effect=api),patch.object(release,'tag_commit',return_value=SHA),patch.object(release,'gh') as gh:
            self.invoke(); gh.assert_not_called()

    def test_failed_fixed_asset_upload_never_advances_latest(self):
        def fail_upload(*args):
            if args[:2] == ('release','upload'): raise RuntimeError('upload failed')
        with patch.object(release,'api',return_value=None) as api,patch.object(release,'tag_commit',side_effect=[None,SHA]),patch.object(release,'gh',side_effect=fail_upload):
            with self.assertRaises(RuntimeError): self.invoke()
        self.assertFalse(any('dev-latest' in str(c) for c in api.call_args_list))

    def test_conflicting_tag_never_publishes(self):
        with patch.object(release,'tag_commit',return_value='b'*40),patch.object(release,'gh') as gh:
            with self.assertRaises(ValueError): self.invoke()
            gh.assert_not_called()

