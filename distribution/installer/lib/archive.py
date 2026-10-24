#!/usr/bin/env python3
"""Safe release extraction/verification; no host changes or subprocesses.

Only directories and regular files are accepted. Every payload file must be
listed in manifest.json, and every listed digest is verified before installation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile

MAX_BYTES = 2 * 1024**3
MAX_FILES = 5000
CURRENT_CLI_FILES = {'lib/node_cli.py', 'lib/first_install.py', 'lib/ci_control.py', 'lib/ci_worker.py', 'lib/ci_state.py', 'lib/delivery.py'}


def require_current(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict) or not CURRENT_CLI_FILES.issubset(manifest['files']):
        raise ValueError('installer lacks the current cloudctl components; rebuild from current source')
    contract(manifest)


class UpgradeError(ValueError):
    """Safe operator-facing message; never include response bodies or credentials."""


def contract(manifest):
    value = manifest.get('compatibility')
    if not isinstance(value, dict):
        raise UpgradeError('release is missing a valid compatibility contract')
    if value.get('schema_version') != 1 or value.get('agent_api') != 'v1' or value.get('paths_revision') != 1:
        raise UpgradeError('unsupported compatibility contract')
    if not isinstance(value.get('state'), dict) or set(value['state']) != {'agent', 'ingress'}:
        raise UpgradeError('incomplete state compatibility contract')
    for rule in value['state'].values():
        if not isinstance(rule, dict) or set(rule) != {'read_min', 'read_max', 'write'} or any(type(n) is not int for n in rule.values()):
            raise UpgradeError('invalid schema contract')
        if not 0 <= rule['read_min'] <= rule['write'] <= rule['read_max']:
            raise UpgradeError('invalid schema range')
    return value


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or ".." in parts or "\\" in name:
        raise ValueError("unsafe archive member")
    return str(PurePosixPath(*parts))


def extract(archive, destination):
    destination = Path(destination)
    if any(destination.iterdir()):
        raise ValueError("extraction directory must be empty")
    with tarfile.open(archive, "r:gz") as handle:
        members = handle.getmembers()
        if len(members) > MAX_FILES or sum(m.size for m in members) > MAX_BYTES:
            raise ValueError("archive exceeds release limits")
        names = set()
        for member in members:
            name = safe_name(member.name)
            if name in names or not (member.isfile() or member.isdir()):
                raise ValueError("duplicate, linked or special archive member")
            names.add(name)
        for member in members:
            path = destination / safe_name(member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with handle.extractfile(member) as source, open(path, "xb") as target:
                shutil.copyfileobj(source, target)
            # Discard ownership, setuid bits and arbitrary archive permissions.
            path.chmod(0o755 if member.mode & 0o111 else 0o644)


def installer_manifest(path):
    """Inspect a checksum-verified .run without executing it or extracting files."""
    with Path(path).open('rb') as source:
        consumed = 0
        while True:
            line = source.readline(1024 * 1024 + 1)
            consumed += len(line)
            if not line or consumed > 1024 * 1024:
                raise ValueError('installer header missing or oversized')
            if line == b'__CLOUD_ARCHIVE__\n': break
        result, count, size = None, 0, 0
        with tarfile.open(fileobj=source, mode='r|gz') as payload:
            for member in payload:
                count += 1
                size += member.size
                if count > MAX_FILES or size > MAX_BYTES:
                    raise ValueError('installer archive exceeds limits')
                if member.name == 'manifest.json':
                    if result is not None or not member.isfile() or member.size > 2 * 1024**2:
                        raise ValueError('invalid installer manifest')
                    result = json.loads(payload.extractfile(member).read())
        if result is None: raise ValueError('installer manifest missing')
        require_current(result)
        return result


def verify(directory, expected_arch=None):
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("arch") not in ("amd64", "arm64"):
        raise ValueError("unsupported release manifest")
    if expected_arch and manifest["arch"] != expected_arch:
        raise ValueError("release architecture does not match this node")
    import re
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", manifest.get("version", "")):
        raise ValueError("invalid release version")
    hashes = manifest.get("files")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("release has no file inventory")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("linked or special release file")
        if path.is_file() and path.relative_to(root).as_posix() != "manifest.json":
            actual.add(path.relative_to(root).as_posix())
    if actual != set(hashes):
        raise ValueError("release inventory mismatch")
    required = {"bin/agentd", "bin/agentctl", "bin/controld", "web/index.html", "runtime/bin/containerd", "runtime/bin/containerd-shim-runc-v2", "runtime/bin/runc", "runtime/bin/nerdctl", "runtime/bin/ctr"}
    required.update({'proxy/bin/caddy', 'proxy/LICENSE', 'proxy/proxy.lock.json', 'systemd/deploy-proxy.service',
                     'lib/upgrade.py', 'lib/upgrade_state.py', 'lib/upgrade_probes.py', 'lib/agent_http.py',
                     'lib/update_worker.py', 'systemd/deploy-update.service', 'systemd/deploy-update.path'})
    require_current(manifest)
    if not all(isinstance(manifest.get(key), dict) for key in ('runtime', 'proxy')):
        raise ValueError('release missing dependency locks')
    required.update("runtime/libexec/cni/" + name for name in ("bridge", "loopback", "host-local", "portmap", "firewall", "tuning"))
    kind = manifest.get('package_kind')
    if kind not in ('offline', 'platform-update'):
        raise ValueError('unsupported package kind')
    if kind == 'platform-update':
        if any(name.startswith(('runtime/', 'proxy/')) for name in actual):
            raise ValueError('platform update must not embed dependency files')
        required = {name for name in required if not name.startswith(('runtime/', 'proxy/'))}
        required.update({'lib/dependencies.py', 'runtime.lock.json'})
        for key in ('runtime', 'proxy'):
            lock = manifest.get(key)
            if not isinstance(lock, dict) or not isinstance(lock.get('archives'), dict):
                raise ValueError('missing dependency lock')
            expected = lock['archives'].get(manifest['arch'])
            if key == 'proxy' and isinstance(expected, dict): expected = expected.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
                raise ValueError('invalid dependency digest')
            if '{arch}' not in lock.get('archive_url_template', ''):
                raise ValueError('invalid dependency URL template')
        if json.loads((root/'runtime.lock.json').read_text()) != manifest['runtime']:
            raise ValueError('runtime lock differs from manifest')
    if not required.issubset(actual):
        raise ValueError("release is missing application or runtime components")
    for name, expected in hashes.items():
        if safe_name(name) != name or not re.fullmatch("[a-f0-9]{64}", expected):
            raise ValueError("invalid release digest entry")
        if digest(root / name) != expected:
            raise ValueError("release file checksum mismatch: " + name)
    return manifest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("extract", "verify"))
    p.add_argument("source")
    p.add_argument("--destination")
    p.add_argument("--arch")
    args = p.parse_args()
    if args.action == "extract":
        extract(args.source, args.destination)
    else:
        print(verify(args.source, args.arch)["version"])


if __name__ == "__main__":
    main()
