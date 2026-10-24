"""Public dev Release contract, shared by the publisher and node client."""
import re

SCHEMA = 1
MAX_INSTALLER = 512 * 1024**2
VERSION = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'


def validate(value, repository):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'repository', 'run_id', 'commit', 'version', 'installers', 'cli'}:
        raise ValueError('invalid dev manifest fields')
    if type(value['schema_version']) is not int or value['schema_version'] != SCHEMA or value['repository'] != repository:
        raise ValueError('dev manifest source/schema differs')
    if not isinstance(value['run_id'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value['run_id']) or int(value['run_id']) >= 2**64:
        raise ValueError('invalid dev run')
    if not isinstance(value['commit'], str) or not re.fullmatch('[a-f0-9]{40}', value['commit']):
        raise ValueError('invalid dev commit')
    if not isinstance(value['version'], str) or not re.fullmatch(VERSION, value['version']):
        raise ValueError('invalid dev version')
    if not isinstance(value['installers'], dict) or set(value['installers']) != {'amd64', 'arm64'}:
        raise ValueError('both dev architectures required')
    assets = [(value['cli'], 'cloudctl', 4*1024**2)]
    assets += [(value['installers'][arch], f'deploy-platform-{value["version"]}-linux-{arch}.run', MAX_INSTALLER) for arch in ('amd64', 'arm64')]
    for item, name, limit in assets:
        if not isinstance(item, dict) or set(item) != {'name', 'size', 'sha256'} or item['name'] != name:
            raise ValueError('invalid dev asset identity')
        if type(item['size']) is not int or not 0 < item['size'] <= limit:
            raise ValueError('invalid dev asset size')
        if not isinstance(item['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', item['sha256']):
            raise ValueError('invalid dev asset digest')
    return value
