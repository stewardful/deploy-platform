"""Installed server policy, private job storage and launch serialization."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager

PROTOCOL = 1
JOBS = Path('/var/lib/deploy-platform/delivery/jobs')
LAUNCH_LOCK = Path('/run/lock/deploy-platform-ci-launch.lock')
WORKER_LOCK = Path('/run/lock/deploy-platform-ci.lock')


def trusted(path, *, directory=False, private=True):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & (0o077 if private else 0o022):
        raise ValueError('untrusted delivery path')
    return path


def config():
    from update_worker import settings, CURRENT
    repository, _ = settings(read_token=False)
    manifest = json.loads(trusted(CURRENT, private=False).read_text())
    return {'protocol': PROTOCOL, 'repository': repository, 'arch': manifest['arch']}


def stage(run_id, attempt, *, exists=True):
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9]{1,20}', run_id): raise ValueError('invalid run')
    if not isinstance(attempt, str) or not re.fullmatch(r'[0-9]{1,6}', attempt): raise ValueError('invalid attempt')
    trusted(JOBS.parent, directory=True)
    trusted(JOBS, directory=True)
    path = JOBS / (run_id + '-' + attempt)
    if exists: trusted(path, directory=True)
    return path


@contextmanager
def locked(path, *, nonblocking=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('untrusted lock')
        fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
