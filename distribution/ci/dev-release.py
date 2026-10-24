#!/usr/bin/env python3
"""Publish fixed dev builds, then promote the latest successful source without deploying."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'installer/lib'))
from archive import installer_manifest, digest
from dev_manifest import validate
from release import api, tag_commit, ReleaseError
from version import build_metadata


def asset(path):
    if not path.is_file() or path.is_symlink(): raise ReleaseError('missing or linked dev asset')
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': digest(path)}


def inventory(repo, run_id, commit, version, directory):
    manifest = {'schema_version': 1, 'repository': repo, 'run_id': run_id, 'commit': commit,
                'version': version, 'installers': {}, 'cli': asset(directory/'cloudctl')}
    files = [directory/'cloudctl', directory/'cloudctl.sha256']
    for arch in ('amd64', 'arm64'):
        path = directory/f'deploy-platform-{version}-linux-{arch}.run'
        info = installer_manifest(path)
        if (info.get('package_kind'), info.get('version'), info.get('arch'), info.get('build')) != ('platform-update', version, arch, {'channel': 'dev', 'commit': commit}):
            raise ReleaseError('dev installer provenance differs')
        manifest['installers'][arch] = asset(path)
        files += [path, path.with_suffix('.sha256')]
    validate(manifest, repo)
    for item in [manifest['cli'], *manifest['installers'].values()]:
        checksum = directory/('cloudctl.sha256' if item['name'] == 'cloudctl' else item['name'][:-4]+'.sha256')
        expected = item['sha256']+'  '+item['name']
        if checksum.is_symlink() or checksum.read_text().splitlines().count(expected) != 1:
            raise ReleaseError('dev checksum file differs')
    return manifest, files


def gh(*args):
    subprocess.run(['gh', *args], check=True)


def existing_matches(release, files):
    """Reruns may observe a published build, but must never overwrite it."""
    expected = {p.name: asset(p) for p in files}
    actual = {a['name']: a for a in release.get('assets', [])}
    if len(actual) != len(release.get('assets', [])) or set(actual) != set(expected):
        raise ReleaseError('published dev asset inventory differs')
    for name, item in expected.items():
        found = actual[name]
        if found.get('size') != item['size'] or found.get('digest') != 'sha256:'+item['sha256']:
            raise ReleaseError('published dev assets differ; create a new build')


def publish(repo, run_id, commit, version, directory):
    if not re.fullmatch(r'[1-9][0-9]{0,19}', run_id) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ReleaseError('invalid build identity')
    manifest, files = inventory(repo, run_id, commit, version, directory)
    index = directory/'dev.json'
    index.write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n')
    files += [index]
    tag = 'dev-'+run_id
    pinned = tag_commit(repo, tag)
    if pinned is not None and pinned != commit: raise ReleaseError('dev tag already used by another commit')
    if pinned is None: api(repo, 'git/refs', {'ref': 'refs/tags/'+tag, 'sha': commit})
    if tag_commit(repo, tag) != commit: raise ReleaseError('dev tag changed')
    release = api(repo, 'releases/tags/'+tag, missing=True)
    if release is not None and not release['draft']:
        if not release['prerelease']: raise ReleaseError('dev build must be a prerelease')
        existing_matches(release, files)
    else:
        if release is None:
            gh('release', 'create', tag, '--repo', repo, '--verify-tag', '--draft', '--prerelease',
               '--target', commit, '--title', f'Dev build {run_id}', '--notes', f'CI-passed dev build: {commit}. Not a stable release.')
        gh('release', 'upload', tag, '--repo', repo, *map(str, files), '--clobber')
        gh('release', 'edit', tag, '--repo', repo, '--prerelease', '--draft=false', '--latest=false')
    # An older/rerun source may publish its fixed build, but cannot replace latest.
    if api(repo, 'git/ref/heads/dev')['object']['sha'] != commit:
        print('Published fixed dev build; newer dev source exists, latest unchanged'); return
    latest = api(repo, 'releases/tags/dev-latest', missing=True)
    if latest is not None:
        match = re.fullmatch(r'Dev latest \(([1-9][0-9]*)\)', latest.get('name', ''))
        if not match: raise ReleaseError('unmanaged dev-latest release')
        if int(match.group(1)) > int(run_id):
            print('Newer build already promoted; latest unchanged'); return
    else:
        if tag_commit(repo, 'dev-latest') is not None: raise ReleaseError('unmanaged dev-latest tag')
        api(repo, 'git/refs', {'ref': 'refs/tags/dev-latest', 'sha': commit})
        gh('release', 'create', 'dev-latest', '--repo', repo, '--verify-tag', '--draft', '--prerelease',
           '--title', f'Dev latest ({run_id})', '--notes', 'Discovery pointer only; installers use fixed dev build tags.')
    # Reserve the run before uploading: even a partial promotion must not allow an older rerun to replace it.
    gh('release', 'edit', 'dev-latest', '--repo', repo, '--title', f'Dev latest ({run_id})')
    # Upload discovery last. Installers are never downloaded from this mutable release.
    gh('release', 'upload', 'dev-latest', '--repo', repo, str(directory/'cloudctl'), str(directory/'cloudctl.sha256'), '--clobber')
    gh('release', 'upload', 'dev-latest', '--repo', repo, str(index), '--clobber')
    gh('release', 'edit', 'dev-latest', '--repo', repo, '--prerelease', '--draft=false', '--latest=false', '--title', f'Dev latest ({run_id})')


if __name__ == '__main__':
    if os.environ.get('GITHUB_REF') != 'refs/heads/dev' or os.environ.get('GITHUB_EVENT_NAME') not in ('push', 'workflow_dispatch'):
        raise SystemExit('dev publishing requires a trusted dev event')
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or '..' in repo: raise SystemExit('invalid repository')
    version, _ = build_metadata(Path.cwd(), 'both')
    publish(repo, os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_SHA'], version, Path('dist'))
