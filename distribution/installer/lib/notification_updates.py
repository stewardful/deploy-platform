"""Local dev policy and verified, durable once-per-build automatic upgrade attempts."""
import json
import re
import urllib.error
import update_worker as worker
from state import write_record


def policy():
    repo, _ = worker.settings(read_token=False)
    config = json.loads(worker.private_read(worker.CONFIG, 4096)) if worker.CONFIG.exists() else {}
    section = config.get('notifications', {})
    if not isinstance(section, dict) or set(section)-{'dev_auto_upgrade','secret_file'} or type(section.get('dev_auto_upgrade', False)) is not bool:
        raise ValueError('invalid local notification policy')
    return repo, section.get('dev_auto_upgrade', False)


def receipt(repo):
    path = worker.SPOOL/'notification-dev.json'
    if not path.exists() and not path.is_symlink(): return None
    value = json.loads(worker.private_read(path, 4096))
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'repository', 'run_id', 'phase'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or value['repository'] != repo or
            not isinstance(value['run_id'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value['run_id']) or int(value['run_id']) >= 2**64 or
            value['phase'] not in ('attempted', 'failed', 'succeeded', 'skipped')):
        raise ValueError('invalid automatic upgrade receipt')
    return value


def record(repo, run, phase):
    write_record(worker.SPOOL/'notification-dev.json', {'schema_version': 1, 'repository': repo, 'run_id': run, 'phase': phase})


def execute(request):
    from dev_update import discover, execute as install
    repo, enabled = policy()
    run = request.get('run_id')
    if request.get('channel') != 'dev-notice' or request.get('phase') != 'queued' or set(request)!={'channel','repository','run_id','phase'} or request.get('repository') != repo:
        raise ValueError('invalid notification request')
    if not isinstance(run, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', run) or int(run) >= 2**64:
        raise ValueError('invalid notification run')
    if not enabled:
        worker.report('', 'skipped', '本机未启用 dev 自动升级'); return
    if not worker.CURRENT.is_file(): raise ValueError('automatic updates require an installed platform')
    old = receipt(repo)
    if old is not None and int(run) <= int(old['run_id']):
        worker.report('', 'skipped', '该通知已处理，不自动重试'); return
    # Notification fields cannot advance the ledger: resolve a trusted public build first.
    candidate = discover(run)
    run = candidate['run_id']
    if old is not None and int(run) <= int(old['run_id']):
        worker.report(candidate['version'], 'skipped', '该构建已尝试，不自动重试'); return
    try: latest = discover()
    except urllib.error.HTTPError as error:
        # The fixed build is published before the first latest pointer exists.
        if error.code != 404: raise
        latest = candidate
    if int(latest['run_id']) > int(run):
        record(repo, run, 'skipped')
        worker.report(candidate['version'], 'skipped', '已有更新构建，忽略旧通知'); return
    worker.ACTIVE_DETAILS.update(run_id=run, commit=candidate['commit'])

    def guard():
        current_repo, allowed = policy()
        if current_repo != repo or not allowed: raise ValueError('local automatic upgrade policy changed')
    guard()
    # Claim before download/installation. A restart or failed installer must not retry it.
    record(repo, run, 'attempted')
    try:
        current = json.loads(worker.CURRENT.read_text())
        if current.get('build', {}).get('commit') == candidate['commit']:
            record(repo, run, 'skipped')
            worker.report(candidate['version'], 'skipped', '该提交已安装，无需重复升级'); return
        install(candidate, before_install=guard)
        record(repo, run, 'succeeded')
    except Exception:
        record(repo, run, 'failed')
        raise
