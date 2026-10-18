#!/usr/bin/env python3
"""Test-only GitHub HTTPS endpoint setup inside the marked disposable node.

No production URL override: actual Go and worker HTTPS requests are routed by
isolated node DNS/NAT to this endpoint, with a CA trusted only in that node.
"""
import hashlib
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.request

REPO = 'stewardful/deploy-platform'


def setup(request):
    if not Path('/run/cloud-upgrade-isolated-test').exists():
        raise RuntimeError('isolated node required')
    # Read version from the selected installer archive without changing services.
    source = Path('/var/tmp/new.run')
    with source.open('rb') as handle:
        while handle.readline().strip() != b'__CLOUD_ARCHIVE__':
            if handle.tell() >= source.stat().st_size:
                raise RuntimeError('installer archive marker missing')
        import tarfile
        with tarfile.open(fileobj=handle, mode='r|gz') as archive:
            for member in archive:
                if member.name == 'manifest.json':
                    manifest = json.load(archive.extractfile(member))
                    break
            else: raise RuntimeError('manifest missing')
    with source.open('rb') as handle:
        sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    asset = {'id': 123, 'name': 'deploy-platform-' + manifest['version'] + '-linux-' + manifest['arch'] + '.run',
             'size': source.stat().st_size, 'digest': 'sha256:' + sha}
    release = {'tag_name': 'v' + manifest['version'], 'draft': False, 'prerelease': False, 'assets': [asset]}
    Path('/var/tmp/test-release.json').write_text(json.dumps(release))
    subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '1',
                    '-subj', '/CN=api.github.com', '-addext', 'subjectAltName=DNS:api.github.com',
                    '-keyout', '/var/tmp/test-github.key', '-out', '/usr/local/share/ca-certificates/cloud-test-github.crt'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['update-ca-certificates'], check=True, stdout=subprocess.DEVNULL)
    with Path('/etc/hosts').open('a') as handle: handle.write('\n127.0.0.2 api.github.com\n')
    subprocess.run(['iptables', '-t', 'nat', '-A', 'OUTPUT', '-d', '127.0.0.2', '-p', 'tcp', '--dport', '443', '-j', 'REDIRECT', '--to-ports', '14443'], check=True)
    token = Path('/etc/deploy-platform/github.token')
    token.write_text('isolated-release-token'); token.chmod(0o600)
    cfg = Path('/etc/deploy-platform/updates.json')
    cfg.write_text(json.dumps({'repository': REPO, 'token_file': str(token)})); cfg.chmod(0o600)
    subprocess.run(['systemd-run', '--unit', 'cloud-test-github', '/usr/bin/python3', '-B', '/var/tmp/update-delivery.py', 'serve'], check=True)
    for _ in range(60):
        try:
            req = urllib.request.Request('https://api.github.com/repos/' + REPO + '/releases/latest', headers={'Authorization': 'Bearer isolated-release-token'})
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200: break
        except OSError: time.sleep(0.5)
    else: raise RuntimeError('isolated GitHub endpoint unavailable')
    subprocess.run(['systemctl', 'restart', 'deploy-control'], check=True)
    time.sleep(1)
    status = request('/api/v1/updates/check', {})
    if not status.get('candidate') or status['candidate']['version'] != manifest['version']:
        raise RuntimeError('real Go update check failed: ' + str(status))
    old_pid = subprocess.check_output(['systemctl', 'show', 'deploy-control', '-p', 'MainPID', '--value'])
    corrupt = Path('/run/cloud-test-corrupt-download')
    corrupt.touch()
    request('/api/v1/updates/upgrade', {'version': manifest['version']})
    result = Path('/var/lib/deploy-platform/updates/result.json')
    for _ in range(90):
        if result.exists() and json.loads(result.read_text()).get('phase') == 'failed': break
        time.sleep(1)
    else: raise RuntimeError('corrupted package was not rejected')
    if old_pid != subprocess.check_output(['systemctl', 'show', 'deploy-control', '-p', 'MainPID', '--value']):
        raise RuntimeError('corrupted download interrupted the control service')
    if Path('/var/lib/deploy-platform/install-transaction.json').exists():
        raise RuntimeError('corrupted download entered installation')
    corrupt.unlink()
    for _ in range(10):
        if not Path('/var/lib/deploy-platform/updates/active.json').exists(): break
        time.sleep(0.2)
    result.unlink()
    print('PASS: corrupt release download rejected before service interruption; explicit retry only', flush=True)
    queued = request('/api/v1/updates/upgrade', {'version': manifest['version']})
    if not queued['execution']: raise RuntimeError('upgrade not queued')
    print('PASS: real Go GitHub HTTPS discovery, private token and explicit API approval', flush=True)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        result = Path('/var/lib/deploy-platform/updates/result.json')
        value = json.loads(result.read_text()) if result.exists() else {}
        if value.get('phase') == 'failed': raise RuntimeError('independent update worker failed')
        if value.get('phase') == 'succeeded': break
        time.sleep(1)
    else: raise RuntimeError('update delivery deadline')
    new_pid = subprocess.check_output(['systemctl', 'show', 'deploy-control', '-p', 'MainPID', '--value'])
    if old_pid == new_pid: raise RuntimeError('control service was not replaced')
    print('PASS: real systemd.path downloads validated asset and upgrades across control restart', flush=True)


def serve():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.headers.get('Authorization') != 'Bearer isolated-release-token':
                self.send_error(403); return
            if self.path in ('/repos/' + REPO + '/releases/latest', '/repos/' + REPO + '/releases/tags/' + json.loads(Path('/var/tmp/test-release.json').read_text())['tag_name']):
                data = Path('/var/tmp/test-release.json').read_bytes()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            elif self.path == '/repos/' + REPO + '/releases/assets/123':
                source = Path('/var/tmp/new.run')
                self.send_response(200); self.send_header('Content-Length', str(source.stat().st_size)); self.end_headers()
                with source.open('rb') as handle:
                    if Path('/run/cloud-test-corrupt-download').exists():
                        first = handle.read(1)
                        self.wfile.write(bytes([first[0] ^ 1]))
                    import shutil
                    shutil.copyfileobj(handle, self.wfile, 256*1024)
            else: self.send_error(404)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 14443), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain('/usr/local/share/ca-certificates/cloud-test-github.crt', '/var/tmp/test-github.key')
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    if sys.argv[1:] != ['serve']: raise RuntimeError('test endpoint only')
    serve()
