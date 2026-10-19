"""Pinned private-source snapshots are verified without credentials or network."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('vendor_delivery', ROOT/'distribution/ci/vendor-delivery.py')
vendor = importlib.util.module_from_spec(spec); spec.loader.exec_module(vendor)


class VendorTests(unittest.TestCase):
    def test_committed_snapshot_matches_pinned_upstream_inventory(self):
        vendor.check()

    def test_changed_missing_extra_and_linked_files_are_rejected(self):
        import hashlib
        for mode in ('changed','missing','extra','linked'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); dest = root/'snapshot'; dest.mkdir(); lock = root/'lock.json'
                source = dest/'file'; source.write_bytes(b'approved')
                lock.write_text(json.dumps({'repository': 'stewardful/deploy-delivery', 'commit': 'a'*40, 'files': {'file': hashlib.sha256(b'approved').hexdigest()}}))
                if mode == 'changed': source.write_bytes(b'changed')
                if mode == 'missing': source.unlink()
                if mode == 'extra': (dest/'extra').write_text('unreviewed')
                if mode == 'linked': source.unlink(); source.symlink_to(lock)
                with patch.object(vendor, 'DEST', dest), patch.object(vendor, 'LOCK', lock), self.assertRaises(ValueError):
                    vendor.check()

    def test_sync_requires_complete_sha_before_running_git(self):
        with patch.object(vendor.subprocess, 'check_output') as git, self.assertRaises(ValueError):
            vendor.sync('/unused', 'main')
        git.assert_not_called()
