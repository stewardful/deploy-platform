"""Local authorization and a durable ledger for fixed automatic dev installs."""
import json
import re
import update_worker as worker
from state import write_record


def policy():
    repo,_=worker.settings(read_token=False)
    cfg=json.loads(worker.private_read(worker.CONFIG,4096)) if worker.CONFIG.exists() or worker.CONFIG.is_symlink() else {}
    if set(cfg)-{'repository','token_file','dev_auto_upgrade'} or type(cfg.get('dev_auto_upgrade',False)) is not bool:
        raise ValueError('unsupported install policy')
    return repo,cfg.get('dev_auto_upgrade',False)


def receipt(repo):
    path=worker.SPOOL/'automatic-dev.json'
    if not path.exists() and not path.is_symlink():return None
    value=json.loads(worker.private_read(path,4096))
    if (not isinstance(value,dict) or set(value)!={'schema_version','repository','run_id','phase'} or type(value['schema_version']) is not int or value['schema_version']!=1 or value['repository']!=repo or
        not isinstance(value['run_id'],str) or not re.fullmatch('[1-9][0-9]{0,19}',value['run_id']) or int(value['run_id'])>=2**64 or
        value['phase'] not in ('attempted','succeeded','failed')):raise ValueError('invalid automatic install receipt')
    return value


def record(repo,run,phase):
    write_record(worker.SPOOL/'automatic-dev.json',{'schema_version':1,'repository':repo,'run_id':run,'phase':phase})


def guard(repo):
    source,allowed=policy()
    if source!=repo or not allowed:raise ValueError('local dev automation is disabled')
