"""Anonymous public dev Releases; one pinned request and the shared protected installer."""
import json
from pathlib import Path
import platform
import re
import tempfile
import urllib.request

import update_worker as worker
from dev_manifest import validate


def asset_url(repository, tag, name):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or '..' in repository:
        raise ValueError('invalid repository')
    if not re.fullmatch(r'dev-(?:latest|[1-9][0-9]{0,19})', tag) or not re.fullmatch(r'[A-Za-z0-9._-]+', name):
        raise ValueError('invalid dev asset path')
    return f'https://github.com/{repository}/releases/download/{tag}/{name}'


def open_public(url):
    if not worker.trusted_url(url): raise ValueError('untrusted dev asset URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'deploy-platform-dev-updater'})
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), worker.AssetRedirect()).open(request, timeout=20)


def metadata(repository, tag):
    with open_public(asset_url(repository, tag, 'dev.json')) as response: data = response.read(16385)
    if len(data) > 16384: raise ValueError('oversized dev manifest')
    return validate(json.loads(data), repository)


def discover(run_id=None, *, initial=False):
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', run_id)):
        raise ValueError('invalid run ID')
    # Public dev delivery must not read or depend on any saved private token.
    repo, _ = worker.settings(read_token=False)
    current = None if initial else json.loads(worker.CURRENT.read_text())
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch is None or current is not None and current['arch'] != arch: raise ValueError('node architecture mismatch')
    index = metadata(repo, 'dev-'+run_id if run_id else 'dev-latest')
    if run_id is not None and index['run_id'] != run_id: raise ValueError('dev run identity mismatch')
    # Latest is only a discovery pointer. Pin the immutable build before approval.
    pinned = metadata(repo, 'dev-'+index['run_id']) if run_id is None else index
    if pinned != index: raise ValueError('latest differs from published build')
    item = pinned['installers'][arch]
    return {'channel': 'dev', 'repository': repo, 'run_id': pinned['run_id'], 'commit': pinned['commit'],
            'version': pinned['version'], 'arch': arch, 'name': item['name'],
            'size': item['size'], 'digest': 'sha256:'+item['sha256']}


def execute(request, *, before_install=None):
    initial = request.get('operation') == 'install'
    fresh = discover(request['run_id'], initial=initial)
    if fresh != {k: v for k, v in request.items() if k not in ('phase', 'operation')}:
        raise ValueError('dev build changed after approval; inspect again')
    worker.report(request['version'], 'downloading', '正在下载并校验 dev 构建；应用继续运行')
    with tempfile.TemporaryDirectory(prefix='cloud-dev-update-', dir=worker.DOWNLOAD_ROOT) as directory:
        installer = Path(directory)/'installer.run'
        url = asset_url(request['repository'], 'dev-'+request['run_id'], request['name'])
        worker.download(request['repository'], '', request, installer, open_asset=lambda: open_public(url))
        from archive import installer_manifest
        candidate = installer_manifest(installer)
        if (candidate.get('package_kind') != 'platform-update' or candidate.get('version') != request['version'] or candidate.get('arch') != request['arch'] or
                candidate.get('build') != {'channel': 'dev', 'commit': request['commit']}):
            raise ValueError('installer build provenance mismatch')
        if before_install is not None: before_install()
        worker.install_payload(installer, request['version'], initial=initial)
    installed = json.loads(worker.CURRENT.read_text())
    if (installed['version'] != request['version'] or installed['arch'] != request['arch'] or
            installed.get('build') != candidate['build'] or worker.TRANSACTION.exists()):
        raise ValueError('dev installation did not commit')
    worker.report(request['version'], 'succeeded', 'dev 更新完成，已有项目验证通过')
