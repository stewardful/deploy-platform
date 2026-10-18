"""Serial, bounded read-only business probes. HTTPS always verifies SNI/trust.

Only registered project domains and published loopback ports are permitted.
Applications must supply a side-effect-free GET endpoint for dependency/data checks.
"""
import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit
from upgrade_state import UpgradeError


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(('127.0.0.1', self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def validate(profile, specs, routes, known_projects=None):
    if set(profile) != {'schema_version', 'probes'} or profile['schema_version'] != 1:
        raise UpgradeError('unsupported upgrade probe profile')
    probes = profile['probes']
    if not isinstance(probes, list) or len(probes) > 32:
        raise UpgradeError('at most 32 sequential upgrade probes allowed')
    running = {s['app']['project'] for s in specs}
    known_projects = running if known_projects is None else known_projects
    active = []
    covered = set()
    domains = set()
    app_keys = {(s["app"]["project"], s["app"]["name"]) for s in specs}
    required_domains = {r["route"]["domain"] for r in routes if r["route"]["enabled"] and (r["route"]["app"]["project"], r["route"]["app"]["name"]) in app_keys}
    for probe in probes:
        if not {'project', 'url'} <= set(probe) or set(probe) - {'project', 'url', 'status', 'json_equals', 'unchanged_json', 'ca_file'}:
            raise UpgradeError('invalid upgrade probe fields')
        project = probe['project']
        url = urlsplit(probe['url'])
        if project not in known_projects or url.username or url.password or url.query or url.fragment or any(ord(c) < 33 for c in probe['url']):
            raise UpgradeError('probe must reference a running project without credentials/query/fragment')
        if project not in running:
            continue
        active.append(probe)
        if not (url.path or '/').startswith('/') or (url.path or '/').startswith('//'):
            raise UpgradeError('invalid probe path')
        if type(probe.get('status', 200)) is not int or not 200 <= probe.get('status', 200) < 300:
            raise UpgradeError('probe must require an explicit successful HTTP status')
        if url.scheme == 'https':
            allowed = {r['route']['domain'] for r in routes if r['route']['enabled'] and r['route']['app']['project'] == project}
            if url.hostname not in allowed or url.port not in (None, 443):
                raise UpgradeError('HTTPS probe must use a registered project domain on 443')
            domains.add(url.hostname)
        elif url.scheme == 'http':
            allowed = {p['host'] for s in specs if s['app']['project'] == project for p in s.get('ports', [])}
            if url.hostname != '127.0.0.1' or url.port not in allowed or 'ca_file' in probe:
                raise UpgradeError('HTTP probe must use a published project loopback port')
        else:
            raise UpgradeError('only local HTTP and registered HTTPS probes allowed')
        equals, unchanged = probe.get('json_equals', {}), probe.get('unchanged_json', [])
        if not isinstance(equals, dict) or not isinstance(unchanged, list) or len(equals) + len(unchanged) > 32:
            raise UpgradeError('invalid JSON probe assertions')
        if not all(isinstance(k, str) and 0 < len(k) <= 64 for k in list(equals) + unchanged):
            raise UpgradeError('JSON assertions use bounded top-level field names')
        covered.add(project)
    if covered != running:
        raise UpgradeError('each running project requires a read-only upgrade probe; missing: ' + ','.join(sorted(running - covered)))
    if not required_domains <= domains:
        raise UpgradeError("each enabled running domain requires an HTTPS upgrade probe")
    return active


def run(probe):
    url = urlsplit(probe['url'])
    if url.scheme == 'https':
        context = ssl.create_default_context(cafile=probe.get('ca_file'))
        connection = LocalHTTPS(url.hostname, 443, timeout=5, context=context)
    else:
        connection = http.client.HTTPConnection('127.0.0.1', url.port, timeout=5)
    try:
        connection.request('GET', url.path or '/', headers={'Connection': 'close', 'Cache-Control': 'no-cache'})
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != probe.get('status', 200) or len(data) > 65536:
            raise UpgradeError('business probe status/size check failed')
        if probe.get('json_equals') or probe.get('unchanged_json'):
            value = json.loads(data)
            if not isinstance(value, dict):
                raise UpgradeError('probe expected a JSON object')
            for key, expected in probe.get('json_equals', {}).items():
                if key not in value or value[key] != expected:
                    raise UpgradeError('business probe JSON assertion failed')
            return {key: value[key] for key in probe.get('unchanged_json', [])}
        return {}
    finally:
        connection.close()
