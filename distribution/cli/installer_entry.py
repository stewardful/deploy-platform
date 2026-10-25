#!/usr/bin/env python3
"""Render and verify the released one-command entry; no platform install logic."""
import argparse
import hashlib
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAX_CLI = 4 * 1024**2


def bounded(path, limit):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError('missing, linked or oversized bootstrap asset')
    data = path.read_bytes()
    if not 0 < len(data) <= limit: raise ValueError('bootstrap asset changed')
    return data


def checksum(path, data):
    expected = hashlib.sha256(data).hexdigest()+'  '+path.name+'\n'
    if bounded(path.with_name(path.name+'.sha256'), 256).decode() != expected:
        raise ValueError('bootstrap checksum differs')


def render(repository, tag, cli):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or '..' in repository:
        raise ValueError('invalid bootstrap repository')
    if not re.fullmatch(r'dev-[1-9][0-9]{0,19}|v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
        raise ValueError('bootstrap requires a fixed build tag')
    if tag.startswith('dev-') and int(tag[4:]) >= 2**64: raise ValueError('invalid bootstrap run ID')
    text = (ROOT/'distribution/cli/install.sh.in').read_text()
    for key, value in {
        'HOST_CHECKS': (ROOT/'distribution/installer/lib/os.sh').read_text().strip(),
        'DEFAULT_CHANNEL': 'dev' if tag.startswith('dev-') else 'stable',
        'CLI_URL': 'https://github.com/'+repository+'/releases/download/'+tag+'/cloudctl',
        'CLI_SHA256': hashlib.sha256(cli).hexdigest(),
        'REPOSITORY': repository,
    }.items(): text = text.replace('@@'+key+'@@', value)
    if '@@' in text: raise ValueError('unrendered bootstrap field')
    return text.encode()


def build(directory, repository, tag):
    directory = Path(directory)
    cli = bounded(directory/'cloudctl', MAX_CLI)
    checksum(directory/'cloudctl', cli)
    path = directory/'install.sh'
    if path.is_symlink() or path.with_name('install.sh.sha256').is_symlink(): raise ValueError('linked bootstrap output')
    content = render(repository, tag, cli)
    path.write_bytes(content); path.chmod(0o755)
    path.with_name('install.sh.sha256').write_text(hashlib.sha256(content).hexdigest()+'  install.sh\n')
    return path


def verify(directory, repository, tag):
    directory = Path(directory)
    cli = bounded(directory/'cloudctl', MAX_CLI)
    checksum(directory/'cloudctl', cli)
    actual = bounded(directory/'install.sh', 32*1024)
    if actual != render(repository, tag, cli): raise ValueError('bootstrap provenance differs')
    checksum(directory/'install.sh', actual)
    return [directory/'install.sh', directory/'install.sh.sha256']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    print(build(args.directory, args.repository, args.tag))
