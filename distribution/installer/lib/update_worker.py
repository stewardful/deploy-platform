#!/usr/bin/env python3
"""On-demand stable/dev platform update worker. No HTTP listener; no project mutation logic.

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

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import write_record

SPOOL = Path('/var/lib/deploy-platform/updates')
DOWNLOAD_ROOT = Path('/var/tmp')
CONFIG = Path('/etc/deploy-platform/updates.json')
CURRENT = Path('/opt/deploy-platform/current/manifest.json')
TRANSACTION = Path('/var/lib/deploy-platform/install-transaction.json')
VERSION = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
MAX_INSTALLER = 512 * 1024**2
ACTIVE_DETAILS = {}


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


def settings(*, read_token=True):
    cfg = {'repository': 'stewardful/deploy-platform', 'token_file': ''}
    if CONFIG.exists() or CONFIG.is_symlink():
        cfg.update(json.loads(private_read(CONFIG, 4096)))
    repo = cfg['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or '..' in repo:
        raise ValueError('invalid configured repository')
    token = ''
    if cfg['token_file'] and read_token:
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


def download(repo, token, asset, target, *, open_asset=None):
    if shutil.disk_usage(target.parent).free < asset['size'] + 2 * 1024**3:
        raise ValueError('insufficient free space')
    start, size, digest = time.monotonic(), 0, hashlib.sha256()
    open_asset = open_asset or (lambda: open_github('repos/' + repo + '/releases/assets/' + str(asset['id']), token, True))
    with open_asset() as response, target.open('xb') as output:
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
    initial = request.get('operation') == 'install'
    if initial:
        if CURRENT.exists(): raise ValueError('platform already installed')
        current = None
    else:
        current = json.loads(CURRENT.read_text())
        if (tuple(map(int, version.split('.'))) < tuple(map(int, current['version'].split('.'))) or
                (version == current['version'] and current.get('build', {}).get('channel') != 'dev')):
            raise ValueError('only newer releases or same-version dev-to-stable promotion allowed')
    arch = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if arch is None or current is not None and current['arch'] != arch:
        raise ValueError('unsupported node architecture')
    repo, token = settings()
    if repo != request.get('repository'):
        raise ValueError('configured repository changed; check again')
    asset = discover(repo, token, request, arch)
    report(version, 'downloading', '正在下载并校验安装包，应用继续运行')
    with tempfile.TemporaryDirectory(prefix='cloud-update-', dir=DOWNLOAD_ROOT) as directory:
        installer = Path(directory) / 'installer.run'
        download(repo, token, asset, installer)
        install_payload(installer, version, initial=initial)
    installed = json.loads(CURRENT.read_text())
    if installed['version'] != version or installed.get('build', {}).get('channel') == 'dev' or TRANSACTION.exists():
        raise ValueError('installation has not committed')
    report(version, 'succeeded', '升级完成，已有项目验证通过')


def install_payload(installer, version, *, initial=False, output=None, progress=None):
    """One installer boundary for first install, stable, dev and CI delivery."""
    from archive import installer_manifest
    if installer_manifest(installer).get('version') != version:
        raise ValueError('installer version differs from approved version')
    notify = progress or report
    notify(version, 'verifying', '正在检查安装包完整性')
    options = {} if output is None else {'stdout': output, 'stderr': subprocess.STDOUT}
    subprocess.run(['/usr/bin/bash', str(installer), '--check'], check=True, timeout=120, **options)
    notify(version, 'installing', '正在安装平台并验证状态')
    flags = [] if initial else ['--upgrade', '--skip-apt']
    subprocess.run(['/usr/bin/bash', str(installer), *flags], check=True, **options)


def initial_stable(version, arch):
    repo, token = settings()
    path = 'repos/' + repo + '/releases/' + ('tags/v'+version if version else 'latest')
    if version is not None and not re.fullmatch(VERSION, version): raise ValueError('stable X.Y.Z required')
    with open_github(path, token) as response: data = response.read(1024**2+1)
    if len(data) > 1024**2: raise ValueError('oversized Release metadata')
    release = json.loads(data)
    version = release['tag_name'].removeprefix('v')
    if release['draft'] or release['prerelease'] or not re.fullmatch(VERSION, version): raise ValueError('stable Release required')
    name = 'deploy-platform-'+version+'-linux-'+arch+'.run'
    matches = [a for a in release['assets'] if a['name'] == name]
    if len(matches) != 1: raise ValueError('missing architecture installer')
    asset = matches[0]
    result = {'channel': 'stable', 'version': version, 'repository': repo, 'asset_id': asset['id'], 'size': asset['size'], 'digest': asset['digest']}
    discover(repo, token, result, arch)
    return result


def report(version, phase, message):
    write_record(SPOOL / 'result.json', {**ACTIVE_DETAILS, 'version': version, 'phase': phase, 'message': message})


def main():
    global ACTIVE_DETAILS
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
        request = {}
        try:
            request = json.loads(private_read(active, 4096))
            version = request.get('version', '')
            ACTIVE_DETAILS = {k: request[k] for k in ('channel', 'run_id', 'commit') if k in request}
            report(version, 'checking', '正在确认已批准的版本')
            if request.get('operation') == 'install' and CURRENT.exists():
                raise ValueError('platform already installed')
            if request.get('channel') == 'offline' and request.get('operation') == 'install':
                from first_install import execute_offline
                execute_offline(request)
            elif request.get('channel') == 'dev':
                from dev_update import execute as execute_dev
                execute_dev(request)
            elif request.get('channel', 'stable') == 'stable':
                execute(request)
            else:
                raise ValueError('unsupported update channel')
        except Exception:
            # Neither HTTP signed URLs, token values, nor private installer output
            # are returned through the public API. Detailed installer logs: journal.
            command = 'install' if request.get('operation') == 'install' else 'update'
            report(version, 'failed', '操作未完成；请用 cloudctl system-logs '+command+' 查看日志并确认恢复状态')
            raise RuntimeError('installation/update failed; inspect cloudctl system-logs '+command+' and installation transaction') from None
        finally:
            active.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
