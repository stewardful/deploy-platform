"""CLI vocabulary tests; no host changes and no network."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'distribution/installer/lib'))
import node_cli as cli


class NodeCliTests(unittest.TestCase):
    def test_canonical_routes_preserve_arguments(self):
        for args, forwarded in [
            (['deploy', '-f', 'with spaces.toml'], ['apply', '-f', 'with spaces.toml']),
            (['preview', '-f', 'a.json'], ['plan', '-f', 'a.json']),
            (['agent-health'], ['doctor']),
            (['secret', 'create', '--project', 'demo', '--name', 'key', '--file', 'private'], ['secret-create', '--project', 'demo', '--name', 'key', '--file', 'private']),
            (['secret', 'list', '--project', 'demo'], ['secrets', '--project', 'demo']),
            (['route', 'delete', '--domain', 'a.test', '--expected-revision', '1'], ['route-delete', '--domain', 'a.test', '--expected-revision', '1']),
            (['volume', 'list', '--project', 'demo', '--name', 'db'], ['volumes', '--project', 'demo', '--name', 'db']),
            (['logs', '--project', 'demo', '--name', 'web'], ['logs', '--project', 'demo', '--name', 'web']),
        ]:
            with self.subTest(args=args), patch.object(cli, 'agent') as agent:
                cli.main(args); agent.assert_called_once_with(forwarded)

    def test_removed_names_and_ambiguous_logs_rejected(self):
        for command in ('apply', 'list', 'verify', 'doctor', 'plan', 'secret-create', 'secrets', 'volumes', 'routes', 'route-set', 'route-delete', 'smoke', 'stateful-smoke', 'fullstack-smoke'):
            with self.subTest(command=command), self.assertRaises(ValueError): cli.main([command])
        for args in (['logs'], ['logs', 'agent']):
            with self.assertRaises(ValueError): cli.main(args)

    def test_specific_help_and_argument_rejection(self):
        for args, text in [(['help', 'preview'], 'No live-state comparison'), (['status', '--help'], 'full installed node'),
                           (['version', '--help'], 'manifest'), (['system-logs', '--help'], 'service'),
                           (['secret', '--help'], 'list,create'), (['test', '--help'], 'create resources')]:
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                try: cli.main(args)
                except SystemExit as error: self.assertEqual(error.code, 0)
                self.assertIn(text, output.getvalue())
        for args in (['version', 'extra'], ['status', '--unexpected'], ['system-logs'], ['system-logs', 'evil.service'], ['test', 'smoke', '--bad']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error: cli.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_version_is_platform_inventory_not_only_agent(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(cli, 'ROOT', Path(temp)):
            (Path(temp)/'manifest.json').write_text(json.dumps({'version': '0.5.3', 'arch': 'amd64', 'package_kind': 'platform-update', 'build': {'channel': 'dev', 'commit': 'a'*40}, 'runtime': {'containerd': '2.4.1', 'archive_url_template': 'not an installed version'}, 'proxy': {'version': '2.11.7'}}))
            with patch.object(cli.subprocess, 'check_output', return_value='agent 0.5.3\n'), contextlib.redirect_stdout(io.StringIO()) as output:
                cli.main(['version'])
            value = json.loads(output.getvalue())
            self.assertEqual(value['version'], '0.5.3'); self.assertEqual(value['build']['channel'], 'dev')
            self.assertEqual(value['components']['controlplane'], '0.5.3')
            self.assertEqual(value['components']['runtime'], {'containerd': '2.4.1'})

    def test_logs_and_tests_have_separate_fixed_targets(self):
        with patch.object(cli, 'execute') as execute:
            cli.main(['system-logs', 'update'])
            self.assertEqual(execute.call_args.args[0], ['/usr/bin/journalctl', '-u', 'deploy-update.service', '-n', '100', '--no-pager'])
        with patch.object(cli, 'execute') as execute:
            cli.main(['test', 'fullstack', '--image', 'demo:v1'])
            self.assertEqual(execute.call_args.args[0][-2:], ['--image', 'demo:v1'])

    def test_forwarded_help_uses_cloudctl_names(self):
        with patch.object(cli.subprocess, 'check_output', return_value='Usage: agentctl apply [OPTIONS] --file <FILE>\n'), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(['deploy', '--help'])
        self.assertIn('Usage: cloudctl deploy', output.getvalue())
        self.assertNotIn('agentctl apply', output.getvalue())
