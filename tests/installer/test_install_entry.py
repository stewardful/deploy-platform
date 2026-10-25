"""Released shell entry: pinned CLI, no duplicate install engine or host changes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'distribution/cli'))
import installer_entry as entry

CLI = b'''import json,os,sys
with open(os.environ['CLI_TRACE'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('FAIL_AT')==sys.argv[1]: raise SystemExit(3)
'''


class InstallEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bin=self.root/'bin';self.bin.mkdir()
        self.temporary=self.root/'temporary';self.temporary.mkdir()
        self.cli=self.root/'cloudctl';self.cli.write_bytes(CLI)
        (self.root/'cloudctl.sha256').write_text(hashlib.sha256(CLI).hexdigest()+'  cloudctl\n')
        self.script=entry.build(self.root,'owner/repo','dev-42')
        self.functions=self.root/'functions.sh'
        self.functions.write_text(self.script.read_text().rsplit('main "$@"',1)[0])
        curl=self.bin/'curl'
        curl.write_text('#!'+sys.executable+'\n'+'''import json,os,shutil,sys
with open(os.environ['CURL_TRACE'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
if os.environ.get('CURL_FAIL'): raise SystemExit(22)
shutil.copyfile(os.environ['PAYLOAD'],sys.argv[sys.argv.index('-o')+1])
''');curl.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],TMPDIR=str(self.temporary),
                      CLI_TRACE=str(self.root/'cli.log'),CURL_TRACE=str(self.root/'curl.log'),PAYLOAD=str(self.cli))

    def call(self,*args,setup='',env=None):
        # Replace only host probes: never touch the developer's OS, apt or /opt.
        harness='''source "$1"
shift
check_supported_os(){ return 0; }
existing_install(){ return 1; }
prepare_dependencies(){ return 0; }
id(){ printf '0\\n'; }
uname(){ printf 'x86_64\\n'; }
'''+setup+'\nmain "$@"\n'
        return subprocess.run(['bash','-c',harness,'test-entry',str(self.functions),*args],
                              env={**self.env,**(env or {})},capture_output=True,text=True,timeout=10)

    def calls(self):
        path=self.root/'cli.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_one_command_pins_cli_then_reuses_install_policy_health_and_version(self):
        result=self.call('--channel','dev','--run-id','43')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls(),[['install','--channel','dev','--repository','owner/repo','--run-id','43'],
                                      ['update','policy','--dev-auto-upgrade','off'],['status'],['version']])
        download=json.loads((self.root/'curl.log').read_text())
        self.assertEqual(download[-1],'https://github.com/owner/repo/releases/download/dev-42/cloudctl')
        self.assertIn('--max-filesize',download);self.assertNotIn('--insecure',download)
        self.assertIn('Installation verified',result.stdout)
        self.assertEqual(list(self.temporary.iterdir()),[])

    def test_hash_or_transport_failure_never_executes_download(self):
        self.cli.write_bytes(b'changed executable')
        result=self.call();self.assertNotEqual(result.returncode,0);self.assertEqual(self.calls(),[])
        self.assertIn('checksum mismatch',result.stderr)
        self.assertEqual(list(self.temporary.iterdir()),[])
        result=self.call(env={'CURL_FAIL':'1'})
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.calls(),[])
        self.assertEqual(list(self.temporary.iterdir()),[])

    def test_existing_or_unsupported_host_never_downloads_or_changes_policy(self):
        for setup in ['existing_install(){ return 0; }','check_supported_os(){ return 1; }',
                      'uname(){ printf "riscv64\\n"; }','id(){ printf "1000\\n"; }']:
            with self.subTest(setup=setup):
                result=self.call(setup=setup)
                self.assertNotEqual(result.returncode,0);self.assertEqual(self.calls(),[])
                self.assertFalse((self.root/'curl.log').exists())

    def test_failure_is_not_reported_as_success_or_retried(self):
        for command in ('install','update','status','version'):
            (self.root/'cli.log').unlink(missing_ok=True)
            result=self.call(env={'FAIL_AT':command})
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('Installation verified',result.stdout)
            self.assertEqual(sum(call[0]=='install' for call in self.calls()),1)
            if command=='install':self.assertEqual(len(self.calls()),1)
            self.assertEqual(list(self.temporary.iterdir()),[])

    def test_argument_errors_and_help_have_no_host_side_effects(self):
        for args in [('--channel','alpha'),('--run-id','0'),('--channel','stable','--run-id','42'),
                     ('--version','1.0.0'),('--channel','dev','--yes'),('--channel',)]:
            with self.subTest(args=args):self.assertNotEqual(self.call(*args).returncode,0)
        result=self.call('--help',setup='id(){ return 9; }')
        self.assertEqual(result.returncode,0);self.assertIn('Usage:',result.stdout)
        self.assertEqual(self.calls(),[]);self.assertFalse((self.root/'curl.log').exists())

    def test_formal_version_uses_the_same_entry_and_explicit_cli_option(self):
        result=self.call('--channel','stable','--version','1.2.3')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls()[0][-2:],['--version','1.2.3'])
        script=entry.render('owner/repo','v1.2.3',CLI).decode()
        self.assertIn("channel='stable'",script);self.assertIn('/v1.2.3/cloudctl',script)

    def test_published_entry_and_checksums_are_exactly_bound_to_cli_and_tag(self):
        self.assertEqual(len(entry.verify(self.root,'owner/repo','dev-42')),2)
        for repo,tag in [('owner/repo','dev-latest'),('owner/$(id)','dev-42'),('owner/repo','dev-18446744073709551616')]:
            with self.assertRaises(ValueError):entry.render(repo,tag,CLI)
        with self.assertRaises(ValueError):entry.verify(self.root,'owner/repo','dev-43')
        self.cli.unlink();self.cli.symlink_to(self.script)
        with self.assertRaises(ValueError):entry.verify(self.root,'owner/repo','dev-42')
        self.cli.unlink();self.cli.write_bytes(CLI)
        self.script.write_text(self.script.read_text()+'echo extra\n')
        with self.assertRaises(ValueError):entry.verify(self.root,'owner/repo','dev-42')
