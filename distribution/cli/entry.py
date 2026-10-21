"""Standalone cloudctl entry. The same CLI modules ship in platform releases."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import shutil
import sys
import tempfile
import zipfile


def safe_directory(path):
    if path != path.parent:
        safe_directory(path.parent)
    if not path.exists() and not path.is_symlink(): path.mkdir(mode=0o755)
    meta = path.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
        raise ValueError('untrusted bootstrap directory')


def main():
    current = Path('/opt/deploy-platform/current/lib/node_cli.py')
    if current.is_file():
        os.execv('/usr/bin/python3', ['/usr/bin/python3', '-I', '-B', str(current), *sys.argv[1:]])
    source = Path(sys.argv[0]).read_bytes()
    if len(source) > 4 * 1024**2: raise ValueError('oversized cloudctl')
    with zipfile.ZipFile(io.BytesIO(source)) as bundle:
        members = bundle.infolist()
        if len(members) > 100 or sum(i.file_size for i in members) > 4 * 1024**2:
            raise ValueError('oversized CLI archive')
        inventory = json.loads(bundle.read('cli.json'))
        if set(i.filename for i in members) != set(inventory['files']) | {'cli.json'} or len(members) != len(inventory['files']) + 1:
            raise ValueError('CLI inventory differs')
        data = {}
        for name, expected in inventory['files'].items():
            p = Path(name)
            if p.is_absolute() or '..' in p.parts or len(p.parts) > 2:
                raise ValueError('unsafe CLI member')
            data[name] = bundle.read(name)
            if hashlib.sha256(data[name]).hexdigest() != expected: raise ValueError('CLI checksum mismatch')
    help_args = ([], ['--help'], ['help'], ['version'], ['--version'], ['install', '--help'])
    if os.geteuid() != 0 or sys.argv[1:] in help_args:
        if os.geteuid() != 0 and sys.argv[1:] not in help_args:
            raise ValueError('sudo is required before first installation')
        with tempfile.TemporaryDirectory(prefix='cloudctl-help-') as temp:
            root = Path(temp)
            for name, value in data.items():
                p = root/name; p.parent.mkdir(exist_ok=True); p.write_bytes(value)
            (root/'cli.json').write_text(json.dumps(inventory))
            import subprocess
            raise SystemExit(subprocess.call([sys.executable, '-I', '-B', str(root/'lib/node_cli.py'), *sys.argv[1:]]))
    root = Path('/opt/deploy-platform/bootstrap') / hashlib.sha256(source).hexdigest()
    safe_directory(root.parent)
    content = {**data, 'cli.json': json.dumps(inventory).encode(), 'cloudctl': source}
    if not root.exists() and not root.is_symlink():
        staging = Path(tempfile.mkdtemp(prefix='.cli-', dir=root.parent))
        try:
            (staging/'lib').mkdir(mode=0o755)
            for name, value in content.items():
                path = staging/name
                path.write_bytes(value)
                path.chmod(0o755 if name == 'cloudctl' else 0o644)
            staging.chmod(0o755)
            try: staging.rename(root)
            except OSError:
                if not root.is_dir(): raise
        finally:
            if staging.exists(): shutil.rmtree(staging)
    safe_directory(root)
    safe_directory(root/'lib')
    if {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()} != set(content):
        raise ValueError('bootstrap inventory differs')
    for name, value in content.items():
        path = root/name
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022 or path.read_bytes() != value:
            raise ValueError('immutable CLI bootstrap changed')
    os.execv('/usr/bin/python3', ['/usr/bin/python3', '-I', '-B', str(root/'lib/node_cli.py'), *sys.argv[1:]])


try:
    main()
except Exception:
    raise SystemExit('cloudctl could not start; verify its source, checksum and directory permissions') from None
