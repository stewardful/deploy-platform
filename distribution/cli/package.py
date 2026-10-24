#!/usr/bin/env python3
"""Build the small, architecture-independent cloudctl executable from this repository."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def build(output):
    version = re.search(r'\[workspace.package\]\s*version = "([^"]+)"', (ROOT/'Cargo.toml').read_text()).group(1)
    files = {'__main__.py': (ROOT/'distribution/cli/entry.py').read_bytes()}
    files.update({'lib/'+p.name: p.read_bytes() for p in sorted((ROOT/'distribution/installer/lib').glob('*.py'))})
    files['lib/os.sh'] = (ROOT/'distribution/installer/lib/os.sh').read_bytes()
    files['smoke.py'] = (ROOT/'distribution/installer/smoke.py').read_bytes()
    files['cli.json'] = json.dumps({'version': version, 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}, sort_keys=True).encode()
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    path = output/'cloudctl'
    path.write_bytes(b'#!/usr/bin/python3 -I\n')
    with zipfile.ZipFile(path, 'a', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, value)
    path.chmod(0o755)
    (output/'cloudctl.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  cloudctl\n')
    return path


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT/'target/cli')
    print(build(p.parse_args().output))
