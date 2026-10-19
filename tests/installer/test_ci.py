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
