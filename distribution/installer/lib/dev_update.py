"""Trusted dev Actions adapter. Uses existing download, ZIP and installer components."""
import json
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

import update_worker as worker
import delivery

WORKFLOW = '.github/workflows/dev-deploy.yml'
MAX_RECENT_RUNS = 100
RUNS_PER_PAGE = 20


class CandidateUnavailable(ValueError):
    """A run is not an installable candidate; transport/auth failures are not candidates."""


class Redirect(worker.AssetRedirect):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Dev Actions storage differs from Release storage. Never forward repository credentials.
        delivery.check_url(newurl)
        result = urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)
        result.remove_header('Authorization')
        return result


def github(repo, token, path, binary=False):
    req = urllib.request.Request('https://api.github.com/repos/'+repo+'/'+path, headers={
        'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json',
        'User-Agent': 'deploy-platform-dev-updater', 'X-GitHub-Api-Version': '2022-11-28'})
    if token: req.add_header('Authorization', 'Bearer '+token)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect()).open(req, timeout=20)


def metadata(repo, token, path):
    with github(repo, token, path) as response: data = response.read(1024**2+1)
    if len(data) > 1024**2: raise ValueError('oversized GitHub metadata')
    return json.loads(data)


def discover(run_id=None, *, initial=False):
    if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', run_id)):
        raise ValueError('invalid run ID')
    repo, token = worker.settings()
    current = None if initial else json.loads(worker.CURRENT.read_text())
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch is None or current is not None and current['arch'] != arch: raise ValueError('node architecture mismatch')
    if run_id is not None:
        return candidate(repo, token, arch, run_id, metadata(repo, token, 'actions/runs/'+run_id))
    # Small pages keep GitHub's rich run metadata below the existing response cap.
    for page in range(1, MAX_RECENT_RUNS // RUNS_PER_PAGE + 1):
        response = metadata(repo, token, 'actions/workflows/dev-deploy.yml/runs?branch=dev&status=completed&per_page='+str(RUNS_PER_PAGE)+'&page='+str(page))
        runs = response.get('workflow_runs') if isinstance(response, dict) else None
        if not isinstance(runs, list) or len(runs) > RUNS_PER_PAGE:
            raise ValueError('invalid workflow run listing')
        # GitHub lists newest created runs first. run_number orders source builds;
        # completion/update times of reruns must not override that order.
        if any(not isinstance(run, dict) or type(run.get('run_number')) is not int or run['run_number'] <= 0 or
               type(run.get('id')) is not int or not 0 < run['id'] < 10**20 for run in runs):
            raise ValueError('invalid workflow run identity')
        for run in sorted(runs, key=lambda item: item['run_number'], reverse=True):
            try:
                return candidate(repo, token, arch, str(run['id']), run)
            except CandidateUnavailable:
                continue
            except urllib.error.HTTPError as error:
                # A deleted run/artifact is unavailable. Auth, rate limits, network
                # and malformed responses must abort rather than select older code.
                if error.code not in (404, 410): raise
        if len(runs) < RUNS_PER_PAGE: break
    raise ValueError('no installable dev build in the latest 100 completed runs; specify --run-id for an older build')


def candidate(repo, token, arch, run_id, run):
    # Workflow and source repository are pinned; PRs, forks and other workflows cannot be installed.
    if (str(run.get('id')) != run_id or run.get('path') != WORKFLOW or
            run.get('event') not in ('push', 'workflow_dispatch') or run.get('head_branch') != 'dev' or
            run.get('repository', {}).get('full_name') != repo or run.get('head_repository', {}).get('full_name') != repo or
            not re.fullmatch(r'[a-f0-9]{40}', run.get('head_sha', '')) or
            run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure')):
        raise CandidateUnavailable('completed trusted dev workflow required')
    artifacts = metadata(repo, token, 'actions/runs/'+run_id+'/artifacts?per_page=100')['artifacts']
    matches = [a for a in artifacts if a.get('name') == 'deploy-platform-linux-'+arch]
    if len(matches) != 1: raise CandidateUnavailable('artifact missing or ambiguous')
    item = matches[0]
    origin = item.get('workflow_run', {})
    if (item.get('expired') is not False or str(origin.get('id')) != run_id or origin.get('head_sha') != run['head_sha'] or
            origin.get('head_branch') != 'dev' or origin.get('repository_id') != run['repository'].get('id') or
            origin.get('head_repository_id') != run['repository'].get('id') or type(item.get('id')) is not int or item['id'] <= 0 or
            type(item.get('size_in_bytes')) is not int or not 0 < item['size_in_bytes'] <= worker.MAX_INSTALLER or
            not re.fullmatch(r'sha256:[a-f0-9]{64}', item.get('digest', ''))):
        raise CandidateUnavailable('artifact identity mismatch')
    # A failed SSH deployment may still have a good build. Require the reusable build/test
    # jobs to have succeeded, independently of the deployment job's conclusion.
    jobs = metadata(repo, token, 'actions/runs/'+run_id+'/jobs?per_page=100')['jobs']
    required = ('build / cli', 'build / test', 'build / package (ubuntu-24.04'+('-arm' if arch == 'arm64' else '')+', '+arch+')')
    for name in required:
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1 or matches[0].get('conclusion') != 'success': raise CandidateUnavailable('successful build and tests required')
    return {'channel': 'dev', 'repository': repo, 'run_id': run_id, 'commit': run['head_sha'],
            'arch': arch, 'artifact_id': item['id'], 'size': item['size_in_bytes'], 'digest': item['digest']}


def extract(source, stage, request):
    # SHA256 of the whole ZIP was already checked against GitHub metadata. Read only
    # a bounded checksum entry to configure the existing closed ZIP extractor.
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) != 2: raise ValueError('expected installer and checksum only')
        candidates = [m for m in members if re.fullmatch(r'deploy-platform-('+worker.VERSION+r')-linux-'+request['arch']+r'\.run', m.filename)]
        if len(candidates) != 1: raise ValueError('wrong architecture or artifact inventory')
        installer = candidates[0]
        checksum = archive.getinfo(installer.filename[:-4]+'.sha256')
        if not 0 < installer.file_size <= worker.MAX_INSTALLER or checksum.file_size > 16384:
            raise ValueError('oversized installer or checksum')
        text = archive.read(checksum).decode('ascii')
        matches = re.findall(r'^([a-f0-9]{64})  '+re.escape(installer.filename)+r'$', text, re.MULTILINE)
        if len(matches) != 1: raise ValueError('missing installer checksum')
        version = installer.filename[len('deploy-platform-'):].split('-linux-', 1)[0]
    delivery.extract(source, stage, dict(request, version=version, installer_size=installer.file_size, installer_sha256=matches[0]))
    return version


def execute(request):
    # Recheck source, tests, architecture, artifact ID, digest and size before download.
    initial = request.get('operation') == 'install'
    fresh = discover(request['run_id'], initial=True) if initial else discover(request['run_id'])
    if fresh != {k: v for k, v in request.items() if k not in ('phase', 'operation')}: raise ValueError('dev build changed after approval; inspect again')
    repo, token = worker.settings()
    worker.report('', 'downloading', '正在下载并校验 dev 构建；应用继续运行')
    with tempfile.TemporaryDirectory(prefix='cloud-dev-update-', dir=worker.DOWNLOAD_ROOT) as directory:
        stage = Path(directory)
        source = stage/'artifact.zip'
        worker.download(repo, token, {'id': request['artifact_id'], 'size': request['size'], 'digest': request['digest']}, source,
                        open_asset=lambda: github(repo, token, 'actions/artifacts/'+str(request['artifact_id'])+'/zip', True))
        version = extract(source, stage, request)
        installer = stage/'installer.run'
        from archive import installer_manifest
        candidate = installer_manifest(installer)
        if (candidate.get('version') != version or candidate.get('arch') != request['arch'] or
                candidate.get('build') != {'channel': 'dev', 'commit': request['commit']}):
            raise ValueError('installer build provenance mismatch; rebuild dev with current packaging')
        worker.install_payload(installer, version, initial=initial)
    installed = json.loads(worker.CURRENT.read_text())
    if (installed['version'] != version or installed['arch'] != request['arch'] or
            installed.get('build') != candidate['build'] or worker.TRANSACTION.exists()):
        raise ValueError('dev installation did not commit')
    worker.report(version, 'succeeded', 'dev 更新完成，已有项目验证通过')
