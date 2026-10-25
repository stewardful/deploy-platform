"""Local automatic install authorization is owned by cloudctl, never by a Webhook."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'distribution/installer/lib'))
import install_policy as policy
import update_worker as worker
from state import write_record

class InstallPolicyTests(unittest.TestCase):
    def test_default_off_strict_config_and_durable_attempts(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(worker,'CONFIG',Path(temp)/'config'),patch.object(worker,'SPOOL',Path(temp)):
            self.assertFalse(policy.policy()[1])
            write_record(worker.CONFIG,{'repository':'owner/repo','dev_auto_upgrade':True})
            policy.guard('owner/repo')
            for phase in ('attempted','succeeded','failed'):
                policy.record('owner/repo','42',phase)
                self.assertEqual(policy.receipt('owner/repo')['phase'],phase)
            write_record(worker.CONFIG,{'repository':'owner/repo','notifications':{}})
            with self.assertRaises(ValueError):policy.policy()
            write_record(worker.CONFIG,{'repository':'owner/repo','dev_auto_upgrade':False})
            with self.assertRaises(ValueError):policy.guard('owner/repo')
