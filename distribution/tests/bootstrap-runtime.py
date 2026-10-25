#!/usr/bin/env python3
"""Disposable HTTPS GitHub fixture for the actual one-command first installation."""
import argparse
import hashlib
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
      '-addext','subjectAltName=DNS:github.com','-keyout',str(FIXTURE/'server.key'),
      '-out','/usr/local/share/ca-certificates/bootstrap-test.crt'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['update-ca-certificates'],check=True,stdout=subprocess.DEVNULL)
    with Path('/etc/hosts').open('a') as out:out.write('\n127.0.0.2 github.com\n')
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
    command=['bash','-o','pipefail','-c','curl --fail --silent --show-error "$1" | bash -s -- --channel dev','bootstrap-test',url]
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
    assert 'Installation verified' in result.stdout
    installed=json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())
    assert installed['build']==config['manifest']['build'] and installed['arch']==config['manifest']['arch']
    policy=json.loads(subprocess.check_output(['cloudctl','update','policy'],text=True))
    assert policy['dev_auto_upgrade'] is False
    assert 'succeeded' in subprocess.check_output(['cloudctl','update','status'],text=True)
    token=Path('/etc/deploy-platform/control.token').read_bytes()
    before=(FIXTURE/'requests.log').read_text().count('/cloudctl\n')
    duplicate=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert duplicate.returncode!=0 and 'Existing installation detected' in duplicate.stderr
    assert (FIXTURE/'requests.log').read_text().count('/cloudctl\n')==before
    assert json.loads(Path('/opt/deploy-platform/current/manifest.json').read_text())==installed
    assert Path('/etc/deploy-platform/control.token').read_bytes()==token
    print('PASS: missing Python prepared, verified public dev installed, all services healthy, auto OFF, repeat preserves installation/token',flush=True)


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
