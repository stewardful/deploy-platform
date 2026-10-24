#!/usr/bin/env python3
"""Opt-in real PostgreSQL/Redis persistence acceptance; never prints secret bytes.

Stops/restarts Agent and removes only the test's labelled containers. Data and
immutable secrets remain intentionally retained for inspection, never pruned.
"""
import json
import secrets
import subprocess
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from smoke import health, request, runtime, wait_job, wait_ready


def run():
    health(15)
    project = 'stateful-' + secrets.token_hex(4)
    password = secrets.token_hex(24)
    marker = 'saved-' + secrets.token_hex(6)
    # Values use only the write-only endpoint, never jobs, environment or CLI argv.
    for name, value in [('db-password', password), ('redis-config', 'requirepass "' + password + '"\n')]:
        reply = request('/api/v1/secrets', {'project': project, 'name': name, 'value': value})
        if 'value' in reply: raise RuntimeError('secret response must be metadata only')
    print('test project=' + project + '; retained volumes/secrets use this project', flush=True)
    for kind in ('postgres', 'redis'):
        pg = kind == 'postgres'
        app = {'project': project, 'name': kind}
        spec = {'schema_version': 1, 'app': app,
                'image': 'docker.io/library/postgres:17-bookworm' if pg else 'docker.io/library/redis:7.4-bookworm',
                'resources': {'memory_mib': 384 if pg else 256, 'cpu_millis': 500, 'pids': 128},
                'user': {'uid': 999, 'gid': 999}, 'network': 'bridge',
                'volumes': [{'name': 'data', 'target': '/var/lib/postgresql/data' if pg else '/data'}],
                'secrets': [{'name': 'db-password' if pg else 'redis-config'}],
                'environment': {'POSTGRES_PASSWORD_FILE': '/run/secrets/db-password', 'POSTGRES_DB': 'app', 'PGDATA': '/var/lib/postgresql/data/pgdata'} if pg else {},
                'args': ['postgres', '-c', 'shared_buffers=64MB', '-c', 'max_connections=30'] if pg else ['redis-server', '/run/secrets/redis-config', '--appendonly', 'yes', '--appendfsync', 'everysec', '--dir', '/data', '--maxmemory', '128mb', '--maxmemory-policy', 'noeviction']}
        instance = None
        def current():
            values = request('/api/v1/apps')
            return next(v.get('instance_id') for v in values if v['app'] == app)
        def command(*args):
            if pg:
                return runtime('exec', instance, 'sh', '-c', 'export PGPASSWORD="$(cat /run/secrets/db-password)"; exec psql -h 127.0.0.1 -U postgres -d app -At -v ON_ERROR_STOP=1 "$@"', 'probe', *args)
            return runtime('exec', instance, 'sh', '-c', 'export REDISCLI_AUTH="$(sed -n \'s/^requirepass "\\(.*\\)"$/\\1/p\' /run/secrets/redis-config)"; exec redis-cli --raw "$@"', 'probe', *args)
        def ready():
            deadline = time.monotonic() + 60
            while True:
                try:
                    if pg:
                        if command('-c', 'SELECT 1;').strip() == '1': return
                    elif command('PING').strip() == 'PONG': return
                except (subprocess.SubprocessError, OSError): pass
                if time.monotonic() >= deadline: raise RuntimeError(kind + ' readiness deadline exceeded')
                time.sleep(1)
        def owned(identity):
            labels = json.loads(runtime('inspect', '--', identity))[0]['Config']['Labels']
            if labels.get('io.deploy-platform.managed-by') != 'rust-agent' or labels.get('io.deploy-platform.project') != project or labels.get('io.deploy-platform.app') != kind:
                raise RuntimeError('refusing to change an unowned test container')
        try:
            job = wait_job(request('/api/v1/apps/apply', {'request_id': project + '-' + kind, 'spec': spec, 'startup_wait_ms': 1000}))
            instance = job['result']['instance_id']
            ready()
            if pg: command('-c', "CREATE TABLE IF NOT EXISTS durable_probe(v text); INSERT INTO durable_probe VALUES ('" + marker + "');")
            else:
                if command('SET', 'durable-probe', marker).strip() != 'OK': raise RuntimeError('Redis write failed')
                # Persist AOF before explicitly simulating loss of the container.
                time.sleep(2)
            # Remove the runtime instance while the owner is stopped, keep data/desired state.
            subprocess.run(['systemctl', 'stop', 'deploy-agent'], check=True)
            try:
                owned(instance); runtime('stop', '--', instance); runtime('rm', '--', instance)
            finally:
                subprocess.run(['systemctl', 'start', 'deploy-agent'], check=True)
            wait_ready(30)
            old = instance
            deadline = time.monotonic() + 90
            while True:
                instance = current()
                if instance and instance != old: break
                if time.monotonic() >= deadline: raise RuntimeError('missing container was not recreated')
                time.sleep(1)
            ready()
            result = command('-c', 'SELECT v FROM durable_probe;') if pg else command('GET', 'durable-probe')
            if marker not in result: raise RuntimeError(kind + ' persistent record missing')
            # Metadata endpoints and runtime inspection must not reveal uploaded bytes.
            snapshots = [request('/api/v1/secrets/' + project), request('/api/v1/apps'), request('/api/v1/jobs/' + str(job['id'])), json.loads(runtime('inspect', '--', instance))]
            if password in json.dumps(snapshots): raise RuntimeError('secret leaked into public metadata/inspect')
            # Ordinary SQLite, WAL and platform logs are audited separately by the test runner.
            print('PASS: ' + kind + ' authenticated write + container recreation + persisted read + secret references', flush=True)
        finally:
            wait_ready(30)
            stop = request('/api/v1/apps/' + project + '/' + kind + '/stop', {'request_id': project + '-' + kind + '-stop'})
            wait_job(stop)
            instance = current()
            if instance:
                owned(instance); runtime('rm', '--', instance)
    print('PASS: stateful applications stopped; durable data and project secrets retained', flush=True)

if __name__ == '__main__': run()
