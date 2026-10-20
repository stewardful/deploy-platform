"""Hermetic update packaging and dependency reuse; no cloud or host mutations."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/installer/lib'))
import archive
import dependencies as deps
spec = importlib.util.spec_from_file_location('update_package', ROOT/'distribution/release/package.py')
package = importlib.util.module_from_spec(spec); spec.loader.exec_module(package)


def elf(arch='amd64'):
    value = bytearray(20); value[:6] = b'\x7fELF\x02\x01'; value[18:20] = (62 if arch == 'amd64' else 183).to_bytes(2, 'little')
    return bytes(value)


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as handle:
        for name, value in files.items():
            member = tarfile.TarInfo(name); member.size = len(value); member.mode = 0o755
            handle.addfile(member, io.BytesIO(value))
    return buffer.getvalue()


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root/'source'
        self.cache = self.root/'cache'; self.current = self.root/'current'
        binaries = self.root/'bin'; binaries.mkdir()
        for name in ('agentd','agentctl','controld'): (binaries/name).write_bytes(elf())
        web = self.root/'web'; web.mkdir(); (web/'index.html').write_text('test')
        self.output = self.root/'dist'
        version = __import__('re').search(r'\[workspace.package\]\s*version = "([^"]+)"', (ROOT/'Cargo.toml').read_text()).group(1)
        args = SimpleNamespace(kind='platform-update', version=version, arch='amd64', output=self.output, bin_dir=binaries, web_dir=web, runtime_archive=None, proxy_archive=None)
        package.package(args)
        self.source.mkdir(); archive.extract(next(self.output.glob('*.tar.gz')), self.source)
        self.update_template = self.root/'template'; shutil.copytree(self.source, self.update_template)
        runtime = {'bin/'+n: elf() for n in ('containerd','containerd-shim-runc-v2','ctr','nerdctl','runc')}
        runtime.update({'libexec/cni/'+n: elf() for n in ('bridge','loopback','host-local','portmap','firewall','tuning')})
        self.data = {'runtime': tar_bytes(runtime), 'proxy': tar_bytes({'caddy': elf(), 'LICENSE': b'license'})}
        self.calls = []
        manifest = json.loads((self.source/'manifest.json').read_text())
        for kind in self.data:
            digest = hashlib.sha256(self.data[kind]).hexdigest()
            manifest[kind]['archives']['amd64'] = {'sha256': digest} if kind == 'proxy' else digest
        (self.source/'runtime.lock.json').write_text(json.dumps(manifest['runtime']))
        manifest['files']['runtime.lock.json'] = archive.digest(self.source/'runtime.lock.json')
        (self.source/'manifest.json').write_text(json.dumps(manifest))
        shutil.rmtree(self.update_template); shutil.copytree(self.source, self.update_template)

    def download(self, url, destination, expected):
        kind = 'runtime' if 'nerdctl' in url else 'proxy'
        self.calls.append(kind); destination.write_bytes(self.data[kind])

    def materialize(self, downloader=None):
        return deps.materialize(self.source, current=self.current, cache=self.cache, downloader=downloader or self.download)

    def reset(self):
        shutil.rmtree(self.source); shutil.copytree(self.update_template, self.source)

    def test_update_is_complete_platform_but_has_no_embedded_runtime_and_check_needs_no_network(self):
        value = archive.verify(self.source)
        self.assertEqual(value['package_kind'], 'platform-update')
        self.assertTrue((self.source/'bin/agentd').is_file())
        self.assertFalse((self.source/'runtime').exists()); self.assertFalse((self.source/'proxy').exists())
        result = subprocess.run(['bash', str(next(self.output.glob('*.run'))), '--check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.cache.exists())

    def test_dev_package_carries_cli_components_and_matching_build_provenance(self):
        version = json.loads((self.source/'manifest.json').read_text())['version']
        args = SimpleNamespace(kind='platform-update', version=version, arch='amd64', output=self.output, bin_dir=self.root/'bin', web_dir=self.root/'web', runtime_archive=None, proxy_archive=None)
        with patch.dict(__import__('os').environ, {'GITHUB_SHA': 'a'*40, 'GITHUB_REF': 'refs/heads/dev'}):
            package.package(args)
        runner = next(self.output.glob('*.run'))
        manifest = archive.installer_manifest(runner)
        self.assertEqual(manifest['build'], {'channel': 'dev', 'commit': 'a'*40})
        for name in ('node_cli.py', 'platform_updates.py', 'dev_update.py', 'delivery.py'):
            self.assertIn('lib/'+name, manifest['files'])
        invalid = self.root/'invalid.run'; invalid.write_bytes(b'not a payload')
        with self.assertRaises(ValueError): archive.installer_manifest(invalid)

    def test_first_use_downloads_and_second_use_offline_reuses_cache(self):
        value = self.materialize(); self.assertEqual(value['package_kind'], 'offline')
        self.assertEqual(self.calls, ['runtime','proxy'])
        original = (self.source/'manifest.json').read_bytes()
        self.reset()
        self.materialize(lambda *a: self.fail('unchanged dependency downloaded again'))
        self.assertEqual((self.source/'manifest.json').read_bytes(), original)
        self.assertEqual(archive.verify(self.source)['package_kind'], 'offline')

    def test_existing_full_release_seeds_cache_without_download(self):
        self.materialize(); shutil.copytree(self.source, self.current)
        shutil.rmtree(self.cache); self.reset()
        self.materialize(lambda *a: self.fail('installed dependency downloaded again'))
        self.assertEqual(archive.verify(self.source)['files'], archive.verify(self.current)['files'])

    def test_corrupt_cache_is_not_used_and_is_rebuilt_from_verified_installation(self):
        self.materialize(); shutil.copytree(self.source, self.current)
        entry = next(self.cache.glob('runtime-*')); (entry/'payload/bin/nerdctl').write_bytes(b'tampered')
        self.reset(); self.materialize(lambda *a: self.fail('valid installed dependency ignored'))
        self.assertEqual((self.source/'runtime/bin/nerdctl').read_bytes(), elf())

    def test_corrupt_installed_release_cannot_seed_cache(self):
        self.materialize(); shutil.copytree(self.source, self.current)
        (self.current/'runtime/bin/nerdctl').write_bytes(b'tampered')
        shutil.rmtree(self.cache); self.reset(); self.calls.clear()
        self.materialize(); self.assertEqual(self.calls, ['runtime','proxy'])

    def test_dependency_checksum_failure_leaves_installed_release_unchanged(self):
        self.materialize(); shutil.copytree(self.source, self.current)
        before = (self.current/'manifest.json').read_bytes()
        shutil.rmtree(self.cache); self.reset()
        # A different pinned version cannot reuse the current release.
        value = json.loads((self.source/'manifest.json').read_text()); value['runtime']['nerdctl'] = '99.0.0'
        (self.source/'runtime.lock.json').write_text(json.dumps(value['runtime']))
        value['files']['runtime.lock.json'] = archive.digest(self.source/'runtime.lock.json')
        (self.source/'manifest.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.materialize(lambda url, path, digest: path.write_bytes(b'corrupt'))
        self.assertEqual((self.current/'manifest.json').read_bytes(), before)
        archive.verify(self.current)

    def test_changed_proxy_only_fetches_proxy(self):
        self.materialize(); self.reset(); self.calls.clear()
        value = json.loads((self.source/'manifest.json').read_text()); value['proxy']['version'] = '99.0.0'
        (self.source/'manifest.json').write_text(json.dumps(value))
        self.materialize(); self.assertEqual(self.calls, ['proxy'])

    def test_linked_and_public_cache_are_rejected(self):
        real = self.root/'real'; real.mkdir(); self.cache.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'linked'): self.materialize()
        self.cache.unlink(); self.cache.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'private'): self.materialize()

    def test_cache_file_links_are_not_followed(self):
        self.materialize(); self.reset()
        p = next(self.cache.glob('runtime-*'))/'payload/bin/nerdctl'; p.unlink(); p.symlink_to(self.root/'outside')
        self.calls.clear(); self.materialize(); self.assertEqual(self.calls, ['runtime'])
        self.assertFalse((self.root/'outside').exists())

    def test_unknown_kind_invalid_dependency_digest_and_mixed_payload_are_rejected(self):
        for mutation in ('kind','digest','embedded','lock'):
            self.reset(); value = json.loads((self.source/'manifest.json').read_text())
            if mutation == 'kind': value['package_kind'] = 'something'
            if mutation == 'digest': value['proxy']['archives']['amd64']['sha256'] = 'bad'
            if mutation == 'embedded':
                (self.source/'runtime').mkdir(); (self.source/'runtime/file').write_bytes(b'bad')
                value['files']['runtime/file'] = archive.digest(self.source/'runtime/file')
            if mutation == 'lock': value['runtime']['nerdctl'] = 'different'
            (self.source/'manifest.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError): archive.verify(self.source)

    def test_offline_manifest_remains_supported_without_cache_or_network(self):
        self.materialize(); shutil.rmtree(self.cache)
        value = self.materialize(lambda *a: self.fail('offline bundle used network'))
        self.assertEqual(value['package_kind'], 'offline'); self.assertFalse(self.cache.exists())

    def test_dependency_urls_reject_http_credentials_private_hosts_and_redirects(self):
        for url in ('http://github.com/a', 'https://github.com@evil.com/a', 'https://127.0.0.1/a', 'https://github.com:444/a', 'https://github.com/a#fragment', 'https://github.com/a\n'):
            with self.assertRaises(ValueError): deps.check_url(url)
        with self.assertRaises(ValueError): deps.Redirect().redirect_request(None, None, 302, '', {}, 'http://localhost/a')

    def test_fetch_checks_sha_and_size_and_removes_partial_files(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '7'}
        client = SimpleNamespace(open=lambda *a, **k: Response(b'archive'))
        destination = self.root/'archive.tar.gz'
        with patch.object(deps.urllib.request, 'build_opener', return_value=client):
            deps.fetch('https://github.com/owner/repo/file', destination, hashlib.sha256(b'archive').hexdigest())
            self.assertEqual(destination.read_bytes(), b'archive')
            destination.unlink()
            with self.assertRaisesRegex(ValueError, 'checksum'):
                deps.fetch('https://github.com/owner/repo/file', destination, '0'*64)
            self.assertFalse(destination.exists()); self.assertFalse(destination.with_suffix('.part').exists())
            with patch.object(deps, 'MAX_DOWNLOAD', 2), self.assertRaisesRegex(ValueError, 'limit'):
                deps.fetch('https://github.com/owner/repo/file', destination, '0'*64)

    def test_fetch_network_failures_are_bounded_and_cleaned(self):
        client = SimpleNamespace(open=__import__('unittest.mock', fromlist=['Mock']).Mock(side_effect=OSError('offline')))
        destination = self.root/'archive.tar.gz'
        with patch.object(deps.urllib.request, 'build_opener', return_value=client), patch.object(deps.time, 'sleep'):
            with self.assertRaises(OSError): deps.fetch('https://github.com/owner/repo/file', destination, '0'*64)
        self.assertEqual(client.open.call_count, 3)
        self.assertFalse(destination.exists()); self.assertFalse(destination.with_suffix('.part').exists())

    def test_current_release_requires_explicit_package_kind_and_dependency_locks(self):
        self.materialize()
        manifest = json.loads((self.source/'manifest.json').read_text())
        for key in ('package_kind', 'runtime', 'proxy', 'compatibility'):
            value = dict(manifest); value.pop(key)
            (self.source/'manifest.json').write_text(json.dumps(value))
            with self.subTest(key=key), self.assertRaises(ValueError):
                archive.verify(self.source)
        (self.source/'manifest.json').write_text(json.dumps(manifest))
        archive.verify(self.source)
