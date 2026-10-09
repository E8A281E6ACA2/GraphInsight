#!/usr/bin/env python3
"""Offline S2-03a checks: never invokes Docker, SQLAlchemy or a live service."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch
import s203_isolated_env as runner

RUN_ID = 'audit203a'
PASSWORD = 'sentinel_secret_not_for_logs'
DSN = f'postgresql://s203:{PASSWORD}@127.0.0.1:5544/gi_s203_{RUN_ID}'

class IsolationTests(unittest.TestCase):
    def run_main(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), patch.object(runner, 'command', side_effect=AssertionError('subprocess forbidden')):
            code = runner.main(args)
        self.assertNotIn(PASSWORD, output.getvalue())
        return code, output.getvalue()

    def test_shared_port_rejected_before_any_subprocess(self):
        code, output = self.run_main(['guard-check', '--run-id', RUN_ID, '--pg-port', '5434', '--dsn', DSN.replace(':5544/', ':5434/')])
        self.assertEqual(code, 2)
        self.assertIn('PORT_FORBIDDEN', output)

    def test_legal_target_passes_without_connections(self):
        code, output = self.run_main(['guard-check', '--run-id', RUN_ID, '--dsn', DSN])
        self.assertEqual(code, 0)
        self.assertIn('ddl_calls": 0', output)
        self.assertIn('***', output)

    def test_shared_database_name_rejected(self):
        code, _ = self.run_main(['guard-check', '--run-id', RUN_ID, '--dsn', DSN.replace('gi_s203_' + RUN_ID, 'graphinsight_admin')])
        self.assertEqual(code, 2)

    def test_wrong_dialect_host_or_port_rejected(self):
        for dsn in [DSN.replace('postgresql:', 'sqlite:'), DSN.replace('127.0.0.1', 'localhost'), DSN.replace(':5544/', ':5545/'), DSN + '?sslmode=disable']:
            with self.subTest(dsn='redacted'):
                code, _ = self.run_main(['guard-check', '--run-id', RUN_ID, '--dsn', dsn])
                self.assertEqual(code, 2)

    def test_dry_run_all_actions_create_nothing(self):
        for action in ['up', 'migrate', 'seed', 'token', 'teardown']:
            code, output = self.run_main([action, '--dry-run'])
            self.assertEqual(code, 0)
            self.assertIn('resources_created": 0', output)
            self.assertIn('NOT_EXECUTED', output)

    def test_execution_requires_stage_b_authorization(self):
        for action in ['up', 'migrate', 'seed', 'token', 'teardown']:
            code, _ = self.run_main([action])
            self.assertEqual(code, 2)

    def test_wrong_authorization_never_degrades_to_plan(self):
        code, _ = self.run_main(['up', '--dry-run', '--authorize', 'WRONG'])
        self.assertEqual(code, 2)

    def test_repository_state_rejected_even_in_dry_run(self):
        target = runner.BACKEND / 'tests' / 'state.json'
        for extra in (['--dry-run'], ['--authorize', runner.AUTHORIZATION]):
            code, output = self.run_main(['up', '--state', str(target), *extra])
            self.assertEqual(code, 2)
            self.assertIn('STATE_PATH_INSIDE_REPOSITORY', output)

    def test_default_state_is_outside_repository(self):
        path = runner.default_state_path(RUN_ID)
        self.assertTrue(path.is_absolute())
        self.assertFalse(path.is_relative_to(runner.BACKEND.parent.resolve()))
        self.assertEqual(path.name, 'state.json')

    def test_credential_copies_are_git_ignored(self):
        paths = ['backend/tests/state.json', 'backend/tests/isolated.env', 'backend/tests/admin-token.txt']
        result = subprocess.run(['git', 'check-ignore', *paths], cwd=runner.BACKEND.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(result.stdout.splitlines()), set(paths))

    def test_ready_retries_then_succeeds(self):
        state = {'container': 'gi-s203-' + RUN_ID, 'run_id': RUN_ID}
        failed = subprocess.CompletedProcess([], 1)
        ready = subprocess.CompletedProcess([], 0)
        with patch.object(runner, 'command', side_effect=[failed, ready]) as call, patch.object(runner.time, 'sleep'):
            runner.wait_ready(state)
        self.assertEqual(call.call_count, 2)
        self.assertIn('pg_isready', call.call_args.args[0])

    def test_ready_timeout_is_nonzero_and_requires_teardown(self):
        state = {'container': 'gi-s203-' + RUN_ID, 'run_id': RUN_ID}
        with patch.object(runner.time, 'monotonic', side_effect=[0, 61]), patch.object(runner, 'command') as call:
            with self.assertRaisesRegex(runner.GuardError, 'PG_READY_TIMEOUT_RUN_TEARDOWN'):
                runner.wait_ready(state)
            call.assert_not_called()

    def test_cli_password_redacted(self):
        result = subprocess.run([sys.executable, str(runner.Path(runner.__file__)), 'guard-check', '--run-id', RUN_ID, '--dsn', DSN], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main(verbosity=2)
