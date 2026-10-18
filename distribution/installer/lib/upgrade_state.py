"""Pure compatibility/state checks, also used before recovery switches to old binaries."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile


class UpgradeError(ValueError):
    """Safe operator-facing message; never include response bodies or credentials."""


STATE = Path('/var/lib/deploy-platform')
DATABASES = {'agent': 'agent.sqlite', 'ingress': 'ingress/ingress.sqlite'}


def private_json(path, max_bytes=65536):
    path = Path(path)
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077:
        raise UpgradeError('upgrade configuration must be a private regular file')
    if meta.st_size > max_bytes:
        raise UpgradeError('oversized upgrade configuration')
    return json.loads(path.read_text())


def contract(manifest):
    value = manifest.get('compatibility')
    if value is None:
        # Explicit known legacy layouts, never guess compatibility of an unknown release.
        if manifest.get('version') not in ('0.4.1', '0.5.0'):
            raise UpgradeError('old release has no known compatibility contract')
        value = {'schema_version': 1, 'agent_api': 'v1', 'paths_revision': 1,
                 'state': {'agent': {'read_min': 3, 'read_max': 3, 'write': 3},
                           'ingress': {'read_min': 0, 'read_max': 0, 'write': 0}}}
    if value.get('schema_version') != 1 or value.get('agent_api') != 'v1' or value.get('paths_revision') != 1:
        raise UpgradeError('unsupported compatibility contract')
    if set(value.get('state', {})) != set(DATABASES):
        raise UpgradeError('incomplete state compatibility contract')
    for rule in value['state'].values():
        if set(rule) != {'read_min', 'read_max', 'write'} or any(type(n) is not int for n in rule.values()):
            raise UpgradeError('invalid schema contract')
        if not 0 <= rule['read_min'] <= rule['write'] <= rule['read_max']:
            raise UpgradeError('invalid schema range')
    return value


def database(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise UpgradeError('missing or linked platform database')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    connection.execute('PRAGMA query_only=ON')
    return connection


def schema_versions(root=STATE):
    result = {}
    for name, relative in DATABASES.items():
        with closing(database(root / relative)) as db:
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise UpgradeError('platform SQLite integrity check failed')
            result[name] = db.execute('PRAGMA user_version').fetchone()[0]
    return result


def accepts(policy, versions):
    return all(policy['state'][name]['read_min'] <= number <= policy['state'][name]['read_max']
               for name, number in versions.items())


def check(old, new, root=STATE):
    def version(value):
        if not re.fullmatch(r'\d+\.\d+\.\d+', value):
            raise UpgradeError('automatic upgrades require a stable semantic version')
        return tuple(map(int, value.split('.')))
    if version(new['version']) < version(old['version']):
        raise UpgradeError('automatic platform downgrade is not supported')
    if old['arch'] != new['arch'] or old['runtime'] != new['runtime']:
        raise UpgradeError('architecture/runtime changes require a planned migration')
    if old.get('proxy') != new.get('proxy'):
        raise UpgradeError('proxy/TLS storage version changes require a planned migration')
    old_policy, new_policy = contract(old), contract(new)
    if old_policy['paths_revision'] != new_policy['paths_revision']:
        raise UpgradeError('state path migration requires a separate procedure')
    versions = schema_versions(root)
    if not accepts(old_policy, versions) or not accepts(new_policy, versions):
        raise UpgradeError('installed database schema is incompatible')
    target = {name: rule['write'] for name, rule in new_policy['state'].items()}
    if not accepts(old_policy, target):
        raise UpgradeError('new schema cannot be read by the rollback release; planned migration required')
    required = sum((root / p).stat().st_size for p in DATABASES.values()) * 3 + 64 * 1024**2
    if shutil.disk_usage(root).free < required:
        raise UpgradeError('insufficient space for consistent SQLite backup and rehearsal')
    return old_policy


def pending(root=STATE):
    with closing(database(root / 'agent.sqlite')) as db:
        jobs = db.execute("SELECT count(*) FROM api_jobs WHERE state IN ('queued','running')").fetchone()[0]
        operations = db.execute('SELECT count(*) FROM operations WHERE phase NOT IN (?,?,?)',
                                ('"succeeded"', '"failed"', '"interrupted"')).fetchone()[0]
    return jobs + operations


def logical_state(root=STATE):
    # Deliberately excludes mutable job/recovery timestamps and business volume contents.
    with closing(database(root / 'agent.sqlite')) as db:
        apps = db.execute('SELECT name,spec,fingerprint,desired FROM applications ORDER BY name').fetchall()
    with closing(database(root / 'ingress/ingress.sqlite')) as db:
        routes = db.execute('SELECT domain,revision,spec FROM routes ORDER BY domain').fetchall()
        revision = db.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
    return {'apps': apps, 'routes': routes, 'revision': revision}


def resource_identity(root=STATE):
    """Hash bounded secret files and volume metadata, never scan changing database contents."""
    result = {}
    files = list((root / 'resources/secrets').glob('*/*/value'))
    files += list((root / 'resources/volumes').glob('*/*.json'))
    token = Path('/etc/deploy-platform/control.token')
    if token.exists():
        files.append(token)
    for path in sorted(files):
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > 65536:
            raise UpgradeError('linked, special or oversized resource metadata')
        result[str(path)] = [hashlib.sha256(path.read_bytes()).hexdigest(), meta.st_uid, meta.st_gid, stat.S_IMODE(meta.st_mode)]
        if path.suffix == '.json':
            directory = path.with_suffix('')
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UpgradeError('missing/linked volume data directory')
            result[str(directory)] = [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]
    return result


def backup_and_rehearse(source, destination, root=STATE):
    destination = Path(destination)
    for name, relative in DATABASES.items():
        with closing(database(root / relative)) as src, closing(sqlite3.connect(destination / (name + '.sqlite'))) as dst:
            src.backup(dst)
        (destination / (name + '.sqlite')).chmod(0o600)
    with tempfile.TemporaryDirectory(prefix='state-rehearsal-', dir=destination) as temporary:
        copy = Path(temporary)
        (copy / 'ingress').mkdir(mode=0o700)
        shutil.copy2(destination / 'agent.sqlite', copy / 'agent.sqlite')
        shutil.copy2(destination / 'ingress.sqlite', copy / 'ingress/ingress.sqlite')
        diagnostics = destination / 'migration-rehearsal.stderr'
        with diagnostics.open('xb') as output:
            diagnostics.chmod(0o600)
            subprocess.run([str(Path(source) / 'bin/agentd'), '--check-state', '--state-dir', str(copy),
                            '--address', '/run/deploy-platform-containerd/containerd.sock', '--namespace', 'deploy-platform'],
                           check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=output)
        target = {n: r['write'] for n, r in contract(json.loads((Path(source) / 'manifest.json').read_text()))['state'].items()}
        if schema_versions(copy) != target:
            raise UpgradeError('candidate binary does not match declared state schema')


def check_token(path):
    path = Path(path)
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_size > 256:
        raise UpgradeError('existing management token must be a private regular file')
    if not re.fullmatch(rb'[A-Za-z0-9_-]{32,128}', path.read_bytes().strip()):
        raise UpgradeError('existing management token is missing or invalid; refusing replacement during upgrade')
