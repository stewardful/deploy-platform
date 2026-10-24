#!/usr/bin/env python3
"""Pinned dependency extraction and root-private, versioned reuse before activation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import archive

def check_elf(path, arch):
    with open(path, "rb") as handle:
        header = handle.read(20)
    if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", header[18:20])[0] != {"amd64": 62, "arm64": 183}[arch]:
        raise ValueError("expected Linux ELF64 " + arch + ": " + str(path))


def runtime_subset(source, destination, arch, lock):
    if archive.digest(source) != lock["archives"][arch]:
        raise ValueError("official runtime archive checksum mismatch")
    executables = {"bin/" + name for name in ("containerd", "containerd-shim-runc-v2", "ctr", "nerdctl", "runc")}
    cni = {"libexec/cni/" + name for name in ("bridge", "loopback", "host-local", "portmap", "firewall", "tuning")}
    selected = set()
    with tarfile.open(source, "r:gz") as handle:
        count, total = 0, 0
        for member in handle:
            count += 1; total += member.size
            if count > archive.MAX_FILES or total > archive.MAX_BYTES:
                raise ValueError("dependency archive exceeds extraction limits")
            name = member.name.removeprefix("./")
            if name not in executables | cni and not name.startswith("share/doc/") and name not in ("libexec/cni/LICENSE", "libexec/cni/README.md"):
                continue
            name = archive.safe_name(name)
            if not member.isfile():
                if member.isdir():
                    continue
                raise ValueError("selected runtime file is not regular")
            if name in selected:
                raise ValueError("duplicate runtime file")
            selected.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle.extractfile(member) as src, open(target, "xb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o755 if name in executables | cni else 0o644)
    if not (executables | cni).issubset(selected):
        raise ValueError("runtime release missing required files")
    for name in executables | cni:
        check_elf(destination / name, arch)


def proxy_subset(source, destination, arch, lock):
    if archive.digest(source)!=lock['archives'][arch]['sha256']:
        raise ValueError('official Caddy archive checksum mismatch')
    destination.mkdir(parents=True)
    with tarfile.open(source,'r:gz') as handle:
        for name,target in [('caddy',destination/'bin/caddy'),('LICENSE',destination/'LICENSE')]:
            member=handle.getmember(name)
            if not member.isfile(): raise ValueError('proxy archive file must be regular')
            target.parent.mkdir(parents=True,exist_ok=True)
            with handle.extractfile(member) as src,open(target,'xb') as dst: shutil.copyfileobj(src,dst)
            target.chmod(0o755 if name=='caddy' else 0o644)
    check_elf(destination/'bin/caddy',arch)
    (destination/'proxy.lock.json').write_text(json.dumps(lock, indent=2)+'\n')



MAX_DOWNLOAD = 512 * 1024**2
MAX_SECONDS = 900


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    allowed = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
    if (parsed.scheme != 'https' or parsed.hostname not in allowed or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('dependency download must use an approved HTTPS host')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, expected):
    check_url(url)
    deadline = time.monotonic() + MAX_SECONDS
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}), Redirect())
    partial = destination.with_suffix('.part')
    try:
        for attempt in range(3):
            try:
                with client.open(url, timeout=min(30, max(1, deadline-time.monotonic()))) as response, partial.open('wb') as target:
                    length = response.headers.get('Content-Length')
                    if length is not None and (not length.isdigit() or int(length) > MAX_DOWNLOAD):
                        raise ValueError('dependency archive exceeds limit')
                    total = 0
                    while True:
                        if time.monotonic() >= deadline: raise TimeoutError('dependency download deadline exceeded')
                        chunk = response.read1(256*1024)
                        if not chunk: break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD: raise ValueError('dependency archive exceeds limit')
                        target.write(chunk)
                if archive.digest(partial) != expected: raise ValueError('dependency archive checksum mismatch')
                partial.replace(destination)
                return
            except OSError:
                if attempt == 2 or time.monotonic() >= deadline: raise
                time.sleep(min(2**attempt, max(0, deadline-time.monotonic())))
    finally:
        partial.unlink(missing_ok=True)


def inventory(root):
    values = {}
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('linked or special dependency file')
        if path.is_file(): values[path.relative_to(root).as_posix()] = archive.digest(path)
    return values


def cached_valid(entry):
    try:
        if entry.is_symlink() or (entry/'payload').is_symlink() or (entry/'inventory.json').is_symlink(): return False
        value = json.loads((entry/'inventory.json').read_text())
        return bool(value) and inventory(entry/'payload') == value
    except (OSError, ValueError): return False


def materialize(source, *, current=Path('/opt/deploy-platform/current'), cache=Path('/var/cache/deploy-platform/dependencies'), downloader=fetch):
    source, current, cache = Path(source), Path(current), Path(cache)
    manifest = archive.verify(source)
    if manifest['package_kind'] == 'offline': return manifest
    # SOURCE is an extracted, authenticated update payload; no host services are
    # changed until the assembled full inventory passes the existing installer.
    old = None
    try:
        old = archive.verify(current, manifest['arch'])
        if old['package_kind'] != 'offline': old = None
    except (OSError, ValueError): pass
    if cache.is_symlink() or cache.parent.is_symlink(): raise ValueError('linked dependency cache')
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_status = cache.parent.stat()
    if parent_status.st_uid != os.geteuid() or stat.S_IMODE(parent_status.st_mode) & 0o022:
        raise ValueError('dependency cache parent must be owned by the installer and not writable by others')
    status = cache.stat()
    if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o077:
        raise ValueError('dependency cache must be private and owned by the installer')
    for kind, lock in (('runtime', manifest['runtime']), ('proxy', manifest['proxy'])):
        key = hashlib.sha256(json.dumps({'kind': kind, 'arch': manifest['arch'], 'lock': lock}, sort_keys=True).encode()).hexdigest()
        entry = cache / (kind+'-'+key)
        if not cached_valid(entry):
            if entry.exists() or entry.is_symlink():
                if entry.is_symlink(): raise ValueError('linked dependency cache entry')
                shutil.rmtree(entry)
            with tempfile.TemporaryDirectory(prefix='.stage-', dir=cache) as temp:
                stage = Path(temp); payload = stage/'payload'
                if old and old[kind] == lock:
                    shutil.copytree(current/kind, payload)
                    print('Reusing verified installed ' + kind, flush=True)
                else:
                    payload.mkdir()
                    url = lock['archive_url_template'].format(arch=manifest['arch'])
                    expected = lock['archives'][manifest['arch']]
                    if kind == 'proxy': expected = expected['sha256']
                    download = stage/'archive.tar.gz'
                    print('Downloading pinned ' + kind + ' dependency (first use only)', flush=True)
                    downloader(url, download, expected)
                    # Verify even custom/test downloaders before extracting.
                    if archive.digest(download) != expected: raise ValueError('dependency archive checksum mismatch')
                    if kind == 'runtime': runtime_subset(download, payload, manifest['arch'], lock)
                    else:
                        payload.rmdir()
                        proxy_subset(download, payload, manifest['arch'], lock)
                    download.unlink()
                (stage/'inventory.json').write_text(json.dumps(inventory(payload), sort_keys=True)+'\n')
                stage.rename(entry)
                # TemporaryDirectory cleanup tolerates a renamed directory.
        else:
            print('Reusing verified cached ' + kind, flush=True)
        shutil.copytree(entry/'payload', source/kind)
    manifest['package_kind'] = 'offline'
    manifest['files'] = {p.relative_to(source).as_posix(): archive.digest(p) for p in sorted(source.rglob('*'))
                         if p.is_file() and p != source/'manifest.json'}
    (source/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    return archive.verify(source, manifest['arch'])
