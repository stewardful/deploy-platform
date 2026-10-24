"""CLI adapters for existing stable API and on-demand dev artifact worker."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sys

import update_worker as worker
from state import write_record


def api(path, body=None):
    # Reuse the node's bounded authenticated loopback client (no proxy/redirects).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from smoke import request
    return request('/api/v1/updates'+path, body)


def queue(request):
    # Same submission lock as the Go/Web updater; worker and installer locks remain authoritative.
    with (worker.SPOOL/'submission.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in ('request.json', 'active.json'):
            path = worker.SPOOL/name
            if path.exists() or path.is_symlink():
                raise ValueError('upgrade pending or interrupted; inspect status before retrying')
        request = {**request, 'phase': 'queued'}
        (worker.SPOOL/'result.json').unlink(missing_ok=True)
        write_record(worker.SPOOL/'request.json', request)
    return request


def main(argv):
    p = argparse.ArgumentParser(prog='cloudctl', allow_abbrev=False)
    sub = p.add_subparsers(dest='command', required=True)
    update = sub.add_parser('update', help='Inspect platform update discovery and execution')
    update.add_argument('action', choices=('check', 'status'))
    stable = sub.add_parser('upgrade', help='Queue a checked stable GitHub Release using the existing local API', allow_abbrev=False)
    stable.add_argument('--version', required=True, help='Exact X.Y.Z from update check')
    stable.add_argument('--yes', action='store_true', required=True, help='Explicitly approve this stable version')
    dev = sub.add_parser('upgrade-dev', help='Inspect or queue a dev build; never selects a stable Release', allow_abbrev=False)
    dev.add_argument('--run-id', help='Exact published dev run ID; default: latest CI-passed public prerelease')
    dev.add_argument('--yes', action='store_true', help='Approve this dev run; otherwise inspect metadata only')
    args = p.parse_args(argv)
    if os.geteuid() != 0: raise ValueError('sudo is required')
    if args.command == 'update':
        if args.action == 'check':
            result = api('/check', {})
            if not result.get('enabled') or result.get('error'): raise ValueError('stable update check unavailable')
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
    elif args.command == 'upgrade':
        if not re.fullmatch(worker.VERSION, args.version): raise ValueError('stable X.Y.Z required')
        # The API revalidates the checked candidate and queues the original worker.
        result = api('/upgrade', {'version': args.version})
    else:
        if args.run_id is not None and not re.fullmatch(r'[1-9][0-9]{0,19}', args.run_id): raise ValueError('positive run ID required')
        from dev_update import discover
        result = discover(args.run_id)
        if args.yes: result = queue(result)
        else: result = {**result, 'phase': 'checked'}
    print(json.dumps(result, indent=2))
    if args.command != 'update' and (args.command != 'upgrade-dev' or args.yes):
        print('Accepted, not yet installed. Use cloudctl update status and cloudctl system-logs update.')
