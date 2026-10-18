#!/usr/bin/env python3
"""Owned-file snapshots and install receipts. Does not roll back application DBs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import subprocess

FILES = [
    "/etc/deploy-platform-proxy/bootstrap.json", "/etc/systemd/system/deploy-proxy.service",
    "/etc/deploy-platform/containerd.toml", "/etc/deploy-platform/nerdctl.toml",
    "/etc/deploy-platform/cni/10-cloud.conflist", "/opt/deploy-platform/nerdctl",
    "/etc/systemd/system/deploy-containerd.service", "/etc/systemd/system/deploy-agent.service", "/etc/systemd/system/deploy-control.service",
    "/etc/systemd/system/deploy-update.service", "/etc/systemd/system/deploy-update.path",
    "/etc/sysctl.d/90-deploy-platform.conf", "/etc/modules-load.d/deploy-platform.conf"
]
RECEIPT = "/etc/deploy-platform/install.json"
TRANSACTION = "/var/lib/deploy-platform/install-transaction.json"
BACKUP_ROOT = Path("/var/lib/deploy-platform/backups")
UNITS = ("deploy-containerd.service", "deploy-proxy.service", "deploy-agent.service", "deploy-control.service")



def write_record(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def begin(destination, cidr):
    current = Path('/opt/deploy-platform/current')
    value = {"backup": destination, "cidr": cidr,
             "previous": str(current.readlink()) if current.is_symlink() else None,
             "stage": "draining" if current.is_symlink() else "activating",
             "active": [u for u in UNITS if subprocess.run(['systemctl', 'is-active', '--quiet', u]).returncode == 0]}
    path = Path(TRANSACTION)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_record(path, value)


def recover(source):
    path = Path(TRANSACTION)
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError('transaction cannot be a symlink')
    value = json.loads(path.read_text())
    backup = Path(value['backup'])
    previous = value['previous']
    if backup.parent != BACKUP_ROOT or not (backup / 'snapshot.json').is_file():
        raise ValueError('invalid transaction backup')
    if previous and Path(previous).parent != Path('/opt/deploy-platform/releases'):
        raise ValueError('invalid previous release')
    if any(u not in UNITS for u in value['active']):
        raise ValueError('invalid transaction service')
    if value.get('stage') == 'draining':
        # No services/files have been changed yet. Never cancel an accepted job
        # merely because draining or a pre-activation business check timed out.
        from agent_http import request, AgentError
        gated = Path('/run/deploy-platform/agent.sock.upgrade')
        original = Path('/run/deploy-platform/agent.sock')
        if gated.exists():
            if original.exists() or not stat.S_ISSOCK(gated.lstat().st_mode):
                raise ValueError('cannot restore legacy socket safely')
            gated.rename(original)
        else:
            try: request('/v1/maintenance/exit', 'POST')
            except AgentError as error:
                if error.status != 404: raise
        cleanup_upgrade()
        path.unlink()
        print('[cloud] drain aborted; old Agent resumed without cancelling deployments', flush=True)
        return
    print('[cloud] recovering interrupted activation; database is never automatically downgraded', flush=True)
    for unit in reversed(UNITS[1:]):
        if subprocess.run(['systemctl', 'cat', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            subprocess.run(['systemctl', 'stop', unit], check=True)
    if not previous:
        subprocess.run(['systemctl', 'stop', UNITS[0]], check=False)
        subprocess.run(['systemctl', 'disable', *UNITS], check=False)
    if not (backup / 'etc/systemd/system/deploy-proxy.service').exists():
        subprocess.run(['systemctl','disable','deploy-proxy.service'],check=False)
    # A schema outside the old reader range must never be auto-downgraded.
    baseline = backup / 'upgrade-baseline.json'
    if baseline.exists():
        import upgrade_state
        policy = upgrade_state.private_json(baseline, max_bytes=8 * 1024**2)['rollback_contract']
        if not upgrade_state.accepts(policy, upgrade_state.schema_versions()):
            raise ValueError('rollback blocked: live schema is not readable by the old Agent; preserve backups and plan recovery')
    cleanup_upgrade()
    restore(str(backup))
    current = Path('/opt/deploy-platform/current')
    for name in ('current.next', 'current.rollback'):
        temporary = current.with_name(name)
        if temporary.is_symlink(): temporary.unlink()
    if previous:
        temporary = current.with_name('current.rollback')
        temporary.symlink_to(previous)
        temporary.replace(current)
    else:
        if current.is_symlink(): current.unlink()
        Path(RECEIPT).parent.mkdir(parents=True, exist_ok=True)
        receipt(source, value['cidr'], 'failed')
    # A failed first installation of update support must leave no enabled watcher.
    if not (backup / 'etc/systemd/system/deploy-update.path').exists():
        subprocess.run(['systemctl', 'disable', '--now', 'deploy-update.path'], check=False)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for unit in value['active']:
        subprocess.run(['systemctl', 'reset-failed', unit], check=False)
        subprocess.run(['systemctl', 'start', unit], check=True)
    path.unlink()


def activate_transaction():
    path = Path(TRANSACTION)
    value = json.loads(path.read_text())
    value['stage'] = 'activating'
    write_record(path, value)


def cleanup_upgrade():
    marker = Path('/var/lib/deploy-platform/upgrade-maintenance')
    if marker.is_symlink():
        raise ValueError('linked upgrade marker')
    marker.unlink(missing_ok=True)
    socket = Path('/run/deploy-platform/agent.sock.upgrade')
    if socket.exists():
        if not stat.S_ISSOCK(socket.lstat().st_mode):
            raise ValueError('unexpected upgrade socket file')
        socket.unlink()


def commit():
    Path(TRANSACTION).unlink(missing_ok=True)
    cleanup_upgrade()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def assert_owned():
    path = Path(RECEIPT)
    if path.is_symlink():
        raise ValueError("install receipt cannot be a symlink")
    if not path.exists():
        for name in FILES:
            if Path(name).exists() or Path(name).is_symlink():
                raise ValueError("unmanaged installation path exists: " + name)
        return
    receipt = json.loads(path.read_text())
    if receipt.get("schema_version") != 1 or receipt.get("owner") != "deploy-platform":
        raise ValueError("unrecognized installation receipt")
    for name in FILES:
        if name not in receipt.get("managed_files", {}) and (Path(name).exists() or Path(name).is_symlink()):
            raise ValueError("unmanaged new installation path exists: " + name)
    for name, expected in receipt.get("managed_files", {}).items():
        if name not in FILES or Path(name).is_symlink() or not Path(name).is_file() or digest(name) != expected:
            raise ValueError("managed file changed; preserve/reconcile local changes first: " + name)


def snapshot(destination):
    root = Path(destination)
    existed = []
    for name in FILES + [RECEIPT]:
        p = Path(name)
        if p.exists():
            if p.is_symlink() or not p.is_file():
                raise ValueError("refusing linked/special managed path")
            target = root / name.removeprefix("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            existed.append(name)
    (root / "snapshot.json").write_text(json.dumps(existed))


def restore(destination):
    root = Path(destination)
    existed = json.loads((root / "snapshot.json").read_text())
    for name in FILES + [RECEIPT]:
        target = Path(name)
        if name in existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name.removeprefix("/"), target)
        elif target.exists():
            target.unlink()


def receipt(source, cidr, phase):
    manifest = json.loads((Path(source) / "manifest.json").read_text())
    value = {"schema_version": 1, "owner": "deploy-platform", "version": manifest["version"], "arch": manifest["arch"],
             "manifest_sha256": digest(Path(source) / "manifest.json"), "runtime": manifest["runtime"], "bridge_cidr": cidr, "phase": phase,
             "managed_files": {name: digest(name) for name in FILES if Path(name).is_file()}}
    write_record(RECEIPT, value)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("check", "snapshot", "restore", "receipt", "begin", "recover", "commit", "activate"))
    p.add_argument("--destination")
    p.add_argument("--source")
    p.add_argument("--cidr")
    p.add_argument("--phase", default="ready")
    a = p.parse_args()
    if a.action == "check": assert_owned()
    elif a.action == "snapshot": snapshot(a.destination)
    elif a.action == "restore": restore(a.destination)
    elif a.action == "begin": begin(a.destination, a.cidr)
    elif a.action == "activate": activate_transaction()
    elif a.action == "recover": recover(a.source)
    elif a.action == "commit": commit()
    else: receipt(a.source, a.cidr, a.phase)
