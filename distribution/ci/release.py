#!/usr/bin/env python3
"""Idempotent main releases: pin tags to the event SHA, never the default dev branch."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from version import build_metadata


class ReleaseError(ValueError):
    pass


def api(repo, path, body=None, missing=False):
    args = ['gh', 'api', 'repos/' + repo + '/' + path]
    if body is not None:
        args += ['--method', 'POST', '--input', '-']
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        if missing and 'HTTP 404' in result.stderr:
            return None
        raise ReleaseError('GitHub request failed; check workflow permissions and repository access')
    return json.loads(result.stdout)


def tag_commit(repo, tag):
    ref = api(repo, 'git/ref/tags/' + tag, missing=True)
    if ref is None:
        return None
    obj = ref['object']
    for _ in range(8):
        if obj['type'] == 'commit':
            return obj['sha']
        if obj['type'] != 'tag':
            break
        obj = api(repo, 'git/tags/' + obj['sha'])['object']
    raise ReleaseError('unsupported release tag object')


def check(repo, version, commit):
    pinned = tag_commit(repo, 'v' + version)
    if pinned is not None and pinned != commit:
        raise ReleaseError('version is already used by another commit; bump workspace/internal versions and Cargo.lock before merging main')


def publish(repo, version, commit, directory):
    check(repo, version, commit)
    tag = 'v' + version
    assets = []
    for arch in ('amd64', 'arm64'):
        stem = f'deploy-platform-{version}-linux-{arch}'
        for suffix in ('.run', '.tar.gz', '.sha256'):
            path = directory / (stem + suffix)
            if not path.is_file() or path.is_symlink():
                raise ReleaseError('both complete architecture artifacts required')
            assets.append(str(path))
    if tag_commit(repo, tag) is None:
        api(repo, 'git/refs', {'ref': 'refs/tags/' + tag, 'sha': commit})
    # A default branch of dev must never influence release content or tag target.
    if tag_commit(repo, tag) != commit:
        raise ReleaseError('release tag does not point to the event commit')
    release = api(repo, 'releases/tags/' + tag, missing=True)
    if release is not None and not release['draft']:
        names = {a['name'] for a in release.get('assets', [])}
        if not {Path(a).name for a in assets}.issubset(names):
            raise ReleaseError('published release is incomplete; publish a new version')
        print('Release already published for this commit; kept existing assets')
        return
    env = dict(os.environ, GH_REPO=repo)
    if release is None:
        subprocess.run(['gh', 'release', 'create', tag, '--verify-tag', '--draft', '--target', commit,
                        '--title', tag, '--generate-notes'], check=True, env=env)
    subprocess.run(['gh', 'release', 'upload', tag, *assets, '--clobber'], check=True, env=env)
    subprocess.run(['gh', 'release', 'edit', tag, '--draft=false', '--latest'], check=True, env=env)


if __name__ == '__main__':
    try:
        repo = os.environ['GH_REPO']
        commit = os.environ['GITHUB_SHA']
        if os.environ.get('GITHUB_REF') != 'refs/heads/main':
            raise ReleaseError('formal releases require main')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch('[a-f0-9]{40}', commit):
            raise ReleaseError('invalid repository or commit')
        version, _ = build_metadata(Path.cwd(), 'both')
        if sys.argv[1:] == ['check']:
            check(repo, version, commit)
        elif sys.argv[1:] == ['publish']:
            publish(repo, version, commit, Path('dist'))
        else:
            raise ReleaseError('expected check or publish')
    except (ReleaseError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
