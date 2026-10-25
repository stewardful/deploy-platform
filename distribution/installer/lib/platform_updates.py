"""Independent CLI discovery, policy and shared task submission."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets

import update_worker as worker
from state import write_record


def queue(request):
    # Same submission lock as the Go/Web updater; worker and installer locks remain authoritative.
    with (worker.SPOOL/'submission.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ('request.json', 'active.json'):
            path = worker.SPOOL/name
            if path.exists() or path.is_symlink():
                raise ValueError('upgrade pending or interrupted; inspect status before retrying')
        request = {**request, 'phase': 'queued','task_id':secrets.token_hex(16)}
        (worker.SPOOL/'result.json').unlink(missing_ok=True)
        write_record(worker.SPOOL/'request.json', request)
    return request


def main(argv):
    p = argparse.ArgumentParser(prog='cloudctl', allow_abbrev=False)
    sub = p.add_subparsers(dest='command', required=True)
    update = sub.add_parser('update', help='Inspect platform update discovery and execution')
    update.add_argument('action', choices=('check', 'status', 'policy'))
    update.add_argument('--dev-auto-upgrade', choices=('on', 'off'), help='Local policy only; default is off')
    args = p.parse_args(argv)
    if os.geteuid() != 0: raise ValueError('sudo is required')
    if args.command == 'update':
        if args.dev_auto_upgrade is not None and args.action != 'policy': raise ValueError('policy option requires update policy')
        if args.action == 'policy':
            from install_policy import policy
            policy()  # Validate before changing any local configuration.
            cfg = json.loads(worker.private_read(worker.CONFIG, 4096)) if worker.CONFIG.exists() or worker.CONFIG.is_symlink() else {}
            from first_install import directory
            directory(worker.CONFIG.parent)
            cfg.setdefault('repository','stewardful/deploy-platform')
            cfg.setdefault('token_file','')
            if args.dev_auto_upgrade is not None:
                cfg['dev_auto_upgrade']=args.dev_auto_upgrade=='on'
                write_record(worker.CONFIG,cfg)
            repo, enabled = policy()
            result = {'repository': repo, 'dev_auto_upgrade': enabled}
        elif args.action == 'check':
            import platform
            arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
            result={**worker.initial_stable(None,arch),'phase':'checked'}
        else:
            # Remains available when the control service is stopped during activation.
            result = {'execution': None}
            for name in ('active.json', 'request.json', 'result.json'):
                path = worker.SPOOL/name
                if path.exists():
                    result['execution'] = json.loads(worker.private_read(path, 16384))
                    if name == 'request.json': result['execution']['phase'] = 'queued'
                    if name == 'active.json':
                        with (worker.SPOOL/'worker.lock').open('a') as lock:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                result['execution']['phase'] = 'interrupted'
                            except BlockingIOError:
                                progress = worker.SPOOL/'result.json'
                                if progress.exists(): result['execution'] = json.loads(worker.private_read(progress, 16384))
                    break
    print(json.dumps(result,indent=2))
