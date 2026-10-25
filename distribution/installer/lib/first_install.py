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
from state import write_record, trusted
from platform_updates import queue

CLI_ROOT = Path(__file__).resolve().parent.parent


class InstallError(ValueError):
    """Safe static operator message; never contains token or HTTP response data."""


def directory(path):
    if path != path.parent:
        if path.parent.exists(): trusted(path.parent, directory=True, private=False)
        else: directory(path.parent)
    if not path.exists() and not path.is_symlink(): path.mkdir(mode=0o700)
    trusted(path, directory=True)


def assert_cli_launcher():
    launcher = Path('/usr/local/bin/cloudctl')
    if not launcher.is_symlink(): raise InstallError('cloudctl is not a managed bootstrap link')
    target = launcher.readlink()
    if target.parent.parent != Path('/opt/cloudctl/releases') or target.name != 'cloudctl' or not re.fullmatch('[a-f0-9]{64}', target.parent.name):
        raise InstallError('independent cloudctl target differs')
    for path in (target.parent, target.parent.parent, target.parent.parent.parent):
        trusted(path, directory=True, private=False)
    trusted(target, private=False)
    from archive import digest
    if digest(target) != target.parent.name: raise InstallError('independent cloudctl checksum differs')


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
    worker.install_payload(path, request['version'], initial=request.get('operation')=='install')
    installed = json.loads(worker.CURRENT.read_text())
    if any(installed.get(key) != request.get(key) for key in ('version', 'arch', 'build')) or worker.TRANSACTION.exists():
        raise InstallError('first installation did not commit')
    path.unlink()
    worker.report(request['version'], 'succeeded', '平台首次安装完成')


def main(argv):
    p=argparse.ArgumentParser(prog='cloudctl install',allow_abbrev=False,description='Ensure a verified platform build is installed. Independent from the Web/control service.')
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--file',type=Path);source.add_argument('--channel',choices=('stable','dev'))
    p.add_argument('--version');p.add_argument('--run-id');p.add_argument('--commit',help='Expected dev commit from the verified publish notification')
    p.add_argument('--repository',default='stewardful/deploy-platform');p.add_argument('--token-file',type=Path)
    p.add_argument('--automatic',action='store_true',help='Fixed dev build only; requires the local dev automation switch')
    p.add_argument('--yes',action='store_true',help='Explicitly approve a stable/local update of an installed node')
    p.add_argument('--no-wait',action='store_true')
    args=p.parse_args(argv)
    if os.geteuid()!=0:raise InstallError('sudo is required')
    if args.run_id and (args.channel!='dev' or not re.fullmatch('[1-9][0-9]{0,19}',args.run_id) or int(args.run_id)>=2**64):raise InstallError('invalid fixed dev build')
    if args.version and args.channel!='stable' or args.file and (args.version or args.run_id or args.token_file or args.commit):raise InstallError('source options differ')
    if args.commit and (args.channel!='dev' or not args.run_id or not re.fullmatch('[a-f0-9]{40}',args.commit)):raise InstallError('fixed dev commit required')
    if args.automatic and (args.channel!='dev' or not args.run_id or not args.commit or args.file or args.token_file or args.no_wait):raise InstallError('automatic installs require a fixed dev run and commit, and wait for completion')
    if args.token_file:
        if args.channel!='stable':raise InstallError('--token-file is only used for stable installs')
        if not args.token_file.is_absolute():raise InstallError('absolute token path required')
        worker.private_read(args.token_file,1024)
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repository) or '..' in args.repository:raise InstallError('invalid repository')
    if args.automatic:
        from install_policy import policy
        repo,enabled=policy()
        if repo!=args.repository:raise InstallError('automatic source differs from local configuration')
        if not enabled:print(json.dumps({'phase':'blocked','message':'local dev automation is off'}));return
    from install_policy import policy
    policy()
    preflight()
    if worker.TRANSACTION.exists() or worker.TRANSACTION.is_symlink():raise InstallError('recover the interrupted installation transaction first')
    installed=json.loads(worker.CURRENT.read_text()) if worker.CURRENT.exists() else None
    initial=installed is None
    if initial and Path('/etc/deploy-platform/install.json').exists():raise InstallError('installed manifest missing; recover the platform, not a fresh installation')
    os.umask(0o077);directory(worker.CONFIG.parent);directory(worker.SPOOL)
    config=json.loads(worker.private_read(worker.CONFIG,4096)) if worker.CONFIG.exists() or worker.CONFIG.is_symlink() else {'repository':args.repository,'token_file':''}
    if config.get('repository')!=args.repository:raise InstallError('source differs from local configuration')
    if args.token_file:config['token_file']=str(args.token_file)
    write_record(worker.CONFIG,config)
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if arch is None or installed and installed['arch']!=arch:raise InstallError('unsupported or changed architecture')
    if args.file:
        if args.file.is_symlink() or not args.file.is_file() or args.file.stat().st_size>worker.MAX_INSTALLER:raise InstallError('invalid local installer')
        from archive import installer_manifest,digest
        manifest=installer_manifest(args.file)
        if manifest.get('arch')!=arch:raise InstallError('installer architecture differs')
        if installed and all(installed.get(k)==manifest.get(k) for k in ('version','arch','build')):
            print(json.dumps({'phase':'unchanged','version':manifest['version']}));return
        if installed and not args.yes:print(json.dumps({'phase':'confirmation_required','version':manifest['version']}));return
        target=worker.SPOOL/'first-install.run'
        with args.file.open('rb') as src,target.open('xb') as dst:
            size=0
            while chunk:=src.read(256*1024):
                size+=len(chunk)
                if size>worker.MAX_INSTALLER:raise InstallError('installer exceeds size limit')
                dst.write(chunk)
        target.chmod(0o600)
        request={key:manifest.get(key) for key in ('version','arch','build')}
        request.update(channel='offline',digest=digest(target))
    elif args.channel=='dev':
        from dev_update import discover
        request=discover(args.run_id,initial=initial)
        if args.commit and request['commit']!=args.commit:raise InstallError('published build does not match notified commit')
        if installed and installed.get('build')=={'channel':'dev','commit':request['commit']}:
            print(json.dumps({'phase':'unchanged','version':request['version'],'run_id':request['run_id']}));return
        if args.automatic:request['automatic']=True
    else:
        request=worker.initial_stable(args.version,arch)
        if installed and installed['version']==request['version'] and installed.get('build',{}).get('channel')=='stable':
            print(json.dumps({'phase':'unchanged','version':request['version']}));return
        if installed and not args.yes:print(json.dumps({'phase':'confirmation_required','version':request['version']}));return
    if initial:request['operation']='install'
    request=queue(request)
    subprocess.run(['systemd-run','--no-block','--collect','--unit=cloudctl-install-'+request['task_id'],'--property=Type=oneshot','--property=TimeoutStartSec=infinity',
                    '--property=UMask=0077','--property=Nice=10','--property=MemoryHigh=256M',
                    '/usr/bin/python3','-I','-B',str(Path(__file__).with_name('update_worker.py'))],check=True)
    if args.no_wait:print(json.dumps({'phase':'queued','task_id':request['task_id']}));return
    deadline=time.monotonic()+2400
    result=worker.SPOOL/'tasks'/(request['task_id']+'.json')
    while time.monotonic()<deadline:
        if result.exists():
            value=json.loads(worker.private_read(result,16384))
            if value['phase'] in ('succeeded','skipped','blocked'):print(json.dumps(value));return
            if value['phase']=='failed':raise InstallError('installation failed; inspect cloudctl system-logs install')
        time.sleep(1)
    raise TimeoutError('installation continues independently; inspect cloudctl update status')
