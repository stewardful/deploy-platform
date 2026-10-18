#!/usr/bin/env python3
"""Opt-in acceptance on a disposable node with a local ACME test CA.

Requires /run/cloud-ingress-isolated-test, an existing four-service test project,
test-domain hosts entries, and a local trusted ACME CA. Never run on a user node.
No private key is exported or printed. All service changes are confined to the
explicit test node; applications/data and certificate storage are retained.
"""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
sys.path.insert(0, '/opt/deploy-platform/current')
from smoke import request, runtime, wait_job, wait_ready


def system(*args):
    subprocess.run(['systemctl', *args], check=True)


def until(check, message, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if check(): return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(message)


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect('/run/deploy-platform-proxy/admin.sock')


def run(project):
    if not Path('/run/cloud-ingress-isolated-test').is_file() or not project.startswith('stack-'):
        raise RuntimeError('only an explicitly marked disposable test node/project is allowed')
    front = 'frontend.' + project + '.example.com'
    backend = 'api.' + project + '.example.com'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    def read(domain=front):
        with opener.open('https://' + domain + '/api/visits', timeout=5) as response:
            return json.loads(response.read(4096))
    def working():
        data = read()
        return data['postgres'] >= 1 and str(data['postgres']) == data['redis']
    def rows(): return request('/api/v1/ingress')['routes']
    def row(domain): return next(r for r in rows() if r['route']['domain'] == domain)
    def intent(name, action):
        wait_job(request('/api/v1/apps/' + project + '/' + name + '/' + action,
                         {'request_id': project + '-https-' + action + '-' + str(time.time_ns())}))
    # Accelerate a real ACME renewal without changing any production defaults.
    cert = next(Path('/var/lib/deploy-platform-proxy/caddy/certificates').rglob(front + '.crt'))
    before = hashlib.sha256(cert.read_bytes()).hexdigest()
    config_connection = UnixHTTP('localhost')
    config_connection.request('GET', '/config/')
    config = json.loads(config_connection.getresponse().read())
    config_connection.close()
    config['apps']['tls']['automation']['renew_interval'] = 5_000_000_000
    for policy in config['apps']['tls']['automation']['policies']:
        policy['renewal_window_ratio'] = 0.99
    test_config = Path('/etc/deploy-platform-proxy/cloud-renew-test.json')
    test_config.write_text(json.dumps(config)); test_config.chmod(0o644)
    dropin = Path('/etc/systemd/system/deploy-proxy.service.d/cloud-renew-test.conf')
    dropin.parent.mkdir(exist_ok=True)
    if dropin.exists(): raise RuntimeError('refusing to replace an existing test override')
    dropin.write_text('[Service]\nExecStart=\nExecStart=/opt/deploy-platform/current/proxy/bin/caddy run --config ' + str(test_config) + '\n')
    system('stop', 'deploy-agent')
    try:
        system('daemon-reload'); system('restart', 'deploy-proxy')
        until(lambda: hashlib.sha256(cert.read_bytes()).hexdigest() != before and working(), 'ACME renewal did not update the served certificate', 120)
        print('PASS: real ACME automatic renewal, fresh valid certificate served; production renewal defaults unchanged', flush=True)
    finally:
        dropin.unlink(missing_ok=True); test_config.unlink(missing_ok=True)
        system('daemon-reload'); system('restart', 'deploy-proxy'); system('start', 'deploy-agent')
        wait_ready(30)
    until(working, 'proxy bootstrap did not recover routes after restart')
    print('PASS: proxy-only restart restores desired routes and retained certificate storage', flush=True)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open('http://' + front, timeout=5)
    except urllib.error.HTTPError as error:
        if error.code not in (301, 308) or not error.headers['Location'].startswith('https://' + front): raise
    else: raise RuntimeError('HTTP must redirect to HTTPS')
    connection = http.client.HTTPSConnection(front, context=ssl.create_default_context(), timeout=5)
    connection.request('GET', '/api/visits', headers={'Host': backend})
    if connection.getresponse().status != 421: raise RuntimeError('SNI/Host mismatch was accepted')
    connection.close()
    try:
        request('/api/v1/ingress', {'route': row(front)['route'], 'expected_revision': 0})
    except urllib.error.HTTPError as error:
        if error.code != 409: raise
    else: raise RuntimeError('stale revision overwrote a domain binding')
    print('PASS: HTTP redirect, strict SNI/Host match and stale-edit rejection', flush=True)
    intent('frontend', 'stop')
    try:
        until(lambda: not row(front)['upstream_ready'], 'stopped upstream was not removed')
        try: read()
        except urllib.error.HTTPError as error:
            if error.code != 503: raise
        else: raise RuntimeError('stopped app must return HTTP 503')
    finally: intent('frontend', 'start')
    until(working, 'started application did not recover HTTPS')
    print('PASS: application stop/start maps to HTTPS 503/recovery without changing certificate', flush=True)
    app = {'project': project, 'name': 'backend'}
    old = next(v['instance_id'] for v in request('/api/v1/apps') if v['app'] == app)
    labels = json.loads(runtime('inspect', '--', old))[0]['Config']['Labels']
    if labels.get('io.deploy-platform.managed-by') != 'rust-agent' or labels.get('io.deploy-platform.project') != project:
        raise RuntimeError('unowned runtime test instance')
    system('stop', 'deploy-agent')
    try: runtime('stop', '--', old); runtime('rm', '--', old)
    finally: system('start', 'deploy-agent')
    wait_ready(30)
    until(lambda: next(v['instance_id'] for v in request('/api/v1/apps') if v['app'] == app) != old, 'backend not recreated', 90)
    until(lambda: read(backend)['postgres'] >= 1 and working(), 'HTTPS upstream did not recover after backend recreation', 90)
    print('PASS: Agent restart, backend recreation, frontend/backend HTTPS and retained database record', flush=True)
    for domain in (front, backend):
        status = request('/api/v1/ingress/certificate/' + domain)
        if status['state'] != 'valid' or not status['trusted']: raise RuntimeError('served certificate not verified')
    print('PASS: complete HTTPS runtime acceptance', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--project', required=True)
    run(parser.parse_args().project)
