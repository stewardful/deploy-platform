#!/usr/bin/env python3
"""Disposable HTTPS GitHub fixture for the actual one-command first installation."""
import argparse
import hashlib
import hmac
import urllib.request
import urllib.error
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=Path('/var/tmp/bootstrap-fixture')
MARKER=Path('/run/cloud-bootstrap-isolated-test')


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(256*1024):value.update(chunk)
    return value.hexdigest()


def prepare(args):
    sys.path.insert(0,str(ROOT/'distribution/installer/lib'))
    sys.path.insert(0,str(ROOT/'distribution/cli'))
    from archive import installer_manifest
    from dependencies import fetch
    from dev_manifest import validate
    from installer_entry import verify
    manifest=installer_manifest(args.installer);arch=manifest['arch']
    tag='dev-'+args.run_id
    verify(args.cli.parent,args.repository,tag)
    output=args.output;output.mkdir(parents=True,exist_ok=True)
    paths={}
    def asset(url,name,source):
        shutil.copyfile(source,output/name);paths[urllib.parse.urlsplit(url).path]=name
    base='https://github.com/'+args.repository+'/releases/download/'
    asset(base+tag+'/cloudctl','cloudctl',args.cli)
    asset(base+'dev-latest/install.sh','install.sh',args.cli.parent/'install.sh')
    asset(base+tag+'/installer.run','installer.run',args.installer)
    name='deploy-platform-'+manifest['version']+'-linux-'+arch+'.run'
    paths[urllib.parse.urlsplit(base+tag+'/'+name).path]='installer.run'
    del paths[urllib.parse.urlsplit(base+tag+'/installer.run').path]
    item={'name':name,'size':args.installer.stat().st_size,'sha256':digest(args.installer)}
    index={'schema_version':1,'repository':args.repository,'run_id':args.run_id,'commit':manifest['build']['commit'],
           'version':manifest['version'],'cli':{'name':'cloudctl','size':args.cli.stat().st_size,'sha256':digest(args.cli)},
           'installers':{a:dict(item,name='deploy-platform-'+manifest['version']+'-linux-'+a+'.run') for a in ('amd64','arm64')}}
    validate(index,args.repository)
    (output/'dev.json').write_text(json.dumps(index))
    for release in (tag,'dev-latest'):paths[urllib.parse.urlsplit(base+release+'/dev.json').path]='dev.json'
    for kind,supplied in [('runtime',args.runtime),('proxy',args.proxy)]:
        lock=manifest[kind];url=lock['archive_url_template'].format(arch=arch)
        checksum=lock['archives'][arch]
        if kind=='proxy':checksum=checksum['sha256']
        destination=output/(kind+'.tar.gz')
        if supplied:shutil.copyfile(supplied,destination)
        else:fetch(url,destination,checksum)
        if digest(destination)!=checksum:raise ValueError('fixture dependency checksum differs')
        paths[urllib.parse.urlsplit(url).path]=destination.name
    (output/'fixture.json').write_text(json.dumps({'paths':paths,'repository':args.repository,'manifest':manifest}))


class Releases(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        config=json.loads((FIXTURE/'fixture.json').read_text())
        if self.headers.get('Authorization') or self.path not in config['paths']:self.send_error(404);return
        path=FIXTURE/config['paths'][self.path]
        with (FIXTURE/'requests.log').open('a') as out:out.write(self.path+'\n')
        corrupt=path.name=='cloudctl' and (FIXTURE/'corrupt-cli').exists()
        self.send_response(200);self.send_header('Content-Length',str(path.stat().st_size));self.end_headers()
        with path.open('rb') as source:
            if corrupt:
                value=source.read(1);self.wfile.write(bytes([value[0]^1]))
            shutil.copyfileobj(source,self.wfile)


def setup():
    if not MARKER.exists():raise ValueError('disposable bootstrap node required')
    subprocess.run(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-days','1','-subj','/CN=github.com',
      '-addext','subjectAltName=DNS:github.com,DNS:api.github.com','-keyout',str(FIXTURE/'server.key'),
      '-out','/usr/local/share/ca-certificates/bootstrap-test.crt'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['update-ca-certificates'],check=True,stdout=subprocess.DEVNULL)
    with Path('/etc/hosts').open('a') as out:out.write('\n127.0.0.2 github.com api.github.com\n')
    subprocess.run(['iptables','-t','nat','-A','OUTPUT','-d','127.0.0.2','-p','tcp','--dport','443','-j','REDIRECT','--to-ports','14443'],check=True)
    subprocess.run(['systemd-run','--unit=bootstrap-test-https','/usr/bin/python3','-B',str(FIXTURE/'bootstrap-runtime.py'),'serve'],check=True)
    config=json.loads((FIXTURE/'fixture.json').read_text())
    url='https://github.com/'+config['repository']+'/releases/download/dev-latest/install.sh'
    # Wait for the local HTTPS fixture, keeping certificate verification enabled.
    for _ in range(100):
        response=subprocess.run(['curl','--fail','--silent','--output','/dev/null',url],capture_output=True)
        if response.returncode==0:break
        time.sleep(.1)
    else:raise RuntimeError('HTTPS fixture not ready')
    command=['bash','-o','pipefail','-c','curl --fail --silent --show-error "$1" | bash -s -- --channel dev --cli-only','bootstrap-test',url]
    failed_fetch=subprocess.run(command[:-1]+[url+'.missing'],capture_output=True,text=True,timeout=30)
    assert failed_fetch.returncode!=0 and not Path('/var/lib/deploy-platform/updates').exists()
    (FIXTURE/'corrupt-cli').touch()
    result=subprocess.run(command,capture_output=True,text=True,timeout=60)
    assert result.returncode!=0 and 'checksum mismatch' in result.stderr
    assert not Path('/var/lib/deploy-platform/updates').exists() and not Path('/opt/deploy-platform/current').exists()
    (FIXTURE/'corrupt-cli').unlink()
    print('PASS: actual curl pipe rejects changed cloudctl before installation',flush=True)
    # Test real dependency preparation, without changing the host or the fixture server.
    subprocess.run(['apt-get','purge','-y','python3-minimal'],check=True,stdout=subprocess.DEVNULL)
    assert not Path('/usr/bin/python3').exists()
    result=subprocess.run(command,capture_output=True,text=True,timeout=600)
    print(result.stdout,flush=True)
    if result.returncode:
        print(result.stderr,file=sys.stderr)
        for name in ('/opt','/var/lib/deploy-platform','/var/lib/deploy-platform/updates','/etc/deploy-platform'):
            path=Path(name)
            print('fixture path:',name,oct(path.stat().st_mode & 0o7777) if path.exists() else 'absent',file=sys.stderr)
        raise RuntimeError('one-command install failed')
    assert 'Independent cloudctl ready' in result.stdout
    launcher=Path('/usr/local/bin/cloudctl').resolve()
    assert launcher.parent.parent==Path('/opt/cloudctl/releases')
    assert not Path('/opt/deploy-platform/current').exists()
    assert json.loads(subprocess.check_output(['cloudctl','version'],text=True))['installed'] is False
    if (FIXTURE/'webhook').exists():
        install_via_webhook(config)
    else:
        subprocess.run(['cloudctl','install','--channel','dev'],check=True)
    installed=json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())
    assert installed['build']==config['manifest']['build'] and installed['arch']==config['manifest']['arch']
    assert Path('/usr/local/bin/cloudctl').resolve()==launcher
    policy=json.loads(subprocess.check_output(['cloudctl','update','policy'],text=True))
    if not (FIXTURE/'webhook').exists():assert policy['dev_auto_upgrade'] is False
    assert 'succeeded' in subprocess.check_output(['cloudctl','update','status'],text=True)
    full_command=command.copy();full_command[4]=full_command[4].replace(' --cli-only','')
    full_result=subprocess.run(full_command,capture_output=True,text=True,timeout=90)
    if full_result.returncode:raise RuntimeError(full_result.stderr)
    assert 'Installation verified' in full_result.stdout
    token=Path('/etc/deploy-platform/control.token').read_bytes()
    subprocess.run(['systemctl','stop','deploy-control'],check=True)
    duplicate=json.loads(subprocess.check_output(['cloudctl','install','--channel','dev'],text=True))
    assert duplicate['phase']=='unchanged'
    current=Path('/opt/deploy-platform/current');saved=current.with_name('test-unavailable')
    current.rename(saved)
    try:
        assert json.loads(subprocess.check_output(['cloudctl','version'],text=True))['installed'] is False
        subprocess.run(['cloudctl','update','status'],check=True)
    finally:saved.rename(current)
    subprocess.run(['systemctl','start','deploy-control'],check=True)
    subprocess.run(['cloudctl','status'],check=True)
    assert json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())==installed
    assert Path('/etc/deploy-platform/control.token').read_bytes()==token
    assert Path('/usr/local/bin/cloudctl').resolve()==launcher
    print('PASS: missing Python prepared, independent CLI-only ready; protected install and no-op without Go API; CLI survives unavailable platform; data/token/launcher preserved',flush=True)


def until(predicate,label,seconds=300):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if predicate():return
        except (OSError,ValueError,KeyError):pass
        time.sleep(.3)
    raise RuntimeError(label)


def install_via_webhook(config):
    bundle=FIXTURE/'webhook'
    shutil.copyfile(bundle/'deploy-webhook','/usr/local/bin/deploy-webhook');Path('/usr/local/bin/deploy-webhook').chmod(0o755)
    for name in ('deploy-webhook.service','deploy-webhook-worker.service','deploy-webhook-worker.path'):
        target=Path('/etc/systemd/system')/name
        shutil.copyfile(bundle/name,target);target.chmod(0o644)
    directory=Path('/etc/deploy-webhook');directory.mkdir(mode=0o700)
    secret=b'a'*32;(directory/'webhook.secret').write_bytes(secret);(directory/'webhook.secret').chmod(0o600)
    subprocess.run(['systemd-analyze','verify',*[str(Path('/etc/systemd/system')/name) for name in ('deploy-webhook.service','deploy-webhook-worker.service','deploy-webhook-worker.path')]],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','deploy-webhook.service','deploy-webhook-worker.path','deploy-webhook-worker.service'],check=True)
    def status():return json.loads(subprocess.check_output(['deploy-webhook','--status'],text=True))
    index=json.loads((FIXTURE/'dev.json').read_text());run=index['run_id'];commit=index['commit']
    def send(number,sha,stable=False):
        body=json.dumps({'action':'published','repository':{'full_name':config['repository'],'id':1},'release':{'id':int(number),'tag_name':'v9.9.9' if stable else 'dev-'+number,'target_commitish':sha,'prerelease':not stable,'draft':False,'published_at':'2026-10-26T00:00:00Z'}}).encode()
        signature='sha256='+hmac.new(secret,body,hashlib.sha256).hexdigest()
        req=urllib.request.Request('http://127.0.0.1:8090/hooks/github/releases',data=body,headers={'Content-Type':'application/json','X-GitHub-Event':'release','X-GitHub-Delivery':'00000000-0000-0000-0000-000000000001','X-Hub-Signature-256':signature})
        with urllib.request.urlopen(req,timeout=5) as response:assert response.status==200
    until(lambda:subprocess.run(['curl','--fail','--silent','http://127.0.0.1:8090/healthz'],stdout=subprocess.DEVNULL).returncode==0,'HTTP did not start')
    send(run,commit);until(lambda:(status().get('dev') or {}).get('phase')=='blocked','default policy was not enforced')
    assert not Path('/opt/deploy-platform/current').exists()
    subprocess.run(['cloudctl','update','policy','--dev-auto-upgrade','on'],check=True)
    next_run=str(int(run)+1);index['run_id']=next_run;(FIXTURE/'dev.json').write_text(json.dumps(index))
    for url,name in list(config['paths'].items()):
        if '/dev-'+run+'/' in url:config['paths'][url.replace('/dev-'+run+'/','/dev-'+next_run+'/')]=name
    (FIXTURE/'fixture.json').write_text(json.dumps(config))
    send(next_run,commit);until(lambda:(status().get('dev') or {}).get('phase')=='succeeded','Webhook CLI install did not succeed',420)
    send(next_run,commit);send(run,commit)
    before=Path('/opt/deploy-platform/current').readlink()
    # A different validly signed commit cannot substitute an unrelated installer.
    wrong_run=str(int(next_run)+1)
    index['run_id']=wrong_run;(FIXTURE/'dev.json').write_text(json.dumps(index))
    for url,name in list(config['paths'].items()):
        if '/dev-'+next_run+'/' in url:config['paths'][url.replace('/dev-'+next_run+'/','/dev-'+wrong_run+'/')]=name
    (FIXTURE/'fixture.json').write_text(json.dumps(config))
    send(wrong_run,'c'*40);until(lambda:(status().get('dev') or {}).get('phase')=='failed','wrong commit not rejected')
    send(wrong_run,'c'*40)
    send(wrong_run,commit,stable=True);until(lambda:status().get('stable') is not None,'stable manual record missing')
    assert Path('/opt/deploy-platform/current').readlink()==before
    index['run_id']=next_run;(FIXTURE/'dev.json').write_text(json.dumps(index))
    subprocess.run(['systemctl','restart','deploy-webhook'],check=True)
    subprocess.run(['systemctl','start','deploy-webhook-worker'],check=False)
    assert Path('/opt/deploy-platform/current').readlink()==before
    subprocess.run(['cloudctl','update','policy','--dev-auto-upgrade','off'],check=True)
    print('PASS: real DynamicUser Webhook → root worker → fixed cloudctl → first install; default off, dedup, wrong SHA rejected, stable manual, restart no retry',flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['setup']:setup()
    elif sys.argv[1:]==['serve']:
        if not MARKER.exists():raise SystemExit('disposable fixture required')
        server=http.server.ThreadingHTTPServer(('0.0.0.0',14443),Releases)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain('/usr/local/share/ca-certificates/bootstrap-test.crt',str(FIXTURE/'server.key'))
        server.socket=context.wrap_socket(server.socket,server_side=True);server.serve_forever()
    else:
        parser=argparse.ArgumentParser();parser.add_argument('installer',type=Path);parser.add_argument('cli',type=Path);parser.add_argument('output',type=Path)
        parser.add_argument('--run-id',default=os.environ.get('GITHUB_RUN_ID','42'))
        parser.add_argument('--repository',default=os.environ.get('GITHUB_REPOSITORY','stewardful/deploy-platform'))
        parser.add_argument('--runtime',type=Path);parser.add_argument('--proxy',type=Path)
        prepare(parser.parse_args())
