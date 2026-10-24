"""A notification is a hint; root worker owns verification, local approval and once-per-build receipts."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'distribution/installer/lib'))
import notification_updates as notice
import update_worker as worker
import dev_update as dev
import platform_updates as cli

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for name,path in [('SPOOL',self.root),('CURRENT',self.root/'manifest.json'),('CONFIG',self.root/'updates.json')]:
            self.stack.enter_context(patch.object(worker,name,path))
        self.stack.enter_context(patch.object(worker,'ACTIVE_DETAILS',{}))
        worker.CURRENT.write_text(json.dumps({'version':'0.5.3','arch':'amd64','build':{'commit':'b'*40,'channel':'dev'}}))
        self.config(True)
        self.request={'channel':'dev-notice','repository':'owner/repo','run_id':'42','phase':'queued'}
        self.candidate={'channel':'dev','repository':'owner/repo','run_id':'42','commit':'a'*40,'version':'0.5.3','arch':'amd64'}
        self.discover=self.stack.enter_context(patch.object(dev,'discover',return_value=self.candidate))
        self.install=self.stack.enter_context(patch.object(dev,'execute'))

    def config(self,enabled):
        worker.CONFIG.write_text(json.dumps({'repository':'owner/repo','token_file':str(self.root/'unavailable-token'),'notifications':{'dev_auto_upgrade':enabled}}));worker.CONFIG.chmod(0o600)

    def test_default_off_and_disabled_queued_request_never_discovers_or_installs(self):
        for config in ({},{'notifications':{'dev_auto_upgrade':False}}):
            worker.CONFIG.write_text(json.dumps(dict(config,repository='owner/repo')))
            notice.execute(self.request)
        self.discover.assert_not_called();self.install.assert_not_called()
        self.assertFalse((self.root/'notification-dev.json').exists())

    def test_verified_build_is_claimed_then_uses_existing_dev_execution(self):
        notice.execute(self.request)
        self.assertEqual(notice.receipt('owner/repo')['phase'],'succeeded')
        self.install.assert_called_once()
        self.assertEqual(self.install.call_args.args[0],self.candidate)
        self.install.call_args.kwargs['before_install']()
        self.assertEqual((self.root/'notification-dev.json').stat().st_mode&0o777,0o600)

    def test_failed_build_is_not_retried_even_on_repeated_snapshot(self):
        self.install.side_effect=RuntimeError('installer failed')
        with self.assertRaises(RuntimeError):notice.execute(self.request)
        self.assertEqual(notice.receipt('owner/repo')['phase'],'failed')
        self.discover.reset_mock();notice.execute(self.request)
        self.discover.assert_not_called();self.assertEqual(self.install.call_count,1)

    def test_policy_is_rechecked_immediately_before_install(self):
        def install(candidate,*,before_install):
            self.config(False);before_install()
        self.install.side_effect=install
        with self.assertRaises(ValueError):notice.execute(self.request)
        self.assertEqual(notice.receipt('owner/repo')['phase'],'failed')

    def test_bad_hint_cannot_poison_verified_high_water_mark(self):
        notice.record('owner/repo','41','succeeded')
        self.discover.side_effect=ValueError('no trusted build')
        with self.assertRaises(ValueError):notice.execute(dict(self.request,run_id='999999999'))
        self.assertEqual(notice.receipt('owner/repo')['run_id'],'41');self.install.assert_not_called()

    def test_installed_commit_and_older_notifications_are_skipped(self):
        current=json.loads(worker.CURRENT.read_text());current['build']['commit']='a'*40;worker.CURRENT.write_text(json.dumps(current))
        notice.execute(self.request);self.install.assert_not_called()
        self.assertEqual(notice.receipt('owner/repo')['phase'],'skipped')
        notice.record('owner/repo','43','succeeded');self.discover.reset_mock();notice.execute(self.request);self.discover.assert_not_called()

    def test_delayed_notification_is_not_installed_over_newer_public_build(self):
        self.discover.side_effect=[self.candidate,dict(self.candidate,run_id='43')]
        notice.execute(self.request);self.install.assert_not_called()
        self.assertEqual(notice.receipt('owner/repo')['phase'],'skipped')

    def test_current_only_receipt_and_non_executable_request(self):
        for extra in ({'url':'https://evil/installer'},{'command':'sh'},{'repository':'other/repo'},{'run_id':'0'},{'run_id':None},{'phase':'installing'},{'channel':'stable'}):
            with self.assertRaises(ValueError):notice.execute(dict(self.request,**extra))
        self.install.assert_not_called()
        for value in ({},{'schema_version':0},{'schema_version':1,'repository':'owner/repo','run_id':'42','phase':'unknown'}):
            path=self.root/'notification-dev.json';path.write_text(json.dumps(value));path.chmod(0o600)
            with self.assertRaises(ValueError):notice.execute(self.request)

    def test_local_policy_cli_preserves_repository_and_token_but_never_prints_token(self):
        with patch.object(cli.os,'geteuid',return_value=0),contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(['update','policy','--dev-auto-upgrade','off'])
            self.assertFalse(notice.policy()[1]);self.assertNotIn('notifications',json.loads(worker.CONFIG.read_text()))
            cli.main(['update','policy','--dev-auto-upgrade','on'])
            self.assertTrue(notice.policy()[1]);self.assertNotIn('unavailable-token',out.getvalue())
        self.assertEqual(json.loads(worker.CONFIG.read_text())['token_file'],str(self.root/'unavailable-token'))
