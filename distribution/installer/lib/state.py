#!/usr/bin/env python3
"""Owned-file snapshots and install receipts. Does not roll back application DBs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

FILES = [
    "/etc/deploy-platform-proxy/bootstrap.json", "/etc/systemd/system/deploy-proxy.service",
    "/etc/deploy-platform/containerd.toml", "/etc/deploy-platform/nerdctl.toml",
    "/etc/deploy-platform/cni/10-cloud.conflist", "/opt/deploy-platform/nerdctl",
    "/etc/systemd/system/deploy-containerd.service", "/etc/systemd/system/deploy-agent.service", "/etc/systemd/system/deploy-control.service",
    "/etc/sysctl.d/90-deploy-platform.conf", "/etc/modules-load.d/deploy-platform.conf"
]
RECEIPT = "/etc/deploy-platform/install.json"
TRANSACTION = "/var/lib/deploy-platform/install-transaction.json"
UNITS = ("deploy-containerd.service", "deploy-proxy.service", "deploy-agent.service", "deploy-control.service")


def begin(destination, cidr):
    current = Path('/opt/deploy-platform/current')
    value = {"backup": destination, "cidr": cidr,
             "previous": str(current.readlink()) if current.is_symlink() else None,
             "active": [u for u in UNITS if subprocess.run(['systemctl', 'is-active', '--quiet', u]).returncode == 0]}
    path = Path(TRANSACTION)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value))
    temp.chmod(0o600)
    temp.replace(path)


def recover(source):
    path = Path(TRANSACTION)
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError('transaction cannot be a symlink')
    value = json.loads(path.read_text())
    backup = Path(value['backup'])
    previous = value['previous']
    if backup.parent != Path('/var/lib/deploy-platform/backups') or not (backup / 'snapshot.json').is_file():
        raise ValueError('invalid transaction backup')
    if previous and Path(previous).parent != Path('/opt/deploy-platform/releases'):
        raise ValueError('invalid previous release')
    if any(u not in UNITS for u in value['active']):
        raise ValueError('invalid transaction service')
    print('[cloud] recovering interrupted activation; database is never automatically downgraded', flush=True)
    for unit in reversed(UNITS[1:]):
        if subprocess.run(['systemctl', 'cat', unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            subprocess.run(['systemctl', 'stop', unit], check=True)
    if not previous:
        subprocess.run(['systemctl', 'stop', UNITS[0]], check=False)
        subprocess.run(['systemctl', 'disable', *UNITS], check=False)
    if not (backup / 'etc/systemd/system/deploy-proxy.service').exists():
        subprocess.run(['systemctl','disable','deploy-proxy.service'],check=False)
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
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for unit in value['active']:
        subprocess.run(['systemctl', 'reset-failed', unit], check=False)
        subprocess.run(['systemctl', 'start', unit], check=True)
    path.unlink()


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
    temp = Path(RECEIPT + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(RECEIPT)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("check", "snapshot", "restore", "receipt", "begin", "recover", "commit"))
    p.add_argument("--destination")
    p.add_argument("--source")
    p.add_argument("--cidr")
    p.add_argument("--phase", default="ready")
    a = p.parse_args()
    if a.action == "check": assert_owned()
    elif a.action == "snapshot": snapshot(a.destination)
    elif a.action == "restore": restore(a.destination)
    elif a.action == "begin": begin(a.destination, a.cidr)
    elif a.action == "recover": recover(a.source)
    elif a.action == "commit": Path(TRANSACTION).unlink(missing_ok=True)
    else: receipt(a.source, a.cidr, a.phase)
