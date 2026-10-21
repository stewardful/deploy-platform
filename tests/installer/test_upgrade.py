"""Upgrade failure injection: temp state, fake services/transports, no host changes."""
import copy
import json
from pathlib import Path
import sqlite3
import ssl
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'distribution/installer/lib'))
import upgrade_state as state
import upgrade_probes as probes
import upgrade
import state as transaction

POLICY = json.loads((ROOT / 'distribution/release/compatibility.json').read_text())


def manifest(version='0.5.3'):
    return {'version': version, 'arch': 'arm64', 'runtime': {'fixed': 'same'}, 'compatibility': copy.deepcopy(POLICY)}


def databases(root):
    (root / 'ingress').mkdir()
    with sqlite3.connect(root / 'agent.sqlite') as db:
        db.executescript((ROOT / 'crates/agent-store-sqlite/schema.sql').read_text())
    with sqlite3.connect(root / 'ingress/ingress.sqlite') as db:
        db.executescript('CREATE TABLE meta(id INTEGER PRIMARY KEY,revision INTEGER); INSERT INTO meta VALUES(1,0); CREATE TABLE routes(domain TEXT,revision INTEGER,spec TEXT);')


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        databases(self.root)

    def test_same_layout_upgrade_preserves_existing_databases(self):
        before = state.logical_state(self.root)
        self.assertEqual(state.check(manifest(), manifest('0.5.4'), self.root), POLICY)
        self.assertEqual(state.logical_state(self.root), before)

    def test_rejects_runtime_architecture_downgrade_and_missing_contract(self):
        for key, value in [('runtime', {'fixed': 'different'}), ('arch', 'amd64'), ('proxy', {'version': 'changed'}), ('version', '0.4.1')]:
            candidate = manifest('0.5.4'); candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                state.check(manifest(), candidate, self.root)
        for version in ('0.3.0', '0.4.1', '0.5.0', '0.5.3'):
            old = manifest(version); del old['compatibility']
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'valid compatibility'):
                state.check(old, manifest('0.5.4'), self.root)

    def test_schema_that_breaks_rollback_is_rejected_before_live_migration(self):
        candidate = manifest('0.5.4')
        candidate['compatibility']['state']['agent'].update(read_max=4, write=4)
        with self.assertRaisesRegex(ValueError, 'rollback release'):
            state.check(manifest(), candidate, self.root)
        self.assertEqual(state.schema_versions(self.root)['agent'], 3)

    def test_corrupt_future_and_linked_databases_are_rejected(self):
        with sqlite3.connect(self.root / 'agent.sqlite') as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            state.check(manifest(), manifest(), self.root)
        (self.root / 'agent.sqlite').unlink()
        (self.root / 'agent.sqlite').symlink_to(self.root / 'ingress/ingress.sqlite')
        with self.assertRaisesRegex(ValueError, 'linked'):
            state.schema_versions(self.root)

    def test_insufficient_backup_space_is_rejected(self):
        with patch.object(state.shutil, 'disk_usage', return_value=Mock(free=1)), self.assertRaisesRegex(ValueError, 'space'):
            state.check(manifest(), manifest(), self.root)

    def test_resource_identity_tracks_secrets_and_volume_identity_without_scanning_data(self):
        secret = self.root / 'resources/secrets/demo/password/value'
        secret.parent.mkdir(parents=True); secret.write_text('test-only-value')
        volume = self.root / 'resources/volumes/dp-demo-db/data'
        volume.mkdir(parents=True)
        marker = volume.with_suffix('.json'); marker.write_text('{"name":"data"}')
        data = volume / 'business-data'; data.write_text('changing live data')
        before = state.resource_identity(self.root)
        data.write_text('normal writes during platform upgrade')
        self.assertEqual(state.resource_identity(self.root), before)
        secret.write_text('another-test-value')
        self.assertNotEqual(state.resource_identity(self.root), before)
        secret.write_text('test-only-value')
        volume.rename(volume.with_name('retained-old-directory')); volume.mkdir()
        self.assertNotEqual(state.resource_identity(self.root), before)

    def test_stop_boundary_rechecks_pending_work_before_backup_or_activation(self):
        baseline = self.root / upgrade.BASELINE
        upgrade.write_private(baseline, {'logical': {}, 'resources': {}})
        with patch.object(state, 'pending', return_value=1), patch.object(state, 'backup_and_rehearse') as rehearse:
            with self.assertRaisesRegex(ValueError, 'refusing activation'):
                upgrade.stopped('unused', self.root)
            rehearse.assert_not_called()

    def test_rehearsal_uses_only_private_copy_and_checks_result_schema(self):
        source = self.root / 'release'; source.mkdir()
        (source / 'manifest.json').write_text(json.dumps(manifest()))
        backup = self.root / 'backup'; backup.mkdir(mode=0o700)
        original = (self.root / 'agent.sqlite').read_bytes()
        def candidate(args, **kwargs):
            copied = Path(args[args.index('--state-dir') + 1])
            self.assertNotEqual(copied, self.root)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o700)
            self.assertEqual(state.schema_versions(copied), {'agent': 3, 'ingress': 0})
            self.assertIn('--check-state', args)
            self.assertEqual(kwargs['timeout'], 45)
        with patch.object(state.subprocess, 'run', side_effect=candidate):
            state.backup_and_rehearse(source, backup, self.root)
        self.assertEqual((self.root / 'agent.sqlite').read_bytes(), original)
        self.assertEqual((backup / 'agent.sqlite').stat().st_mode & 0o777, 0o600)

    def test_future_schema_after_activation_blocks_rollback_without_restoring_files(self):
        baseline = self.root / upgrade.BASELINE
        baseline.write_text(json.dumps({'rollback_contract': POLICY})); baseline.chmod(0o600)
        (self.root / 'snapshot.json').write_text('[]')
        journal = self.root / 'transaction.json'
        journal.write_text(json.dumps({'backup': str(self.root), 'previous': '/opt/deploy-platform/releases/old', 'bootstrap_cli': None, 'stage': 'activating', 'active': []}))
        with patch.object(transaction, 'TRANSACTION', str(journal)), \
             patch.object(transaction, 'BACKUP_ROOT', self.root.parent), \
             patch.object(transaction.subprocess, 'run') as run, \
             patch.object(state, 'schema_versions', return_value={'agent': 4, 'ingress': 0}), \
             patch.object(transaction, 'restore') as restore:
            with self.assertRaisesRegex(ValueError, 'rollback blocked'):
                transaction.recover('unused')
            restore.assert_not_called()
        self.assertTrue(journal.exists())


class DrainTests(unittest.TestCase):
    def test_drain_failure_recovery_resumes_without_stopping_services_or_jobs(self):
        import agent_http
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'snapshot.json').write_text('[]')
            journal = root / 'transaction.json'
            journal.write_text(json.dumps({'backup': str(root), 'previous': '/opt/deploy-platform/releases/old', 'bootstrap_cli': None, 'active': list(transaction.UNITS), 'stage': 'draining'}))
            with patch.object(transaction, 'BACKUP_ROOT', root.parent), patch.object(transaction, 'TRANSACTION', str(journal)), patch.object(transaction, 'cleanup_upgrade'), patch.object(agent_http, 'request') as request, patch.object(transaction.subprocess, 'run') as run:
                transaction.recover('unused')
                request.assert_called_once_with('/v1/maintenance/exit', 'POST')
                run.assert_not_called()
            self.assertFalse(journal.exists())


    def test_failed_first_activation_restores_bootstrap_cli_for_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); backup = root/'backup'; backup.mkdir()
            (backup/'snapshot.json').write_text('[]')
            bootstrap = root/'bootstrap'/('a'*64)/'cloudctl'; bootstrap.parent.mkdir(parents=True); bootstrap.write_text('CLI')
            current = root/'current'; current.symlink_to(root/'new-release')
            launcher = root/'cloudctl'; launcher.symlink_to(current/'cloudctl')
            journal = root/'transaction.json'
            journal.write_text(json.dumps({'backup': str(backup), 'previous': None, 'bootstrap_cli': str(bootstrap), 'active': [], 'stage': 'activating', 'cidr': '10.89.0.0/24'}))
            with patch.object(transaction, 'CURRENT', current), patch.object(transaction, 'LAUNCHER', launcher), \
                 patch.object(transaction, 'BOOTSTRAP_ROOT', root/'bootstrap'), patch.object(transaction, 'BACKUP_ROOT', root), \
                 patch.object(transaction, 'RECEIPT', str(root/'receipt')), patch.object(transaction, 'TRANSACTION', str(journal)), \
                 patch.object(transaction, 'cleanup_upgrade'), patch.object(transaction, 'restore') as restore, \
                 patch.object(transaction, 'receipt'), patch.object(transaction.subprocess, 'run'):
                transaction.recover('unused')
            self.assertEqual(launcher.readlink(), bootstrap)
            self.assertFalse(current.is_symlink())
            self.assertFalse(journal.exists())
            restore.assert_called_once_with(str(backup))

    def test_native_waits_for_accepted_jobs_and_does_not_force_cancel(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(upgrade, 'MARKER', Path(temp) / 'marker'), \
             patch.object(upgrade, 'request', side_effect=[{}, {'draining': True, 'quiescent': False, 'pending_jobs': 1, 'active_writers': 0}, {'draining': True, 'quiescent': True, 'pending_jobs': 0, 'active_writers': 0}]) as request, \
             patch.object(upgrade.time, 'sleep'):
            upgrade.drain(10)
            self.assertEqual(request.call_args_list[0].args, ('/v1/maintenance/enter', 'POST'))
            self.assertTrue(upgrade.MARKER.exists())
            self.assertEqual(upgrade.MARKER.stat().st_mode & 0o777, 0o600)

    def test_timeout_keeps_marker_for_outer_transaction_recovery(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(upgrade, 'MARKER', Path(temp) / 'marker'), \
             patch.object(upgrade, 'request', side_effect=[{}, {'draining': True, 'quiescent': False, 'pending_jobs': 1, 'active_writers': 0}]), \
             patch.object(upgrade.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaises(TimeoutError): upgrade.drain(10)
            self.assertTrue(upgrade.MARKER.exists())

    def test_unsupported_or_failed_maintenance_api_aborts_without_socket_fallback(self):
        import agent_http
        for status in (404, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp, \
                 patch.object(upgrade, 'MARKER', Path(temp) / 'marker'), \
                 patch.object(upgrade, 'request', side_effect=agent_http.AgentError(status)) as request:
                with self.assertRaises(agent_http.AgentError): upgrade.drain(10)
                request.assert_called_once_with('/v1/maintenance/enter', 'POST')

    def test_failed_gate_exit_keeps_transaction_for_recovery(self):
        import agent_http
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'snapshot.json').write_text('[]')
            journal = root / 'transaction.json'
            journal.write_text(json.dumps({'backup': str(root), 'previous': '/opt/deploy-platform/releases/current', 'bootstrap_cli': None, 'active': [], 'stage': 'draining'}))
            with patch.object(transaction, 'BACKUP_ROOT', root.parent), patch.object(transaction, 'TRANSACTION', str(journal)), \
                 patch.object(agent_http, 'request', side_effect=agent_http.AgentError(404)), \
                 patch.object(transaction, 'cleanup_upgrade') as cleanup:
                with self.assertRaises(agent_http.AgentError): transaction.recover('unused')
                cleanup.assert_not_called()
            self.assertTrue(journal.exists())


SPEC = {'app': {'project': 'demo', 'name': 'frontend'}, 'ports': [{'host': 18080, 'container': 8080}]}
ROUTE = {'route': {'domain': 'app.your-domain.com', 'app': SPEC['app'], 'enabled': True}, 'proxy_state': 'applied', 'upstream_ready': True}
PROBE = {'project': 'demo', 'url': 'https://app.your-domain.com/api/upgrade-check', 'json_equals': {'service': 'backend'}, 'unchanged_json': ['postgres', 'redis']}


class ProbeTests(unittest.TestCase):
    def test_missing_secret_or_inconsistent_volume_reference_blocks_capture(self):
        spec = dict(SPEC, secrets=[{'name': 'password'}], volumes=[{'name': 'data', 'target': '/data'}])
        with patch.object(upgrade, 'request', return_value=[]), self.assertRaisesRegex(ValueError, 'secret reference'):
            upgrade.validate_resource_refs([spec])
        with patch.object(upgrade, 'request', side_effect=[[{'name': 'password'}], [{'name': 'data', 'target': '/wrong', 'uid': 0, 'gid': 0}]]), self.assertRaisesRegex(ValueError, 'volume metadata'):
            upgrade.validate_resource_refs([spec])

    def test_profile_requires_project_and_https_domain_coverage(self):
        profile = {'schema_version': 1, 'probes': [PROBE]}
        probes.validate(profile, [SPEC], [ROUTE])
        with self.assertRaisesRegex(ValueError, 'each running project'):
            probes.validate({'schema_version': 1, 'probes': []}, [SPEC], [])
        with self.assertRaisesRegex(ValueError, 'HTTPS upgrade probe'):
            probes.validate({'schema_version': 1, 'probes': [{'project': 'demo', 'url': 'http://127.0.0.1:18080/health'}]}, [SPEC], [ROUTE])

    def test_stopped_project_retains_its_profile_without_being_started_or_probed(self):
        profile = {'schema_version': 1, 'probes': [PROBE]}
        self.assertEqual(probes.validate(profile, [], [ROUTE], known_projects={'demo'}), [])

    def test_probe_rejects_external_ports_credentials_queries_and_shell_fields(self):
        for changes in [{'url': 'http://169.254.169.254/'}, {'url': 'http://127.0.0.1:8088/'}, {'url': 'https://unknown.com/'}, {'url': 'https://user:password@app.your-domain.com/'}, {'url': 'https://app.your-domain.com/?key=secret'}, {'command': 'echo unsafe'}, {'status': 302}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                probes.validate({'schema_version': 1, 'probes': [dict(PROBE, **changes)]}, [SPEC], [ROUTE])

    def test_json_assertions_and_stable_persisted_data_are_returned_without_writes(self):
        connection = Mock(); response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"service":"backend","postgres":42,"redis":"42"}'
        with patch.object(probes, 'LocalHTTPS', return_value=connection):
            self.assertEqual(probes.run(PROBE), {'postgres': 42, 'redis': '42'})
        self.assertEqual(connection.request.call_args.args, ('GET', '/api/upgrade-check'))
        self.assertEqual(response.read.call_args.args, (65537,))
        connection.close.assert_called_once()

    def test_redirect_large_body_and_dependency_failure_are_not_success(self):
        for status, body in [(302, b''), (503, b''), (200, b'x' * 65537), (200, b'{"service":"wrong"}')]:
            connection = Mock(); connection.getresponse.return_value.status = status; connection.getresponse.return_value.read.return_value = body
            with patch.object(probes, 'LocalHTTPS', return_value=connection), self.assertRaises(ValueError):
                probes.run(PROBE)

    def test_https_connects_only_loopback_and_verifies_real_sni(self):
        context = Mock(); raw = Mock()
        connection = probes.LocalHTTPS('app.your-domain.com', 443, timeout=5, context=context)
        with patch.object(probes.socket, 'create_connection', return_value=raw) as connect:
            connection.connect()
        connect.assert_called_once_with(('127.0.0.1', 443), 5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname='app.your-domain.com')
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError('untrusted certificate')
        with patch.object(probes.socket, 'create_connection', return_value=raw), self.assertRaises(ssl.SSLCertVerificationError):
            connection.connect()
        raw.close.assert_called_once()

    def test_post_upgrade_missing_stopped_app_or_changed_data_fails(self):
        app = {'app': SPEC['app'], 'desired': 'stopped', 'fingerprint': 'fixed', 'instance_id': None}
        value = {'logical': {}, 'resources': {}, 'apps': [app], 'instances': {}, 'routes': [], 'probes': []}
        with patch.object(state, 'logical_state', return_value={}), patch.object(state, 'resource_identity', return_value={}), \
             patch.object(upgrade, 'request', side_effect=[{'draining': True, 'quiescent': True}, []]):
            with self.assertRaisesRegex(ValueError, 'inventory'): upgrade.verify_once(value)
        value.update(apps=[], probes=[{'check': PROBE, 'before': {'postgres': 42, 'redis': '42'}}])
        with patch.object(state, 'logical_state', return_value={}), patch.object(state, 'resource_identity', return_value={}), \
             patch.object(upgrade, 'request', side_effect=[{'draining': True, 'quiescent': True}, [], {'routes': []}]), patch.object(upgrade, 'inspect_apps', return_value={}), patch.object(probes, 'run', return_value={'postgres': 0, 'redis': '0'}):
            with self.assertRaisesRegex(ValueError, 'persisted-data'): upgrade.verify_once(value)

    def test_verification_failure_keeps_private_diagnostics_and_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            upgrade.write_private(root / upgrade.BASELINE, {})
            with patch.object(upgrade, 'verify_once', side_effect=ValueError('application inventory changed')), patch.object(upgrade.time, 'monotonic', side_effect=[0, 11]):
                with self.assertRaises(RuntimeError): upgrade.verify(root, 10)
            failure = root / 'upgrade-failure.json'
            self.assertEqual(failure.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(failure.read_text())['phase'], 'verify')
            self.assertFalse((root / 'upgrade-report.json').exists())

    def test_private_profiles_reject_symlinks_and_world_readable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'checks'; path.write_text('{}'); path.chmod(0o644)
            with self.assertRaises(ValueError): state.private_json(path)
            path.chmod(0o600)
            self.assertEqual(state.private_json(path), {})
            link = Path(temp) / 'linked'; link.symlink_to(path)
            with self.assertRaises(ValueError): state.private_json(link)

    def test_existing_management_token_is_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'token'
            with self.assertRaises(FileNotFoundError): state.check_token(path)
            path.write_text('test-token-that-is-long-enough-1234567890\n'); path.chmod(0o600)
            state.check_token(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError): state.check_token(path)
            path.chmod(0o600); path.write_text('short')
            with self.assertRaises(ValueError): state.check_token(path)


if __name__ == '__main__': unittest.main()
