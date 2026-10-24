#!/usr/bin/env python3
"""Isolated SSE and trusted HTTPS Release fixture; never an override in production code."""
import hashlib
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.request

ROOT=Path('/var/tmp')
MARKER=Path('/run/cloud-notification-isolated-test')
REPO='stewardful/deploy-platform'


def metadata(run):
    manifest=json.loads((ROOT/'new-manifest.json').read_text())
    data=(ROOT/'new.run').read_bytes()
    commit=manifest['build']['commit'] if run=='42' else 'c'*40
    item={'name':'deploy-platform-'+manifest['version']+'-linux-'+manifest['arch']+'.run','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    return {'schema_version':1,'repository':REPO,'run_id':run,'commit':commit,'version':manifest['version'],
            'cli':{'name':'cloudctl','size':1,'sha256':'b'*64},'installers':{arch:dict(item,name='deploy-platform-'+manifest['version']+'-linux-'+arch+'.run') for arch in ('amd64','arm64')}}


def stable_version():
    version=json.loads((ROOT/'new-manifest.json').read_text())['version']
    major,minor,patch=map(int,version.split('.'))
    return f'{major}.{minor}.{patch+1}'


def current_run(): return (ROOT/'notification-run').read_text().strip()


class Releases(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        if self.headers.get('Authorization'): self.send_error(403);return
        run=current_run()
        if self.path.endswith('/dev.json'):
            if '/dev-42/' in self.path: run='42'
            if '/dev-43/' in self.path: run='43'
            data=json.dumps(metadata(run)).encode()
        elif self.path.endswith('.run'):
            number='43' if '/dev-43/' in self.path else '42'
            counter=ROOT/('downloads-'+number)
            counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1')
            data=(ROOT/'new.run').read_bytes()
        elif self.path.endswith('/releases/latest') or '/releases/tags/' in self.path:
            # Deliberately advertise a stable version; the consumer must only prompt.
            m=metadata(run);item=m['installers'][json.loads((ROOT/'new-manifest.json').read_text())['arch']]
            data=json.dumps({'tag_name':'v'+stable_version(),'draft':False,'prerelease':False,'assets':[{'id':123,'name':'deploy-platform-'+stable_version()+'-linux-'+json.loads((ROOT/'new-manifest.json').read_text())['arch']+'.run','size':item['size'],'digest':'sha256:'+item['sha256']}]}).encode()
        else: self.send_error(404);return
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


class Events(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        if self.path!='/v1/events':self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        previous=''
        try:
            while True:
                run=current_run()
                if run!=previous:
                    notice={'repository':REPO,'channel':'dev','tag':'dev-'+run,'release_id':int(run),'published_at':'2026-10-25T00:00:00Z'}
                    stable=dict(notice,channel='stable',tag='v'+stable_version())
                    value={'revision':int(run),'latest':{'dev':notice,'stable':stable}}
                    self.wfile.write(('event: snapshot\ndata: '+json.dumps(value)+'\n\n').encode());previous=run
                else:self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush();time.sleep(1)
        except (OSError,ConnectionError):pass


def wait(predicate,label,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if predicate():return
        except (OSError,ValueError,KeyError):pass
        time.sleep(0.3)
    raise RuntimeError('deadline: '+label)


def setup():
    sys.path.insert(0,'/opt/deploy-platform/current/lib')
    from archive import installer_manifest
    manifest=installer_manifest(ROOT/'new.run')
    (ROOT/'new-manifest.json').write_text(json.dumps(manifest));(ROOT/'notification-run').write_text('42')
    subprocess.run(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-days','1','-subj','/CN=github.com',
      '-addext','subjectAltName=DNS:github.com,DNS:api.github.com','-keyout',str(ROOT/'notification-test.key'),
      '-out','/usr/local/share/ca-certificates/notification-test.crt'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['update-ca-certificates'],check=True,stdout=subprocess.DEVNULL)
    with Path('/etc/hosts').open('a') as out:out.write('\n127.0.0.2 github.com api.github.com\n')
    subprocess.run(['iptables','-t','nat','-A','OUTPUT','-d','127.0.0.2','-p','tcp','--dport','443','-j','REDIRECT','--to-ports','14443'],check=True)
    for kind in ('releases','events'):
        subprocess.run(['systemd-run','--unit','notification-test-'+kind,'/usr/bin/python3','-B',str(ROOT/'notification-runtime.py'),kind],check=True)
    (Path('/etc/deploy-platform/updates.json')).write_text(json.dumps({'repository':REPO}))
    Path('/etc/deploy-platform/updates.json').chmod(0o600)
    subprocess.run(['systemctl','restart','deploy-control'],check=True)
    sys.path.insert(0,'/opt/deploy-platform/current')
    from smoke import request
    def status():return request('/api/v1/updates')
    wait(lambda:status()['notifications']['connected'] and (status().get('candidate') or {}).get('version')==stable_version(),'SSE and stable prompt')
    spool=Path('/var/lib/deploy-platform/updates')
    assert not (spool/'request.json').exists() and not (spool/'active.json').exists()
    print('PASS: real Go SSE, dev default off, stable candidate prompts without installing',flush=True)
    subprocess.run(['cloudctl','update','policy','--dev-auto-upgrade','on'],check=True)
    def attempt():return json.loads((spool/'notification-dev.json').read_text())
    wait(lambda:attempt()['phase']=='succeeded','automatic dev installation',240)
    wait(lambda:not (spool/'active.json').exists(),'worker finished')
    installed=json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())
    assert installed['build']==manifest['build']
    backups=lambda:len(list(Path('/var/lib/deploy-platform/backups').iterdir()))
    saved=backups()
    subprocess.run(['systemctl','restart','deploy-control'],check=True);time.sleep(7)
    assert backups()==saved and attempt()['phase']=='succeeded'
    print('PASS: SSE → existing queue → public HTTPS verified dev → original installer; restart does not reinstall',flush=True)
    (ROOT/'notification-run').write_text('43')
    wait(lambda:attempt()['run_id']=='43' and attempt()['phase']=='failed','provenance mismatch refused')
    assert json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())['build']==manifest['build'] and backups()==saved
    downloads=(ROOT/'downloads-43').read_text()
    subprocess.run(['systemctl','restart','deploy-control'],check=True);time.sleep(7)
    assert (ROOT/'downloads-43').read_text()==downloads and backups()==saved and attempt()['phase']=='failed'
    subprocess.run(['cloudctl','update','policy','--dev-auto-upgrade','off'],check=True)
    print('PASS: wrong embedded commit refused before installation; durable failure suppresses reconnect retry',flush=True)


if __name__=='__main__':
    if not MARKER.exists():raise SystemExit('disposable notification node required')
    if sys.argv[1:] == ['setup']:setup()
    elif sys.argv[1:] == ['events']:http.server.ThreadingHTTPServer(('127.0.0.1',8090),Events).serve_forever()
    elif sys.argv[1:] == ['releases']:
        server=http.server.ThreadingHTTPServer(('0.0.0.0',14443),Releases)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain('/usr/local/share/ca-certificates/notification-test.crt',str(ROOT/'notification-test.key'))
        server.socket=context.wrap_socket(server.socket,server_side=True);server.serve_forever()
    else:raise SystemExit('expected setup, releases or events')
