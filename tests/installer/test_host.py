"""Shared host policy executes as shell; no system changes or apt calls."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'distribution/installer/lib/os.sh'


class HostPolicyTests(unittest.TestCase):
    def test_only_verified_ubuntu_lts_versions_are_accepted(self):
        for distro, version, accepted in [
            ('ubuntu', '24.04', True), ('ubuntu', '26.04', True),
            ('ubuntu', '22.04', False), ('ubuntu', '25.10', False),
            ('ubuntu', '28.04', False), ('debian', '26.04', False),
            ('ubuntu', '', False), ('ubuntu', '26.04;exit 0', False),
        ]:
            with self.subTest(distro=distro, version=version):
                result = subprocess.run(
                    ['bash', '-c', 'source "$1"; supported_os "$2" "$3"',
                     'host-policy', str(POLICY), distro, version],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode == 0, accepted)
