#!/usr/bin/env python3
"""Real four-service acceptance via the same authenticated Web API.

Only the unique test projects' labelled instances are changed. Data/secrets and
project networks are retained. --keep-running leaves a live demo for the Web UI.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.request
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
from smoke import health, request, runtime, wait_job, wait_ready

root = Path(__file__).resolve().parent
spec_file = root / 'examples/fullstack/generate.py'
if not spec_file.exists(): spec_file = root.parents[1] / 'examples/fullstack/generate.py'
module_spec = importlib.util.spec_from_file_location('fullstack_specs', spec_file)
module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)

def run(image, policy, keep):
    health(20)
    project = 'stack-' + secrets.token_hex(4)
    other = project + '-isolated'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    specs = module.specifications(project,image,port,policy)
    password = secrets.token_hex(24)
    for name,value in [('db-password',password),('redis-password',password),('redis-config','requirepass "'+password+'"\n')]:
        request('/api/v1/secrets',{'project':project,'name':name,'value':value})
    def command(name,action,p=project):
        return wait_job(request('/api/v1/apps/'+p+'/'+name+'/'+action,{'request_id':project+'-'+action+'-'+secrets.token_hex(5)}))
    def identity(name,p=project):
        return next(a['instance_id'] for a in request('/api/v1/apps') if a['app']=={'project':p,'name':name})
    def owned(name,p=project):
        instance=identity(name,p)
        labels=json.loads(runtime('inspect','--',instance))[0]['Config']['Labels']
        if labels.get('io.deploy-platform.managed-by')!='rust-agent' or labels.get('io.deploy-platform.project')!=p or labels.get('io.deploy-platform.app')!=name:
            raise RuntimeError('unowned test container')
        return instance
    def diag(name,p=project):return request('/api/v1/diagnostics/'+p+'/'+name)
    def until(check,message,seconds=60):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                if check():return
            except (OSError,ValueError,subprocess.SubprocessError):pass
            time.sleep(1)
        raise RuntimeError(message)
    def visits(method='GET'):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req=urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/visits',method=method)
        with opener.open(req,timeout=5) as response:return json.loads(response.read(4096))
    deployed=[];other_deployed=False;completed=False
    try:
        for spec in specs:
            job=request('/api/v1/apps/apply',{'request_id':project+'-'+spec['app']['name'],'spec':spec,'startup_wait_ms':1000})
            deployed.append(spec['app']['name'])
            wait_job(job)
        until(lambda:diag('backend')['health']=='healthy','backend did not become ready')
        until(lambda:diag('frontend')['health']=='healthy','frontend did not become ready')
        result=visits('POST');count=result['postgres']
        if count<1 or result['redis']!=str(count):raise RuntimeError('database/cache data mismatch')
        print('PASS: frontend -> backend -> authenticated PostgreSQL + Redis, only frontend loopback port published',flush=True)
        command('postgres','stop')
        until(lambda:diag('backend')['health']=='unhealthy','HTTP readiness missed database outage',10)
        try:visits()
        except OSError:pass
        else:raise RuntimeError('frontend must surface backend dependency failure')
        log=request('/api/v1/logs/'+project+'/backend?tail=20')['text']
        if 'dependency unavailable' not in log:raise RuntimeError('application failure log missing')
        command('postgres','start')
        until(lambda:diag('backend')['health']=='healthy','dependency did not recover')
        print('PASS: database outage produces unhealthy HTTP + actionable logs, then recovers',flush=True)
        # Service name must stay usable after the backend IP/instance changes.
        old=owned('backend')
        subprocess.run(['systemctl','stop','deploy-agent'],check=True)
        try:runtime('stop','--',old);runtime('rm','--',old)
        finally:subprocess.run(['systemctl','start','deploy-agent'],check=True)
        wait_ready(30)
        until(lambda:identity('backend')!=old,'backend was not recreated',90)
        until(lambda:diag('backend')['health']=='healthy','recreated backend not ready')
        until(lambda:visits()['postgres']==count,'service name or persisted data did not recover',20)
        print('PASS: Agent restart + backend recreation + stable service-name resolution + persisted database count',flush=True)
        # A second project with the same service hostname must not access the first.
        isolated=dict(specs[-1],app={'project':other,'name':'frontend'},ports=[])
        wait_job(request('/api/v1/apps/apply',{'request_id':other,'spec':isolated,'startup_wait_ms':1000}));other_deployed=True
        peer=owned('frontend',other)
        target=json.loads(runtime('inspect','--',owned('backend')))[0]['NetworkSettings']['Networks']['dp-project-'+project]['IPAddress']
        for address in ['http://backend:8080/health','http://'+target+':8080/health']:
            result=subprocess.run(['/opt/deploy-platform/nerdctl','exec',peer,'/fullstack-demo','--probe',address],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            if result.returncode==0:raise RuntimeError('cross-project name/IP isolation failed')
        print('PASS: cross-project service names and direct container IP access blocked',flush=True)
        snapshots=[request('/api/v1/apps'),request('/api/v1/secrets/'+project),request('/api/v1/logs/'+project+'/backend?tail=30')]
        if password in json.dumps(snapshots):raise RuntimeError('secret leaked into metadata or demo logs')
        completed=True
        print('PASS: complete stack; project='+project+'; frontend=http://127.0.0.1:'+str(port),flush=True)
    finally:
        wait_ready(30)
        if other_deployed:command('frontend','stop',other)
        if not keep or not completed:
            for name in reversed(deployed):command(name,'stop')
            print('test applications stopped; retained volumes/secrets/project networks can be inspected',flush=True)
        else:print('demo left running for Web management',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--pull-policy',choices=['always','if_not_present','never'],default='always');p.add_argument('--keep-running',action='store_true')
    args=p.parse_args();run(args.image,args.pull_policy,args.keep_running)
