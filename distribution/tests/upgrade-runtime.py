#!/usr/bin/env python3
"""Requires a freshly prepared, explicitly marked disposable Ubuntu node.

Never run on a live server. Installs local packages, uses a local ACME CA,
and injects a backend outage during activation to test rollback.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode = True


def system(*args):
    return subprocess.run(args, check=True)


def until(check, message, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if check(): return
        except (OSError, ValueError, subprocess.SubprocessError): pass
        time.sleep(0.5)
    raise RuntimeError(message)


def main():
    if not Path('/run/cloud-upgrade-isolated-test').is_file() or Path('/etc/deploy-platform/install.json').exists():
        raise RuntimeError('requires a fresh, explicitly marked disposable node')
    system('bash', '/var/tmp/old.run', '--skip-apt')
    sys.path.insert(0, '/opt/deploy-platform/current')
    from smoke import request, runtime, wait_ready
    runtime('load', '-i', '/var/tmp/demo.tar')
    system('cloudctl', 'fullstack-smoke', '--image', 'cloud-fullstack-demo:upgrade-check', '--pull-policy', 'never', '--keep-running')
    apps = request('/api/v1/apps')
    project = next(a['app']['project'] for a in apps if a['app']['name'] == 'frontend' and a['desired'] == 'running')
    if not re.fullmatch(r'stack-[0-9a-f]+', project): raise RuntimeError('unexpected test project')
    domain = 'frontend.' + project + '.example.com'
    with Path('/etc/hosts').open('a') as hosts: hosts.write('\n127.0.0.1 ' + domain + '\n')
    # Local Caddy CA provides a real ACME HTTP-01 flow without a public domain.
    ca = {
        'admin': {'listen': 'unix//run/cloud-upgrade-ca/admin.sock', 'config': {'persist': False}},
        'storage': {'module': 'file_system', 'root': '/var/tmp/cloud-upgrade-ca'},
        'apps': {
            'pki': {'certificate_authorities': {'cloud': {'name': 'Upgrade isolated CA', 'install_trust': False}}},
            'http': {'servers': {'ca': {'listen': ['127.0.0.1:14000'], 'routes': [{'handle': [{'handler': 'acme_server', 'ca': 'cloud', 'lifetime': '2h', 'challenges': ['http-01']}]}], 'tls_connection_policies': [{}], 'automatic_https': {'disable_redirects': True}}}},
            'tls': {'certificates': {'automate': ['localhost']}, 'automation': {'policies': [{'subjects': ['localhost'], 'issuers': [{'module': 'internal', 'ca': 'cloud'}]}]}}
        }
    }
    Path('/run/cloud-upgrade-ca').mkdir()
    Path('/var/tmp/cloud-upgrade-ca.json').write_text(json.dumps(ca))
    system('systemd-run', '--unit', 'cloud-upgrade-ca', '/opt/deploy-platform/current/proxy/bin/caddy', 'run', '--config', '/var/tmp/cloud-upgrade-ca.json')
    root = Path('/var/tmp/cloud-upgrade-ca/pki/authorities/cloud/root.crt')
    until(root.exists, 'local CA did not initialize')
    shutil.copyfile(root, '/usr/local/share/ca-certificates/cloud-upgrade-isolated.crt')
    system('update-ca-certificates')
    dropin = Path('/etc/systemd/system/deploy-agent.service.d/upgrade-ca.conf')
    dropin.parent.mkdir(exist_ok=True)
    start = next(line for line in Path('/etc/systemd/system/deploy-agent.service').read_text().splitlines() if line.startswith('ExecStart='))
    dropin.write_text('[Service]\nExecStart=\n' + start + ' --ingress-acme-ca https://localhost:14000/acme/cloud/directory\n')
    system('systemctl', 'daemon-reload'); system('systemctl', 'restart', 'deploy-agent'); wait_ready(30)
    request('/api/v1/ingress', {'route': {'domain': domain, 'app': {'project': project, 'name': 'frontend'}, 'port': 8080, 'enabled': True, 'tls': 'production'}, 'expected_revision': 0})
    def read():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open('https://' + domain + '/api/upgrade-check', timeout=5) as response:
            return json.loads(response.read(4096))
    until(lambda: read()['postgres'] >= 1, 'initial HTTPS data chain failed', 120)
    before = read()
    token = hashlib.sha256(Path('/etc/deploy-platform/control.token').read_bytes()).digest()
    profile = {'schema_version': 1, 'probes': [{'project': project, 'url': 'https://' + domain + '/api/upgrade-check', 'json_equals': {'service': 'backend'}, 'unchanged_json': ['postgres', 'redis']}]}
    current = Path('/opt/deploy-platform/current').readlink()
    # Missing coverage must fail before stopping an existing service.
    pid = subprocess.check_output(['systemctl', 'show', 'deploy-agent', '--property=MainPID', '--value'])
    failed = subprocess.run(['bash', '/var/tmp/new.run', '--skip-apt', '--upgrade'])
    if failed.returncode == 0 or current != Path('/opt/deploy-platform/current').readlink(): raise RuntimeError('missing profile did not block activation')
    if pid != subprocess.check_output(['systemctl', 'show', 'deploy-agent', '--property=MainPID', '--value']): raise RuntimeError('preflight stopped the Agent')
    print('PASS: missing business/HTTPS probe coverage rejected before service interruption', flush=True)
    checks = Path('/etc/deploy-platform/upgrade-checks.json')
    checks.write_text(json.dumps(profile)); checks.chmod(0o600)
    if os.environ.get('CLOUD_TEST_WEB_UPDATE') == '1':
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location('delivery', '/var/tmp/update-delivery.py')
        delivery = module_from_spec(spec); spec.loader.exec_module(delivery)
        delivery.setup(request)
    else:
        system('bash', '/var/tmp/new.run', '--skip-apt', '--upgrade', '--drain-seconds', '30', '--verify-seconds', '40')
    until(lambda: read() == before, 'upgraded HTTPS/database data changed')
    if token != hashlib.sha256(Path('/etc/deploy-platform/control.token').read_bytes()).digest(): raise RuntimeError('token changed')
    if Path('/var/lib/deploy-platform/install-transaction.json').exists() or Path('/var/lib/deploy-platform/upgrade-maintenance').exists(): raise RuntimeError('upgrade transaction left open')
    system('cloudctl', 'maintenance', 'status')
    print('PASS: upgrade preserves containers, HTTPS, PostgreSQL/Redis values, volumes, secrets and token', flush=True)
    # Inject one backend stop after candidate startup, while recovery is paused.
    backend = next(a['instance_id'] for a in request('/api/v1/apps') if a['app'] == {'project': project, 'name': 'backend'})
    helper = Path('/etc/deploy-platform/upgrade-failure.py')
    helper.write_text("""import json,subprocess
from pathlib import Path
flag=Path('/run/upgrade-inject-once')
if flag.exists() and json.loads(Path('/etc/deploy-platform/install.json').read_text())['phase']=='activating':
    flag.unlink()
    subprocess.run(['/opt/deploy-platform/nerdctl','stop','--',""" + repr(backend) + """],check=True)
""")
    inject = Path('/etc/systemd/system/deploy-control.service.d/upgrade-failure.conf')
    inject.parent.mkdir(exist_ok=True); inject.write_text('[Service]\nExecStartPost=+/usr/bin/python3 /etc/deploy-platform/upgrade-failure.py\n')
    Path('/run/upgrade-inject-once').touch()
    system('systemctl', 'daemon-reload')
    current = Path('/opt/deploy-platform/current').readlink()
    failed = subprocess.run(['bash', '/var/tmp/new.run', '--skip-apt', '--drain-seconds', '30', '--verify-seconds', '10'])
    if failed.returncode == 0: raise RuntimeError('business outage was accepted as successful upgrade')
    if current != Path('/opt/deploy-platform/current').readlink(): raise RuntimeError('rollback did not restore the previous release')
    if Path('/var/lib/deploy-platform/install-transaction.json').exists(): raise RuntimeError('rollback did not complete')
    until(lambda: read() == before, 'old release recovery did not restore business data')
    inject.unlink(); system('systemctl', 'daemon-reload')
    print('PASS: post-activation dependency failure blocks commit, restores old release and resumes projects/data', flush=True)
    # Normal same-release installation now exercises the native maintenance API.
    system('bash', '/var/tmp/new.run', '--skip-apt', '--drain-seconds', '30', '--verify-seconds', '40')
    if read() != before: raise RuntimeError('native maintenance reinstall changed data')
    print('PASS: native drain/reinstall, trusted HTTPS and unchanged persisted records', flush=True)


if __name__ == '__main__': main()
