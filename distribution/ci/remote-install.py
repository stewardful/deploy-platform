#!/usr/bin/env python3
"""Root systemd entry point for one CI delivery, independent of SSH and controld."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def record(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.result-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify(stage):
    installer = stage / 'installer.run'
    checksum = stage / 'SHA256SUMS'
    for path in (installer, checksum):
        if path.is_symlink() or not path.is_file():
            raise ValueError('CI inputs must be regular files')
    text = checksum.read_text()
    if not re.fullmatch(r'[a-f0-9]{64}  installer\.run\n', text):
        raise ValueError('invalid checksum record')
    with installer.open('rb') as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(256 * 1024), b''): digest.update(chunk)
        actual = digest.hexdigest()
    if actual != text[:64]:
        raise ValueError('uploaded installer checksum mismatch')


def install(stage, commit):
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('invalid deployment commit')
    result = stage / 'result.json'
    status = {'commit': commit, 'phase': 'running', 'message': 'Checking package and running protected installer'}
    record(result, status)
    try:
        verify(stage)
        with (stage / 'install.log').open('w') as log:
            command = ['/usr/bin/bash', str(stage / 'installer.run')]
            subprocess.run(command + ['--check'], check=True, stdout=log, stderr=subprocess.STDOUT)
            args = ['--upgrade']
            if Path('/etc/deploy-platform/install.json').exists():
                args.append('--skip-apt')
            subprocess.run(command + args, check=True, stdout=log, stderr=subprocess.STDOUT)
        status.update(phase='succeeded', message='Installer committed and existing application probes passed')
        (stage / 'installer.run').unlink()
    except Exception:
        status.update(phase='failed', message='Deployment failed; inspect install.log and installer transaction before retrying')
        raise
    finally:
        record(result, status)


if __name__ == '__main__':
    os.umask(0o077)
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('root and event commit required')
    try:
        install(Path(__file__).resolve().parent, sys.argv[1])
    except Exception:
        raise SystemExit('CI deployment failed; inspect the private deployment log') from None
