#!/usr/bin/env python3
"""Upgrade coordination. Owns no business deployment logic and never runs shell probes."""
import argparse
import json
from pathlib import Path
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from agent_http import request, AgentError, SOCKET
from archive import verify as verify_release
import upgrade_state as state
from upgrade_state import UpgradeError
import upgrade_probes as probes
from state import write_record

MARKER = state.STATE / 'upgrade-maintenance'
GATED_SOCKET = SOCKET + '.upgrade'
BASELINE = 'upgrade-baseline.json'


def write_private(path, value):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise UpgradeError('upgrade record already exists; recover the previous transaction')
    write_record(path, value)


def preflight(source, checks='/etc/deploy-platform/upgrade-checks.json'):
    new = json.loads((Path(source) / 'manifest.json').read_text())
    if 'compatibility' not in new:
        raise UpgradeError('candidate is missing an upgrade compatibility contract')
    state.contract(new)
    for binary in ('agentd', 'agentctl'):
        version = subprocess.check_output([str(Path(source) / 'bin' / binary), '--version'], timeout=5).decode().strip().split()
        if not version or version[-1] != new['version']:
            raise UpgradeError('candidate Agent version does not match release manifest')
    receipt = Path('/etc/deploy-platform/install.json')
    if not receipt.exists():
        return
    state.check_token('/etc/deploy-platform/control.token')
    old = verify_release(Path('/opt/deploy-platform/current'))
    if 'proxy' not in old:
        old['proxy'] = json.loads(Path('/opt/deploy-platform/current/proxy/proxy.lock.json').read_text())
    state.check(old, new)
    logical = state.logical_state()
    specs = [json.loads(row[1]) for row in logical['apps'] if row[3] == 'running']
    routes = [{'route': json.loads(row[2])} for row in logical['routes']]
    profile = state.private_json(checks) if Path(checks).exists() else {'schema_version': 1, 'probes': []}
    probes.validate(profile, specs, routes, {json.loads(row[1])["app"]["project"] for row in logical["apps"]})


def drain(seconds):
    # Durable marker makes candidate Agent start read-only before its worker starts.
    write_private(MARKER, {'schema_version': 1})
    try:
        request('/v1/maintenance/enter', 'POST')
        native = True
    except AgentError as error:
        if error.status != 404:
            raise
        # Legacy 0.4.1/0.5.0: remove the public connection entry, not its containers.
        # Established requests finish; all new CLI/Go connections fail closed.
        if Path(GATED_SOCKET).exists():
            raise UpgradeError('stale upgrade socket requires transaction recovery')
        if not stat.S_ISSOCK(Path(SOCKET).lstat().st_mode):
            raise UpgradeError('expected Agent Unix Socket')
        Path(SOCKET).rename(GATED_SOCKET)
        native = False
    deadline, idle_since = time.monotonic() + seconds, None
    while True:
        if native:
            status = request('/v1/maintenance')
            ready = status['draining'] and status['quiescent'] and status['pending_jobs'] == 0 and status['active_writers'] == 0
        else:
            ready = state.pending() == 0
        now = time.monotonic()
        idle_since = (idle_since if idle_since is not None else now) if ready else None
        # Legacy server has a five-second request deadline; include a safety interval.
        if ready and (native or now - idle_since >= 6):
            return
        if now >= deadline:
            raise TimeoutError('deployment drain timed out; upgrade aborted without cancelling jobs')
        time.sleep(0.25)


def agent(path):
    return request(path, socket_path=GATED_SOCKET if Path(GATED_SOCKET).exists() else SOCKET)


def inspect_apps(apps):
    identities = {}
    present = set(subprocess.check_output(['/opt/deploy-platform/nerdctl', 'ps', '--all', '--no-trunc', '--format', '{{.ID}}'], text=True, timeout=15).split())
    for app in apps:
        key = app['app']
        name = key['project'] + '/' + key['name']
        if not app['instance_id'] or app['instance_id'] not in present:
            if app['desired'] == 'running':
                raise UpgradeError('running application has no instance: ' + name)
            continue
        data = json.loads(subprocess.check_output(['/opt/deploy-platform/nerdctl', 'inspect', '--', app['instance_id']], timeout=15))[0]
        labels = data.get('Config', {}).get('Labels', {})
        if labels.get('io.deploy-platform.managed-by') != 'rust-agent' or labels.get('io.deploy-platform.project') != key['project'] or labels.get('io.deploy-platform.app') != key['name'] or labels.get('io.deploy-platform.spec-sha256') != app['fingerprint']:
            raise UpgradeError('application ownership/fingerprint check failed: ' + name)
        running = data.get('State', {}).get('Running', False)
        if running != (app['desired'] == 'running'):
            raise UpgradeError('application running/stopped intent mismatch: ' + name)
        identities[name] = data['Id']
        if app['desired'] == 'running':
            diagnostic = agent('/v1/diagnostics/' + name)
            if diagnostic['state'] != 'running' or diagnostic['health'] == 'unhealthy':
                raise UpgradeError('application diagnostic failed: ' + name)
    return identities



def validate_resource_refs(specs):
    secrets = {}
    for spec in specs:
        key = spec['app']; project = key['project']
        if spec.get('secrets'):
            if project not in secrets:
                secrets[project] = {s['name'] for s in agent('/v1/secrets/' + project)}
            if any(s['name'] not in secrets[project] for s in spec['secrets']):
                raise UpgradeError('application secret reference is missing')
        if spec.get('volumes'):
            volumes = {v['name']: v for v in agent('/v1/volumes/' + project + '/' + key['name'])}
            user = spec.get('user') or {'uid': 0, 'gid': 0}
            for expected in spec['volumes']:
                actual = volumes.get(expected['name'])
                if actual is None or actual['target'] != expected['target'] or actual['uid'] != user['uid'] or actual['gid'] != user['gid']:
                    raise UpgradeError('application volume metadata is missing or inconsistent')


def capture(backup, checks):
    logical = state.logical_state()
    resources = state.resource_identity()
    apps = agent('/v1/apps')
    routes = agent('/v1/ingress')['routes']
    validate_resource_refs([json.loads(row[1]) for row in logical['apps']])
    specs = [json.loads(row[1]) for row in logical['apps'] if row[3] == 'running']
    profile = state.private_json(checks) if Path(checks).exists() else {'schema_version': 1, 'probes': []}
    values = probes.validate(profile, specs, routes, {json.loads(row[1])["app"]["project"] for row in logical["apps"]})
    # Every enabled route is additionally checked after upgrade; explicit probes
    # must cover business dependencies and any stable persisted-data markers.
    running_keys = {tuple(s["app"][k] for k in ("project", "name")) for s in specs}
    for route in routes:
        if route['route']['enabled'] and tuple(route["route"]["app"][k] for k in ("project", "name")) in running_keys:
            if route['proxy_state'] != 'applied' or not route['upstream_ready']:
                raise UpgradeError('existing domain is not healthy before upgrade')
    old = json.loads((Path('/opt/deploy-platform/current') / 'manifest.json').read_text())
    value = {'rollback_contract': state.contract(old), 'logical': logical, 'resources': resources,
             'apps': apps, 'instances': inspect_apps(apps), 'routes': routes,
             'probes': [{'check': check, 'before': probes.run(check)} for check in values]}
    write_private(Path(backup) / BASELINE, value)


def stopped(source, backup):
    value = state.private_json(Path(backup) / BASELINE, max_bytes=8 * 1024**2)
    if state.pending() or json.loads(json.dumps(state.logical_state())) != value['logical'] or state.resource_identity() != value['resources']:
        raise UpgradeError('state changed while stopping Agent; refusing activation')
    state.backup_and_rehearse(source, backup)
    value['target_schemas'] = {n: r['write'] for n, r in state.contract(json.loads((Path(source) / 'manifest.json').read_text()))['state'].items()}
    write_record(Path(backup) / BASELINE, value)


def verify_once(value):
    maintenance = agent('/v1/maintenance')
    if not maintenance.get('draining') or not maintenance.get('quiescent'):
        raise UpgradeError('candidate Agent is not quiescent behind its upgrade gate')
    if 'target_schemas' in value and state.schema_versions() != value['target_schemas']:
        raise UpgradeError('live schema differs from the rehearsed candidate schema')
    if json.loads(json.dumps(state.logical_state())) != value['logical'] or state.resource_identity() != value['resources']:
        raise UpgradeError('persisted applications/routes/resources changed')
    apps = agent('/v1/apps')
    def identities(values):
        return sorted((a['app']['project'], a['app']['name'], a['fingerprint'], a['desired']) for a in values)
    if identities(apps) != identities(value['apps']):
        raise UpgradeError('API application inventory or desired state changed')
    if inspect_apps(apps) != value['instances']:
        raise UpgradeError('application containers were replaced or lost')
    current = agent('/v1/ingress')
    expected = {r['route']['domain']: r['upstream_ready'] for r in value['routes']}
    if {r['route']['domain'] for r in current['routes']} != set(expected):
        raise UpgradeError('API domain inventory changed')
    for route in current['routes']:
        if route['route']['enabled'] and (route['proxy_state'] != 'applied' or route['upstream_ready'] != expected[route['route']['domain']]):
            raise UpgradeError('domain proxy has not restored its upstream')
    for item in value['probes']:
        if probes.run(item['check']) != item['before']:
            raise UpgradeError('business persisted-data probe changed')


def verify(backup, seconds):
    value = state.private_json(Path(backup) / BASELINE, max_bytes=8 * 1024**2)
    deadline = time.monotonic() + seconds
    while True:
        try:
            verify_once(value)
            write_private(Path(backup) / 'upgrade-report.json', {'schema_version': 1, 'status': 'passed', 'applications': len(value['apps']), 'business_probes': len(value['probes']), 'checked_at': int(time.time())})
            print('PASS: preserved apps, container identities, intent, routes, secrets/volumes and configured business probes')
            return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError) as error:
            failure = Path(backup) / 'upgrade-failure.json'
            if not failure.exists():
                write_record(failure, {'schema_version': 1, 'phase': 'verify', 'error_type': type(error).__name__,
                                       'reason': str(error) if isinstance(error, UpgradeError) else 'business/proxy verification failed'})
            if time.monotonic() >= deadline:
                # Never print response bodies, tokens or secret checksums.
                raise RuntimeError('post-upgrade project verification failed; see private upgrade-baseline.json and upgrade-failure.json') from None
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('preflight', 'drain', 'capture', 'stopped', 'verify', 'resume'))
    parser.add_argument('--source')
    parser.add_argument('--backup')
    parser.add_argument('--checks-file', default='/etc/deploy-platform/upgrade-checks.json')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 3600:
        parser.error('timeout must be 10..3600 seconds')
    if args.action == 'preflight': preflight(args.source, args.checks_file)
    elif args.action == 'drain': drain(args.timeout)
    elif args.action == 'capture': capture(args.backup, args.checks_file)
    elif args.action == 'stopped': stopped(args.source, args.backup)
    elif args.action == 'verify': verify(args.backup, args.timeout)
    else: request('/v1/maintenance/exit', 'POST')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Profiles and backend exceptions can contain private response data.
        print('upgrade guard failed (' + type(error).__name__ + '); inspect the installer recovery result before retrying', file=sys.stderr)
        if isinstance(error, UpgradeError):
            print(str(error), file=sys.stderr)
        sys.exit(1)
