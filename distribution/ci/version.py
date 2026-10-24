#!/usr/bin/env python3
"""Stable platform version and a closed set of native build targets."""
import json
import os
from pathlib import Path
import re


def build_metadata(root, arch):
    section = (root / 'Cargo.toml').read_text().split('[workspace.package]', 1)[1].split('[', 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
    if match is None: raise ValueError('workspace version missing')
    version = match.group(1)
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('CI requires a stable workspace version')
    if arch not in ('amd64', 'arm64', 'both'):
        raise ValueError('build architecture must be amd64, arm64 or both')
    targets = [{'runner': 'ubuntu-24.04', 'arch': 'amd64'},
               {'runner': 'ubuntu-24.04-arm', 'arch': 'arm64'}]
    return version, [t for t in targets if arch in ('both', t['arch'])]


if __name__ == '__main__':
    version, targets = build_metadata(Path.cwd(), os.environ.get('BUILD_ARCH', 'both'))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('version=' + version + '\n')
        output.write('targets=' + json.dumps(targets) + '\n')
