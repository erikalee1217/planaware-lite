"""Synthetic saved-row fixtures; the formatter never creates risk decisions."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from planaware.assessment import build_assessment, canonical_json
from planaware.brief import BriefError, build_brief, format_brief, format_buffer, load_assessment, main
from test_assessment import NOW, synthetic_context, synthetic_spec, synthetic_observation, source_spec


def saved_row(spec=None, context=None):
    row = build_assessment(spec or synthetic_spec(), context or synthetic_context())
    row['assessment_id'] = 1  # Synthetic identity, shaped like an exported DB row.
    return json.loads(canonical_json(row))


class JourneyBriefTests(unittest.TestCase):
    def test_on_track_is_conditional_and_preserves_small_remaining_buffer(self):
        row = saved_row(synthetic_spec(latest_arrival=NOW + timedelta(minutes=24)))
        brief, text = build_brief(row), format_brief(row)
        self.assertEqual(brief['status'], 'on_track')
        self.assertEqual(brief['minimum_buffer'], '+1 min')
        self.assertIn('Conditionally feasible', text)
        self.assertIn('boarding is not guaranteed', text)
        self.assertIn('Assumed latest arrival for boarding', text)
        self.assertIn('Synthetic', text)
        self.assertIn('Next check:', text)
        self.assertNotIn('operator-confirmed', text)

    def test_overlap_and_deadline_missed_explain_risk_without_confirming_missed_service(self):
        for spec, reason in [(synthetic_spec(latest_arrival=NOW + timedelta(minutes=26)), 'deadline_overlap'),
                             (synthetic_spec(earliest_arrival=NOW + timedelta(minutes=26), latest_arrival=NOW + timedelta(minutes=27)), 'deadline_missed')]:
            with self.subTest(reason=reason):
                row = saved_row(spec)
                brief, text = build_brief(row), format_brief(row)
                self.assertEqual((brief['status'], brief['reason_code']), ('at_risk', reason))
                self.assertNotIn('definitely', text)
                self.assertNotIn('You missed', text)
                self.assertIn('Next check:', text)
        self.assertEqual(build_brief(saved_row(synthetic_spec(latest_arrival=NOW + timedelta(minutes=26))))['minimum_buffer'], '-1 min')

    def test_status_three_missing_eta_and_interval_stay_unknown(self):
        context = synthetic_context()
        context['observations'] = [synthetic_observation(status=3, estimate=None)]
        row = saved_row(source_spec(earliest_arrival=None, latest_arrival=None), context)
        brief, text = build_brief(row), format_brief(row)
        self.assertEqual(brief['status'], 'insufficient_evidence')
        self.assertEqual(brief['arrival_interval'], {'earliest': None, 'latest': None})
        self.assertIsNone(brief['boarding_deadline'])
        self.assertIsNone(brief['minimum_buffer_microseconds'])
        self.assertIn('ETA is missing or invalid', text)
        self.assertIn('StopStatus=3', text)
        self.assertIn('Synthetic N1 observation fixture', text)
        self.assertIn('Synthetic journey/timing assumptions', text)
        self.assertIn('Check whether the bus is operating', text)
        self.assertNotIn('source ETA=', text)

    def test_source_backed_observation_is_distinct_from_synthetic_references(self):
        context = synthetic_context()
        observation = synthetic_observation(status=3, estimate=None)
        # This is only a synthetic test of the saved-source provenance branch.
        # It is not a newly obtained real observation.
        observation['raw_payload'].pop('synthetic_test_only')
        context['observations'] = [observation]
        text = format_brief(saved_row(source_spec(earliest_arrival=None, latest_arrival=None), context))
        self.assertIn('Saved TDX N1 observation (source-backed)', text)
        self.assertIn('Synthetic journey/timing assumptions', text)

    def test_missing_requirements_and_stale_evidence_have_specific_explanations(self):
        for changes, phrase in [({'earliest_arrival': None}, 'missing or incomplete'),
                                ({'buffer_semantics': 'unknown'}, 'boarding allowance'),
                                ({'earliest_arrival': None, 'latest_arrival': None}, 'arrival interval')]:
            with self.subTest(changes=changes):
                self.assertIn(phrase, format_brief(saved_row(synthetic_spec(**changes))))
        context = synthetic_context()
        context['observations'] = [synthetic_observation(source_age=121)]
        self.assertIn('newer usable source observation', format_brief(saved_row(source_spec(), context)))

    def test_exact_sign_and_units_including_zero_and_microseconds(self):
        for value, expected in [(None, None), (0, '+0 s'), (1, '+0.000001 s'),
                                (-1, '-0.000001 s'), (60000000, '+1 min'),
                                (-60000000, '-1 min'), (60000001, '+60.000001 s')]:
            with self.subTest(value=value):
                self.assertEqual(format_buffer(value), expected)
        for value in (True, 1.0, '1'):
            with self.assertRaises(BriefError):
                format_buffer(value)

    def test_no_mutation_no_risk_calls_and_no_deadline_reconstruction(self):
        row = saved_row()
        # Leave the saved decision intact but remove derived values. The brief
        # must not reconstruct them from departure and buffer or change status.
        row['input_snapshot']['result']['boarding_deadline'] = None
        row['input_snapshot']['result']['minimum_buffer'] = None
        before = deepcopy(row)
        with (patch('planaware.assessment.evaluate_transfer', side_effect=AssertionError('NO EVALUATION')),
              patch('planaware.risk.evaluate_transfer', side_effect=AssertionError('NO EVALUATION'))):
            first = build_brief(row)
            self.assertEqual(first, build_brief(row))
            format_brief(row)
        self.assertEqual(row, before)
        self.assertEqual(first['status'], 'on_track')
        self.assertIsNone(first['boarding_deadline'])
        self.assertIsNone(first['minimum_buffer'])

    def test_saved_summary_and_inconsistent_status_are_rejected(self):
        with self.assertRaises(BriefError):
            build_brief({'status': 'on_track', 'minimum_buffer_microseconds': 60000000})
        row = saved_row()
        row['status'] = 'at_risk'
        with self.assertRaises(BriefError):
            build_brief(row)

    def test_read_interface_only_selects_by_validated_identity(self):
        statements = []
        row = saved_row()
        def execute(statement):
            statements.append(statement)
            return json.dumps(row)
        self.assertEqual(load_assessment(1, execute), row)
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith('SELECT '))
        for identity in (0, True, '1; DELETE'):
            with self.assertRaises(BriefError):
                load_assessment(identity, execute)
        self.assertEqual(len(statements), 1)

    def test_cli_text_and_json_from_export_or_read_only_database(self):
        root = Path(__file__).resolve().parents[1]
        (root / 'work').mkdir(exist_ok=True)
        rows = [saved_row(), saved_row(synthetic_spec(latest_arrival=NOW + timedelta(minutes=26)))]
        with tempfile.TemporaryDirectory(dir=root / 'work') as directory:
            path = Path(directory) / 'synthetic-assessments.json'
            path.write_text(json.dumps(rows))
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(main(['--input', str(path), '--format', 'json']), 0)
            self.assertEqual([row['status'] for row in json.loads(buffer.getvalue())], ['on_track', 'at_risk'])
            with patch('planaware.brief.psql_json_executor', return_value=lambda _: json.dumps(rows[0])), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--assessment-id', '1', '--database', 'demo']), 0)
            path.write_text(json.dumps([rows[0], {'status': 'on_track'}]))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--input', str(path)]), 2)
            self.assertEqual(output.getvalue(), '')
