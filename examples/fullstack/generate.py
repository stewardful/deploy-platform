#!/usr/bin/env python3
"""Pure spec generation: no host mutation, secrets or runtime dependency."""
import argparse
import json
from pathlib import Path

def specifications(project, image, port=18080, pull_policy='always'):
    base = {'schema_version': 1, 'network': 'project', 'ports': [], 'pull_policy': pull_policy}
    def spec(name, image_ref, memory, **values):
        return dict(base, app={'project': project, 'name': name}, image=image_ref,
                    resources={'memory_mib': memory, 'cpu_millis': 500, 'pids': 128}, **values)
    postgres = spec('postgres', 'docker.io/library/postgres:17-bookworm', 384,
        user={'uid':999,'gid':999}, volumes=[{'name':'data','target':'/var/lib/postgresql/data'}],
        secrets=[{'name':'db-password'}],
        environment={'POSTGRES_DB':'app','PGDATA':'/var/lib/postgresql/data/pgdata','POSTGRES_PASSWORD_FILE':'/run/secrets/db-password'},
        args=['postgres','-c','shared_buffers=64MB','-c','max_connections=30'])
    redis = spec('redis', 'docker.io/library/redis:7.4-bookworm', 256,
        user={'uid':999,'gid':999}, volumes=[{'name':'data','target':'/data'}], secrets=[{'name':'redis-config'}],
        args=['redis-server','/run/secrets/redis-config','--appendonly','yes','--appendfsync','everysec','--dir','/data','--maxmemory','128mb','--maxmemory-policy','noeviction'])
    backend = spec('backend',image,128,user={'uid':65532,'gid':65532},secrets=[{'name':'db-password'},{'name':'redis-password'}],
        environment={'ROLE':'backend','GOMEMLIMIT':'48MiB','GOMAXPROCS':'1'},health_check={'port':8080,'path':'/health'})
    frontend = spec('frontend',image,64,user={'uid':65532,'gid':65532},environment={'ROLE':'frontend','GOMEMLIMIT':'24MiB','GOMAXPROCS':'1'},
        health_check={'port':8080,'path':'/health'})
    frontend['ports']=[{'host':port,'container':8080}]
    # Official database images are pulled normally; only custom images may be preloaded.
    postgres['pull_policy']=redis['pull_policy']='if_not_present'
    return [postgres,redis,backend,frontend]

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',default='fullstack');p.add_argument('--image',required=True)
    p.add_argument('--port',type=int,default=18080);p.add_argument('--pull-policy',choices=['always','if_not_present','never'],default='always')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    for spec in specifications(a.project,a.image,a.port,a.pull_policy):
        (a.output/(spec['app']['name']+'.json')).write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
