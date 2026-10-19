"""Disposable-node entrypoint preserves argv/stdin without touching user nodes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT/'distribution/tests/node-exec.sh'


class NodeExecTests(unittest.TestCase):
    def test_private_stdin_and_arguments_survive_scope_entry_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root/'docker'
            docker.write_text('#!'+sys.executable+'\nimport json,sys\nprint(json.dumps({"argv":sys.argv[1:],"stdin":sys.stdin.read()}))\n')
            docker.chmod(0o755)
            payload = 'test-only stdin\nwith spaces\n'
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'])
            result = subprocess.run(['bash','-c','source "$1"; shift; node_exec "$@"','node-test',str(HELPER),'-i','-e','VALUE=has spaces','cloud-installer-test-123','printf','%s','$(never execute)'], input=payload, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value['stdin'],payload)
            self.assertEqual(value['argv'][:6],['exec','-i','-e','VALUE=has spaces','cloud-installer-test-123','/bin/sh'])
            self.assertEqual(value['argv'][-4:],['cloud-test-command','printf','%s','$(never execute)'])
            self.assertIn('/sys/fs/cgroup/init.scope/cgroup.procs',value['argv'][7])
            self.assertIn('TasksAccounting=yes',value['argv'][7])

    def test_unowned_nodes_and_unknown_options_are_rejected_before_docker(self):
        for args in (['production','true'],['cloud-installer-test-123;echo','true'],['--privileged','cloud-installer-test-123','true']):
            result = subprocess.run(['bash','-c','source "$1"; shift; node_exec "$@"','node-test',str(HELPER),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,2,result.stderr)
