#!/usr/bin/env python3
"""Read-only installed CLI contract checks for disposable Ubuntu test nodes."""
import json
from pathlib import Path
import subprocess


def call(*args, code=0):
    result = subprocess.run(['/usr/local/bin/cloudctl', *args], capture_output=True, text=True, timeout=60)
    if result.returncode != code:
        raise RuntimeError('CLI contract failed: '+repr(args)+'; '+result.stderr)
    return result.stdout


def main():
    manifest = json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())
    version = json.loads(call('version'))
    assert version['version'] == manifest['version'] and version['arch'] == manifest['arch']
    assert version['components']['runtime']['containerd'] == manifest['runtime']['containerd']
    assert 'cloudctl deploy' in call('deploy', '--help')
    assert 'No live-state comparison' in call('preview', '--help')
    call('validate', '-f', '/opt/deploy-platform/current/examples/http-service.toml')
    preview = json.loads(call('preview', '-f', '/opt/deploy-platform/current/examples/http-service.toml'))
    assert preview['image'] and 'environment_keys' in preview
    assert isinstance(json.loads(call('apps')), list)
    routes = json.loads(call('route', 'list'))
    assert isinstance(routes['routes'], list) and isinstance(routes['revision'], int)
    call('agent-health')
    call('status')
    assert 'execution' in json.loads(call('update', 'status'))
    call('upgrade', '--help'); call('upgrade-dev', '--help'); call('test', 'fullstack', '--help')
    for args in (['logs'], ['logs', 'agent'], ['plan'], ['apply'], ['version', 'ignored'], ['delivery', 'status']):
        call(*args, code=2)
    print('PASS: installed cloudctl version, preview, Agent queries, health, update status, help and removed commands')


if __name__ == '__main__': main()
