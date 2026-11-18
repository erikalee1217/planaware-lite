"""Offline adapter tests; fixture mutations and acquisition clocks are synthetic."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
import io
import json
from pathlib import Path
import tempfile
import unittest

from planaware.n1 import FrozenSnapshot, N1Scope, ValidationError, freeze_n1_fixture, main


FIXTURES = Path(__file__).parent / 'fixtures'
TEXT = (FIXTURES / 'n1_298_observed.json').read_text()
SCOPE = N1Scope.from_json((FIXTURES / 'n1_298_scope.json').read_text())
REQUESTED = '2026-11-17T19:42:00+08:00'
FETCHED = '2026-11-17T19:42:06+08:00'


def freeze(text=TEXT, **kwargs):
    args = dict(scope=SCOPE, requested_at=REQUESTED, fetched_at=FETCHED, run_key='test-run')
    args.update(kwargs)
    return freeze_n1_fixture(text, **args)


class N1AdapterTests(unittest.TestCase):
    def rows(self, records):
        return json.loads(freeze(json.dumps(records)).records_json)

    def test_observed_fixture_order_values_and_clocks(self):
        raw = json.loads(TEXT)
        rows = json.loads(freeze().records_json)
        self.assertEqual([row['stop_uid'] for row in rows], ['TPE40830', 'TPE40828'])
        self.assertEqual([row['estimate_seconds'] for row in rows], [4535, 4390])
        for row, original in zip(rows, raw):
            self.assertEqual(row['raw_payload'], original)
            self.assertEqual(row['source_updated_at'], original['SrcUpdateTime'])
            self.assertEqual(row['tdx_updated_at'], original['UpdateTime'])
            self.assertEqual(row['eta_status'], 'not_departed')
            self.assertIn('not_a_normal_status_prediction', row['quality_flags'])
            self.assertNotIn('vehicle_id', row)
            self.assertNotIn('travel_seconds', row)

    def test_missing_null_negative_and_zero_estimates(self):
        for value in ('missing', None, -1, 0):
            with self.subTest(value=value):
                raw = json.loads(TEXT)[0]
                if value == 'missing':
                    del raw['EstimateTime']
                else:
                    raw['EstimateTime'] = value
                row = self.rows([raw])[0]
                self.assertEqual(row['estimate_seconds'], 0 if value == 0 else None)
                self.assertEqual(row['raw_payload'], raw)
                if value == 'missing':
                    self.assertIn('missing_estimate', row['quality_flags'])
                elif value is None:
                    self.assertIn('null_estimate', row['quality_flags'])
                elif value == -1:
                    self.assertIn('negative_estimate_preserved_in_raw', row['quality_flags'])

    def test_statuses_are_preserved_without_promoting_unusable_predictions(self):
        labels = ['normal', 'not_departed', 'not_stopping', 'service_ended', 'not_operating']
        for code in [0, 1, 2, 3, 4, 99, None, 'missing']:
            with self.subTest(code=code):
                raw = json.loads(TEXT)[0]
                if code == 'missing':
                    del raw['StopStatus']
                else:
                    raw['StopStatus'] = code
                row = self.rows([raw])[0]
                self.assertEqual(row['stop_status'], None if code == 'missing' else code)
                self.assertEqual(row['eta_status'], labels[code] if type(code) is int and code < 5 else 'unknown')
                self.assertEqual('not_a_normal_status_prediction' in row['quality_flags'], code != 0)
                if code == 99:
                    self.assertIn('unrecognized_stop_status', row['quality_flags'])

    def test_identity_and_numeric_type_errors_reject_whole_snapshot(self):
        changes = [('Direction', 1), ('Direction', True), ('Direction', '0'),
                   ('StopUID', None), ('StopUID', 'wrong'), ('RouteUID', 'wrong'),
                   ('EstimateTime', True), ('EstimateTime', 1.5), ('EstimateTime', '120'),
                   ('StopStatus', -1), ('StopStatus', 32768), ('StopStatus', False),
                   ('StopName', 'wrong'), ('StopID', 123)]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                records = json.loads(TEXT)
                records[1][field] = value
                with self.assertRaises(ValidationError):
                    freeze(json.dumps(records))

    def test_source_time_missing_is_not_replaced_by_tdx_or_fetch_time(self):
        for missing in (True, False):
            raw = json.loads(TEXT)[0]
            if missing:
                del raw['SrcUpdateTime']
            else:
                raw['SrcUpdateTime'] = None
            row = self.rows([raw])[0]
            self.assertIsNone(row['source_updated_at'])
            self.assertEqual(row['tdx_updated_at'], raw['UpdateTime'])
            self.assertIn('missing_source_updated_at' if missing else 'null_source_updated_at', row['quality_flags'])

    def test_only_offset_aware_supported_precision_times_are_accepted(self):
        for value in ('2026-11-17T19:42:00', 'bad', '', '2026-11-17T19:42:00.1234567Z',
                      '2026-11-17T19:42:00+08:99'):
            raw = json.loads(TEXT)[0]
            raw['SrcUpdateTime'] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.rows([raw])
        raw['SrcUpdateTime'] = '2026-11-18T00:00:00.123456Z'  # Source clock skew is retained.
        self.assertEqual(self.rows([raw])[0]['source_updated_at'], raw['SrcUpdateTime'])
        with self.assertRaises(ValidationError):
            freeze(fetched_at='2026-11-17T19:41:59+08:00')

    def test_envelope_and_json_corruption_are_rejected(self):
        for text in ('{}', '{"Items":[]}', '{"LiveEvents":[]}', 'null', '[1]',
                     '[', '[{"x":1,"x":2}]', '[NaN]', '["\\u0000"]', '["\\ud800"]'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                freeze(text)
        self.assertEqual(json.loads(freeze('[]').records_json), [])

    def test_frozen_roundtrip_is_immutable_and_rejects_tampering(self):
        saved = freeze()
        self.assertEqual(FrozenSnapshot.from_json(saved.to_json()), saved)
        self.assertEqual(FrozenSnapshot.from_json(saved.to_json()).to_sql(), saved.to_sql())
        with self.assertRaises(FrozenInstanceError):
            saved.run_key = 'different'
        artifact = json.loads(saved.to_json())
        artifact['records'][0]['estimate_seconds'] = 1
        with self.assertRaises(ValidationError):
            FrozenSnapshot.from_json(json.dumps(artifact))
        artifact = json.loads(saved.to_json())
        artifact['records'][0]['direction'] = False  # False == 0 must not bypass validation.
        with self.assertRaises(ValidationError):
            FrozenSnapshot.from_json(json.dumps(artifact))
        artifact = json.loads(saved.to_json())
        artifact['format_version'] = 'future'
        with self.assertRaises(ValidationError):
            FrozenSnapshot.from_json(json.dumps(artifact))

    def test_distinct_acquisitions_receive_distinct_keys_even_when_identical(self):
        first, second = freeze(run_key=None), freeze(run_key=None)
        self.assertNotEqual(first.run_key, second.run_key)
        self.assertEqual(first.records_json, second.records_json)
        self.assertEqual(freeze(), freeze())

    def test_scope_is_configurable_and_frozen_without_verified_route_assumptions(self):
        alternate = N1Scope('SYNTHETIC_ROUTE', 1, ('SYNTHETIC_STOP',))
        raw = {'RouteUID': alternate.route_uid, 'Direction': 1, 'StopUID': alternate.stop_uids[0]}
        saved = freeze(json.dumps([raw]), scope=alternate)
        self.assertEqual(FrozenSnapshot.from_json(saved.to_json()).scope, alternate)
        self.assertEqual(json.loads(saved.records_json)[0]['route_uid'], alternate.route_uid)
        with self.assertRaises(ValidationError):
            freeze(json.dumps([raw]))
        for config in ({'route_uid': '', 'direction': 0, 'stop_uids': ['x']},
                       {'route_uid': 'x', 'direction': True, 'stop_uids': ['x']},
                       {'route_uid': 'x', 'direction': 0, 'stop_uids': []},
                       {'route_uid': 'x', 'direction': 0, 'stop_uids': ['x', 'x']}):
            with self.subTest(config=config), self.assertRaises(ValidationError):
                N1Scope.from_dict(config)

    def test_unknown_raw_fields_and_decimal_precision_are_preserved(self):
        text = TEXT.replace('"EstimateTime": 4535,',
                            '"unknown": {"number": 0.12345678901234567890123456789}, "EstimateTime": 4535,')
        saved = freeze(text)
        self.assertIn('0.12345678901234567890123456789', saved.records_json)
        self.assertEqual(FrozenSnapshot.from_json(saved.to_json()), saved)

    def test_cli_validates_before_creating_and_never_overwrites_frozen_file(self):
        work = Path(__file__).resolve().parents[1] / 'work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as directory:
            output = Path(directory) / 'frozen.json'
            args = ['freeze', str(FIXTURES / 'n1_298_observed.json'), '--config',
                    str(FIXTURES / 'n1_298_scope.json'), '--output', str(output),
                    '--requested-at', REQUESTED, '--fetched-at', FETCHED]
            self.assertEqual(main(args), 0)
            original = output.read_text()
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 2)
            self.assertEqual(output.read_text(), original)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['sql', str(output)]), 0)
            self.assertEqual(buffer.getvalue().strip(), FrozenSnapshot.from_json(original).to_sql())
            invalid_output = Path(directory) / 'invalid.json'
            invalid_args = args.copy()
            invalid_args[invalid_args.index('--output') + 1] = str(invalid_output)
            invalid_args[invalid_args.index('--fetched-at') + 1] = 'bad'
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(invalid_args), 2)
            self.assertFalse(invalid_output.exists())


if __name__ == '__main__':
    unittest.main()
