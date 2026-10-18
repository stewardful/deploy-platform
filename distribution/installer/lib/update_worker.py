#!/usr/bin/env python3
"""On-demand trusted-release downloader. No HTTP listener; no project mutation logic.

Runs outside deploy-control's cgroup. The existing installer owns compatibility,
maintenance, backups, validation and rollback. Never retries an interrupted upgrade.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from state import write_record

SPOOL = Path('/var/lib/deploy-platform/updates')
DOWNLOAD_ROOT = Path('/var/tmp')
CONFIG = Path('/etc/deploy-platform/updates.json')
CURRENT = Path('/opt/deploy-platform/current/manifest.json')
TRANSACTION = Path('/var/lib/deploy-platform/install-transaction.json')
VERSION = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
MAX_INSTALLER = 512 * 1024**2


def private_read(path, limit):
    before = path.lstat()
    if not path.is_file() or path.is_symlink() or before.st_mode & 0o077:
        raise ValueError('expected private regular file')
    with path.open('rb') as handle:
        if not os.path.samestat(before, os.fstat(handle.fileno())):
            raise ValueError('file changed while opening')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file too large')
    return data


def settings():
    cfg = {'repository': 'stewardful/deploy-platform', 'token_file': ''}
    if CONFIG.exists() or CONFIG.is_symlink():
        cfg.update(json.loads(private_read(CONFIG, 4096)))
    repo = cfg['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or '..' in repo:
        raise ValueError('invalid configured repository')
    token = ''
    if cfg['token_file']:
        path = Path(cfg['token_file'])
        if not path.is_absolute():
            raise ValueError('absolute token path required')
        token = private_read(path, 1024).decode().strip()
        if not token or '\r' in token or '\n' in token:
            raise ValueError('invalid token')
    return repo, token


def trusted_url(url):
    u = urlsplit(url)
    return (u.scheme == 'https' and not u.username and not u.password and
            u.port in (None, 443) and u.hostname in (
                'api.github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com', 'github.com'))


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_url(newurl):
            raise ValueError('untrusted asset redirect')
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        # Never forward repository credentials to asset storage or signed URLs.
        result.remove_header('Authorization')
        return result


def open_github(path, token, binary=False):
    req = urllib.request.Request('https://api.github.com/' + path, headers={
        'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
        'User-Agent': 'deploy-platform-updater', 'X-GitHub-Api-Version': '2022-11-28',
    })
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), AssetRedirect()).open(req, timeout=20)


def discover(repo, token, request, arch):
    with open_github('repos/' + repo + '/releases/tags/v' + request['version'], token) as response:
        data = response.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise ValueError('release response too large')
    release = json.loads(data)
    if release['tag_name'] != 'v' + request['version'] or release['draft'] or release['prerelease']:
        raise ValueError('stable published release required')
    name = 'deploy-platform-' + request['version'] + '-linux-' + arch + '.run'
    matches = [a for a in release['assets'] if a['name'] == name]
    if len(matches) != 1:
        raise ValueError('missing or duplicate architecture asset')
    asset = matches[0]
    for key, field in [('asset_id', 'id'), ('size', 'size'), ('digest', 'digest')]:
        if request[key] != asset[field]:
            raise ValueError('release asset changed after approval; check again')
    if (not isinstance(asset['id'], int) or asset['id'] <= 0 or
            not isinstance(asset['size'], int) or not 0 < asset['size'] <= MAX_INSTALLER or
            not re.fullmatch(r'sha256:[a-f0-9]{64}', asset['digest'])):
        raise ValueError('invalid asset metadata')
    return asset


def download(repo, token, asset, target):
    if shutil.disk_usage(target.parent).free < asset['size'] + 2 * 1024**3:
        raise ValueError('insufficient free space')
    start, size, digest = time.monotonic(), 0, hashlib.sha256()
    with open_github('repos/' + repo + '/releases/assets/' + str(asset['id']), token, True) as response, target.open('xb') as output:
        while True:
            if time.monotonic() - start > 900:
                raise ValueError('download deadline exceeded')
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > asset['size']:
                raise ValueError('download exceeds approved size')
            output.write(chunk)
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size != asset['size'] or 'sha256:' + digest.hexdigest() != asset['digest']:
        raise ValueError('installer size or SHA256 mismatch')


def execute(request):
    version = request.get('version', '')
    if not re.fullmatch(VERSION, version):
        raise ValueError('invalid version')
    current = json.loads(CURRENT.read_text())
    if tuple(map(int, version.split('.'))) <= tuple(map(int, current['version'].split('.'))):
        raise ValueError('only newer releases allowed')
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if arch is None or current['arch'] != arch:
        raise ValueError('unsupported node architecture')
    repo, token = settings()
    if repo != request.get('repository'):
        raise ValueError('configured repository changed; check again')
    asset = discover(repo, token, request, arch)
    report(version, 'downloading', '正在下载并校验安装包，应用继续运行')
    with tempfile.TemporaryDirectory(prefix='cloud-update-', dir=DOWNLOAD_ROOT) as directory:
        installer = Path(directory) / 'installer.run'
        download(repo, token, asset, installer)
        # No shell interpolation or browser-supplied path/options. Existing installer
        # validates its embedded archive before performing any host changes.
        report(version, 'verifying', '正在检查安装包完整性')
        subprocess.run(['/usr/bin/bash', str(installer), '--check'], check=True, timeout=120)
        report(version, 'installing', '正在安全升级；管理页面与公网入口可能短暂断开')
        # Installer's own bounded drain/verify and ERR trap own failure recovery.
        subprocess.run(['/usr/bin/bash', str(installer), '--upgrade', '--skip-apt'], check=True)
    installed = json.loads(CURRENT.read_text())
    if installed['version'] != version or TRANSACTION.exists():
        raise ValueError('installation has not committed')
    report(version, 'succeeded', '升级完成，已有项目验证通过')


def report(version, phase, message):
    write_record(SPOOL / 'result.json', {'version': version, 'phase': phase, 'message': message})


def main():
    os.umask(0o077)
    with (SPOOL / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending, active = SPOOL / 'request.json', SPOOL / 'active.json'
        if active.exists():
            raise ValueError('interrupted upgrade requires operator recovery')
        if not pending.exists():
            return
        os.rename(pending, active)
        version = ''
        try:
            request = json.loads(private_read(active, 4096))
            version = request.get('version', '')
            report(version, 'checking', '正在确认已批准的版本')
            execute(request)
        except Exception:
            # Neither HTTP signed URLs, token values, nor private installer output
            # are returned through the public API. Detailed installer logs: journal.
            report(version, 'failed', '升级未完成；请查看升级服务日志确认回退结果后重试')
            raise RuntimeError('upgrade failed; inspect deploy-update journal and installation transaction') from None
        finally:
            active.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
