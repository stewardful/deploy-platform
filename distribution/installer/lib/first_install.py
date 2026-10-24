"""First platform installation via the same cloudctl, worker and installer as updates."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

import update_worker as worker
from state import write_record
from platform_updates import queue
from ci_state import trusted

CLI_ROOT = Path(__file__).resolve().parent.parent


class InstallError(ValueError):
    """Safe static operator message; never contains token or HTTP response data."""


def directory(path):
    if path != path.parent:
        if path.parent.exists(): trusted(path.parent, directory=True, private=False)
        else: directory(path.parent)
    if not path.exists() and not path.is_symlink(): path.mkdir(mode=0o700)
    trusted(path, directory=True)


def assert_bootstrap_launcher():
    launcher = Path('/usr/local/bin/cloudctl')
    if not launcher.is_symlink(): raise InstallError('cloudctl is not a managed bootstrap link')
    target = launcher.readlink()
    if target.parent.parent != Path('/opt/deploy-platform/bootstrap') or target.name != 'cloudctl' or not re.fullmatch('[a-f0-9]{64}', target.parent.name):
        raise InstallError('cloudctl bootstrap target differs')
    for path in (target.parent, target.parent.parent, target.parent.parent.parent):
        trusted(path, directory=True, private=False)
    trusted(target, private=False)
    from archive import digest
    if digest(target) != target.parent.name: raise InstallError('cloudctl bootstrap checksum differs')


def preflight():
    subprocess.run(['/usr/bin/bash', '-c', 'source "$1"; check_supported_os', 'cloudctl', str(Path(__file__).with_name('os.sh'))], check=True)
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file() or not Path('/run/systemd/system').is_dir():
        raise InstallError('systemd and cgroup v2 required')
    subprocess.run(['systemctl', 'show-environment'], check=True, stdout=subprocess.DEVNULL)
    if shutil.disk_usage('/opt').free < 2 * 1024**3: raise InstallError('2 GiB free disk required')


def execute_offline(request):
    path = worker.SPOOL/'first-install.run'
    trusted(path)
    from archive import digest
    actual = digest(path)
    if actual != request['digest']:
        raise InstallError('first-install package changed')
    worker.install_payload(path, request['version'], initial=True)
    installed = json.loads(worker.CURRENT.read_text())
    if any(installed.get(key) != request.get(key) for key in ('version', 'arch', 'build')) or worker.TRANSACTION.exists():
        raise InstallError('first installation did not commit')
    path.unlink()
    worker.report(request['version'], 'succeeded', '平台首次安装完成')


def main(argv):
    p = argparse.ArgumentParser(prog='cloudctl install', allow_abbrev=False,
        description='Install this node once. Supports GitHub stable/dev downloads or a local verified installer; uses the existing installer and independent update worker.')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', type=Path, help='Local .run installer')
    source.add_argument('--channel', choices=('stable', 'dev'), help='Download a trusted GitHub build')
    p.add_argument('--version', help='Stable X.Y.Z; default: latest stable Release')
    p.add_argument('--run-id', help='Published dev run; default: latest public prerelease')
    p.add_argument('--repository', default='stewardful/deploy-platform')
    p.add_argument('--token-file', type=Path, help='Stable only: absolute private GitHub token file; never a token value')
    p.add_argument('--no-wait', action='store_true')
    args = p.parse_args(argv)
    if os.geteuid() != 0: raise InstallError('sudo is required')
    if worker.CURRENT.exists() or Path('/etc/deploy-platform/install.json').exists():
        raise InstallError('platform already installed; use cloudctl upgrade or upgrade-dev')
    if args.file and (args.version or args.run_id or args.token_file): raise InstallError('--file cannot be combined with online source options')
    if args.run_id and args.channel != 'dev' or args.version and args.channel != 'stable': raise InstallError('version/run ID differs from channel')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository) or '..' in args.repository: raise InstallError('invalid repository')
    if args.token_file:
        if args.channel != 'stable': raise InstallError('--token-file is only used for stable installs')
        if not args.token_file.is_absolute(): raise InstallError('absolute token path required')
        worker.private_read(args.token_file, 1024)
    preflight()
    os.umask(0o077)
    directory(worker.CONFIG.parent)
    directory(worker.SPOOL)
    config = {'repository': args.repository, 'token_file': str(args.token_file) if args.token_file else ''}
    if worker.CONFIG.exists() or worker.CONFIG.is_symlink():
        if json.loads(worker.private_read(worker.CONFIG, 4096)) != config: raise InstallError('existing repository/token configuration differs')
    else: write_record(worker.CONFIG, config)
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if arch is None: raise InstallError('unsupported architecture')
    if args.file:
        if args.file.is_symlink() or not args.file.is_file() or args.file.stat().st_size > worker.MAX_INSTALLER:
            raise InstallError('invalid local installer')
        from archive import installer_manifest, digest
        # Stage a private copy before queueing. Never execute a caller-writable file.
        target = worker.SPOOL/'first-install.run'
        with args.file.open('rb') as src, target.open('xb') as dst:
            size = 0
            while chunk := src.read(256*1024):
                size += len(chunk)
                if size > worker.MAX_INSTALLER: raise InstallError('installer exceeds size limit')
                dst.write(chunk)
        target.chmod(0o600)
        manifest = installer_manifest(target)
        if manifest.get('arch') != arch: raise InstallError('installer architecture differs')
        checksum = digest(target)
        request = {key: manifest.get(key) for key in ('version', 'arch', 'build')}
        request.update(channel='offline', digest=checksum, operation='install')
    elif args.channel == 'dev':
        from dev_update import discover
        request = dict(discover(args.run_id, initial=True), operation='install')
    else:
        request = dict(worker.initial_stable(args.version, arch), operation='install')
    # The bootstrap CLI is part of this platform, not another installed product.
    root = CLI_ROOT
    launcher = root/'cloudctl'
    if launcher.is_file():
        destination = Path('/usr/local/bin/cloudctl')
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or destination.resolve() != launcher:
                raise InstallError('refusing to overwrite an unrelated cloudctl')
        else: destination.symlink_to(launcher)
    queue(request)
    subprocess.run(['systemd-run', '--no-block', '--collect', '--unit=deploy-platform-bootstrap',
                    '--property=Type=oneshot', '--property=TimeoutStartSec=infinity', '--property=UMask=0077',
                    '--property=Nice=10', '--property=MemoryHigh=256M',
                    '/usr/bin/python3', '-I', '-B', str(Path(__file__).with_name('update_worker.py'))], check=True)
    if args.no_wait:
        print('First installation accepted. Use cloudctl update status and cloudctl system-logs install.'); return
    deadline = time.monotonic() + 2400
    while time.monotonic() < deadline:
        result = worker.SPOOL/'result.json'
        if result.exists():
            value = json.loads(worker.private_read(result, 16384))
            if value['phase'] == 'succeeded': print(json.dumps(value, indent=2)); return
            if value['phase'] == 'failed': raise InstallError('installation failed; inspect cloudctl system-logs install')
        time.sleep(1)
    raise TimeoutError('installation continues independently; inspect cloudctl update status')
