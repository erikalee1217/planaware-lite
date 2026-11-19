"""All HTTP is mocked; no real credentials, API requests or existing databases."""
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from planaware.live import (MAX_BYTES, NoRedirect, PipelineError, fetch_once,
                            psql_executor, replay, save_atomic)
from planaware.n1 import FrozenSnapshot, main
from test_n1_adapter import TEXT, SCOPE, freeze

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = {'TDX_CLIENT_ID': 'synthetic-client-private', 'TDX_CLIENT_SECRET': 'synthetic-secret-"\\private'}
TOKEN = 'synthetic.access.token'


def response(text, *, status=200, content_type='application/json'):
    result = Mock()
    result.status = status
    result.headers = Message()
    result.headers['Content-Type'] = content_type
    result.read.side_effect = lambda limit: text.encode('utf-8')[:limit]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def http_mock(text=TEXT):
    opener = Mock()
    opener.open.side_effect = [response(json.dumps({'access_token': TOKEN, 'token_type': 'Bearer'})), response(text)]
    return opener


class LiveTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / 'work')
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / 'frozen.json'
        self.environment = patch.dict(os.environ, CREDENTIALS)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fetch(self, *, opener=None, execute=None, **changes):
        args = dict(scope=SCOPE, city='Taipei', max_records=2, output=self.output,
                    confirm_free_quota=True, opener=opener or http_mock(),
                    execute_sql=execute or Mock(return_value='17'))
        args.update(changes)
        return fetch_once(**args)

    def test_success_authentication_bounded_query_artifact_before_database_and_safe_report(self):
        opener = http_mock()
        def execute(sql):
            self.assertTrue(self.output.exists())
            self.assertEqual(FrozenSnapshot.from_json(self.output.read_text()).to_sql(), sql)
            return '17'
        result = self.fetch(opener=opener, execute=execute)
        self.assertEqual(result['database_run_id'], 17)
        self.assertEqual(result['record_count'], 2)
        self.assertEqual((result['authentication_requests'], result['data_requests']), (1, 1))
        self.assertEqual(opener.open.call_count, 2)
        auth, data = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(auth.get_method(), 'POST')
        self.assertEqual(parse_qs(auth.data.decode())['grant_type'], ['client_credentials'])
        self.assertEqual(data.get_header('Authorization'), 'Bearer ' + TOKEN)
        query = parse_qs(urlparse(data.full_url).query)
        self.assertEqual(query['$top'], ['3'])
        self.assertIn("RouteUID eq 'TPE11023'", query['$filter'][0])
        self.assertIn("StopUID eq 'TPE40828'", query['$filter'][0])
        for value in [*CREDENTIALS.values(), TOKEN]:
            self.assertNotIn(value, self.output.read_text() + str(result))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o400)

    def test_missing_credentials_and_unconfirmed_quota_do_not_request(self):
        for kwargs in ({'confirm_free_quota': False}, {}):
            opener = http_mock()
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(PipelineError):
                self.fetch(opener=opener, **kwargs)
            opener.open.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_http_failures_at_either_stage_have_no_retries_or_secret_diagnostics(self):
        for stage in (0, 1):
            for code in (301, 401, 403, 429, 500, 503):
                with self.subTest(stage=stage, code=code):
                    opener = http_mock()
                    failure = HTTPError('https://tdx.transportdata.tw/', code, TOKEN, {}, io.BytesIO(TOKEN.encode()))
                    opener.open.side_effect = ([response(json.dumps({'access_token': TOKEN, 'token_type': 'Bearer'}))] if stage else []) + [failure]
                    executor = Mock()
                    with self.assertRaises(PipelineError) as raised:
                        self.fetch(opener=opener, execute=executor)
                    self.assertIn(str(code), str(raised.exception))
                    self.assertNotIn(TOKEN, str(raised.exception))
                    self.assertEqual(opener.open.call_count, stage + 1)
                    executor.assert_not_called()
                    self.assertFalse(self.output.exists())

    def test_network_timeout_and_read_failures_are_sanitized(self):
        for error in (URLError(TOKEN), TimeoutError(TOKEN), OSError(TOKEN)):
            opener = http_mock()
            opener.open.side_effect = error
            with self.assertRaises(PipelineError) as raised:
                self.fetch(opener=opener)
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_invalid_or_unbounded_responses_never_publish_or_write(self):
        records = json.loads(TEXT)
        records[1]['Direction'] = '0'
        samples = ['{', '{"Items":[]}', json.dumps(records), TEXT[:-1] + ', ' + TEXT[1:], ' ' * (MAX_BYTES + 1)]
        for text in samples:
            with self.subTest(text=text[:20]):
                executor = Mock()
                with self.assertRaises(PipelineError):
                    self.fetch(opener=http_mock(text), execute=executor)
                executor.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_invalid_authentication_and_content_type_stop_before_data(self):
        for auth in ('bad', '{}', '{"access_token":"x\\nheader","token_type":"Bearer"}'):
            opener = http_mock()
            opener.open.side_effect = [response(auth)]
            with self.assertRaises(PipelineError):
                self.fetch(opener=opener)
            self.assertEqual(opener.open.call_count, 1)
        opener.open.side_effect = [response('html', content_type='text/html')]
        with self.assertRaises(PipelineError):
            self.fetch(opener=opener)

    def test_atomic_publish_writes_read_only_snapshot_and_never_replaces(self):
        saved = freeze()
        save_atomic(saved, self.output)
        self.assertEqual(FrozenSnapshot.from_json(self.output.read_text()), saved)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o400)
        original = self.output.read_bytes()
        with self.assertRaises(PipelineError):
            save_atomic(freeze(run_key='different'), self.output)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.output.parent.glob('.n1-*')), [])

    def test_publish_failure_leaves_no_artifact_or_database_write(self):
        executor = Mock()
        with patch('planaware.live.os.link', side_effect=OSError(TOKEN)), self.assertRaises(PipelineError):
            self.fetch(execute=executor)
        executor.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob('.n1-*')), [])

    def test_existing_path_prevents_new_http_request(self):
        self.output.write_text('original')
        opener = http_mock()
        with self.assertRaises(PipelineError):
            self.fetch(opener=opener)
        opener.open.assert_not_called()
        self.assertEqual(self.output.read_text(), 'original')

    def test_database_failure_retains_artifact_and_replay_does_not_authenticate(self):
        with self.assertRaises(PipelineError) as raised:
            self.fetch(execute=Mock(side_effect=RuntimeError(TOKEN)))
        self.assertNotIn(TOKEN, str(raised.exception))
        artifact = self.output.read_bytes()
        with patch.dict(os.environ, {}, clear=True), patch('planaware.live.build_opener', side_effect=AssertionError('NO HTTP')):
            result = replay(self.output, Mock(return_value='17'))
        self.assertEqual(result['data_requests'], 0)
        self.assertEqual(result['authentication_requests'], 0)
        self.assertEqual(self.output.read_bytes(), artifact)

    def test_empty_snapshot_is_frozen_and_persisted(self):
        result = self.fetch(opener=http_mock('[]'))
        self.assertEqual(result['record_count'], 0)
        self.assertEqual(FrozenSnapshot.from_json(self.output.read_text()).records_json, '[]')

    def test_invalid_bounds_and_no_redirect(self):
        for changes in ({'max_records': 0}, {'max_records': 101}, {'city': '../other'}, {'timeout': 0}):
            opener = http_mock()
            with self.assertRaises(PipelineError):
                self.fetch(opener=opener, **changes)
            opener.open.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other/'))

    def test_psql_executor_uses_stdin_and_does_not_log_errors_or_inherit_tdx(self):
        execute = psql_executor('demo')
        with patch('planaware.live.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '17\n', '')) as process:
            self.assertEqual(execute('SELECT 1;'), '17')
            self.assertEqual(process.call_args.kwargs['input'], 'SELECT 1;')
            self.assertNotIn('TDX_CLIENT_SECRET', process.call_args.kwargs['env'])
            self.assertNotIn('SELECT 1;', process.call_args.args[0])
        with patch('planaware.live.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', TOKEN)), self.assertRaises(PipelineError) as raised:
            execute('SELECT 1;')
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_cli_fetch_and_replay_report_safe_json_and_sanitize_config_errors(self):
        config = ROOT / 'tests/fixtures/n1_298_scope.json'
        args = ['fetch-once', '--config', str(config), '--city', 'Taipei', '--max-records', '2',
                '--output', str(self.output), '--database', 'demo', '--confirm-free-quota']
        with patch('planaware.live.build_opener', return_value=http_mock()), patch('planaware.live.psql_executor', return_value=Mock(return_value='17')):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(buffer.getvalue())['data_requests'], 1)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['replay', str(self.output), '--database', 'demo']), 0)
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(args), 2)
        for secret in (*CREDENTIALS.values(), TOKEN):
            self.assertNotIn(secret, errors.getvalue())
