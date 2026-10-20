"""Pure tests: temp directories only; never invoke apt/systemd or change the host."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


archive = module("release_archive", "distribution/installer/lib/archive.py")
network = module("release_network", "distribution/installer/lib/network.py")
configure = module("release_configure", "distribution/installer/lib/configure.py")
state = module("release_state", "distribution/installer/lib/state.py")
package = module("release_package", "distribution/release/package.py")


class ArchiveTests(unittest.TestCase):
    def test_rejects_traversal_links_duplicates_and_special_files(self):
        for name, kind in [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("device", tarfile.CHRTYPE), ("duplicate", tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "bad.tar.gz"
                with tarfile.open(source, "w:gz") as handle:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    handle.addfile(member, io.BytesIO())
                    if name == "duplicate": handle.addfile(member, io.BytesIO())
                target = root / "extract"
                target.mkdir()
                with self.assertRaises(ValueError): archive.extract(source, target)
                self.assertFalse((root / "escape").exists())

    def test_checksum_inventory_and_architecture_are_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            required = ["bin/agentd", "bin/agentctl", "bin/controld", "web/index.html"]
            required += ["runtime/bin/" + n for n in ("containerd", "containerd-shim-runc-v2", "ctr", "nerdctl", "runc")]
            required += ["runtime/libexec/cni/" + n for n in ("bridge", "loopback", "host-local", "portmap", "firewall", "tuning")]
            required += ['proxy/bin/caddy', 'proxy/LICENSE', 'proxy/proxy.lock.json', 'systemd/deploy-proxy.service',
                         'lib/upgrade.py', 'lib/upgrade_state.py', 'lib/upgrade_probes.py', 'lib/agent_http.py',
                         'lib/update_worker.py', 'systemd/deploy-update.service', 'systemd/deploy-update.path']
            for name in required:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            value = {"schema_version": 1, "version": "0.5.3", "arch": "arm64", "package_kind": "offline", "runtime": {}, "proxy": {}, "compatibility": json.loads((ROOT/'distribution/release/compatibility.json').read_text()), "files": {n: archive.digest(root / n) for n in required}}
            (root / "manifest.json").write_text(json.dumps(value))
            self.assertEqual(archive.verify(root, "arm64")["version"], "0.5.3")
            with self.assertRaises(ValueError): archive.verify(root, "amd64")
            (root / "bin/agentctl").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"): archive.verify(root)
            (root / "bin/agentctl").write_bytes(b"fixture")
            (root / "web/manifest.json").write_text("unlisted")
            with self.assertRaisesRegex(ValueError, "inventory"): archive.verify(root)

    def test_rejects_darwin_and_mismatched_elf(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "binary"
            p.write_bytes(b"MachO-not-a-linux-binary")
            with self.assertRaises(ValueError): package.check_elf(p, "amd64")
            header = bytearray(20)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (183).to_bytes(2, "little")
            p.write_bytes(header)
            package.check_elf(p, "arm64")
            with self.assertRaises(ValueError): package.check_elf(p, "amd64")


class ConfigurationTests(unittest.TestCase):
    def test_fullstack_help_does_not_modify_immutable_release_inventory(self):
        # Importing smoke/generate used to create unlisted .pyc files and break
        # the next same-release installation even after a successful self-test.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('fullstack_smoke.py', 'smoke.py'):
                shutil.copyfile(ROOT / 'distribution/installer' / name, root / name)
            examples = root / 'examples/fullstack'
            examples.mkdir(parents=True)
            shutil.copyfile(ROOT / 'examples/fullstack/generate.py', examples / 'generate.py')
            before = {p.relative_to(root) for p in root.rglob('*') if p.is_file()}
            env = {k: v for k, v in os.environ.items()
                   if k not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
            subprocess.run(['python3', str(root / 'fullstack_smoke.py'), '--help'],
                           check=True, stdout=subprocess.DEVNULL, env=env)
            self.assertEqual(before, {p.relative_to(root) for p in root.rglob('*') if p.is_file()})

    def test_routes_select_nonoverlapping_private_bridge(self):
        self.assertEqual(network.choose([]), "10.89.0.0/24")
        self.assertEqual(network.choose([{"dst": "10.0.0.0/8"}]), "172.30.240.0/24")
        self.assertEqual(network.choose([{"dst": "10.0.0.0/8"}, {"dst": "172.16.0.0/12"}]), "192.168.248.0/24")
        self.assertEqual(network.choose([{"dst": "10.89.0.0/24", "dev": "cloud0"}]), "10.89.0.0/24")
        for cidr in ("8.8.8.0/24", "127.0.0.0/24", "10.1.1.1/24", "10.0.0.0/8", "0.0.0.0/24", "240.0.0.0/24", "192.0.2.0/24"):
            with self.assertRaises(ValueError): network.choose([], cidr)
        with self.assertRaises(ValueError): network.choose([{"dst": "10.0.0.0/8"}], "10.1.2.0/24")

    def test_config_uses_private_runtime_and_dedicated_cni(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            configure.generate(root, "10.89.0.0/24")
            config = (root / "containerd.toml").read_text()
            self.assertIn('version = 3', config)
            self.assertIn('/run/deploy-platform-containerd/containerd.sock', config)
            self.assertNotIn('/run/containerd/containerd.sock', config)
            cni = json.loads((root / "10-cloud.conflist").read_text())
            self.assertEqual(cni['plugins'][0]['bridge'], 'cloud0')
            self.assertEqual(cni['plugins'][0]['ipam']['ranges'][0][0]['gateway'], '10.89.0.1')
            self.assertIn('cgroup_manager = "systemd"', (root / "nerdctl.toml").read_text())
            self.assertIn('umask 022', (root / 'nerdctl').read_text())
            unit = (ROOT / 'distribution/installer/systemd/deploy-containerd.service').read_text()
            self.assertIn('UMask=0022', unit)

    def test_owned_file_snapshots_restore_without_touching_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_files, old_receipt = state.FILES, state.RECEIPT
            try:
                a, b, receipt = root / "config", root / "new", root / "receipt"
                state.FILES, state.RECEIPT = [str(a), str(b)], str(receipt)
                a.write_text('before')
                receipt.write_text(json.dumps({"owner": "deploy-platform", "schema_version": 1, "managed_files": {str(a): state.digest(a)}}))
                state.assert_owned()
                backup = root / "backup"
                backup.mkdir()
                state.snapshot(backup)
                a.write_text('modified')
                b.write_text('new config')
                with self.assertRaises(ValueError): state.assert_owned()
                state.restore(backup)
                self.assertEqual(a.read_text(), 'before')
                self.assertFalse(b.exists())
                state.assert_owned()
            finally:
                state.FILES, state.RECEIPT = old_files, old_receipt


class CommandTests(unittest.TestCase):
    def test_installed_wrapper_uses_immutable_release_cli_without_environment_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(ROOT/'distribution/installer/lib', root/'lib', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT/'distribution/installer/fullstack_smoke.py', root/'fullstack_smoke.py')
            shutil.copyfile(ROOT/'distribution/installer/smoke.py', root/'smoke.py')
            shutil.copytree(ROOT/'examples/fullstack', root/'examples/fullstack')
            wrapper = root/'cloudctl'
            source = (ROOT/'distribution/installer/cloudctl').read_text()
            wrapper.write_text(source.replace('ROOT=/opt/deploy-platform/current', 'ROOT='+shlex.quote(str(root))))
            result = subprocess.run(['bash', str(wrapper), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Applications', result.stdout)
            self.assertIn('upgrade-dev', result.stdout)
            result = subprocess.run(['bash', str(wrapper), 'test', 'fullstack', '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--image', result.stdout)
            result = subprocess.run(['bash', str(wrapper), 'version', 'ignored'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


class TransactionTests(unittest.TestCase):
    def test_begin_records_previous_services_and_commit_metadata_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'transaction.json'
            with patch.object(state, 'TRANSACTION', str(journal)), patch.object(state.subprocess, 'run') as run:
                run.return_value.returncode = 0
                state.begin('/var/lib/deploy-platform/backups/test', '10.89.0.0/24')
                value = json.loads(journal.read_text())
                self.assertEqual(value['active'], list(state.UNITS))
                self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_recovery_rejects_invalid_backup_before_system_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Path(temp) / 'transaction.json'
            journal.write_text(json.dumps({'backup': '/tmp/not-owned', 'previous': None, 'active': []}))
            with patch.object(state, 'TRANSACTION', str(journal)), patch.object(state.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'invalid transaction backup'):
                    state.recover(temp)
                run.assert_not_called()
                self.assertTrue(journal.exists())


if __name__ == "__main__": unittest.main()
