"""Explicitly synthetic scenarios; source-shaped observations are synthetic too."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from planaware.assessment import (
    AssessmentError, AssessmentSpec, build_assessment, canonical_json, inspect_boarding_observation,
    to_utc_datetime,
)
from planaware.risk import RiskInputError

NOW = datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)


def synthetic_spec(**changes):
    return replace(AssessmentSpec(1, 1, NOW, 'synthetic-ref-v1', 'synthetic-assumptions-v1',
        'synthetic', NOW + timedelta(minutes=10), NOW + timedelta(minutes=20),
        'synthetic', 'SYNTHETIC arrival at target boarding location; includes prior durations',
        'boarding_only'), **changes)


def synthetic_context(route='SYNTHETIC_ROUTE', stop='SYNTHETIC_STOP'):
    reference = {'reference_data_version': 'synthetic-ref-v1',
        'assumption_version': 'synthetic-assumptions-v1',
        'valid_from': '2026-11-18', 'valid_to': '2026-11-18', 'basis': 'SYNTHETIC'}
    legs = [{'leg_id': 1, 'journey_id': 1, 'leg_order': 1, 'transport_mode': 'bus',
        'route_uid': route, 'direction': 1, 'origin_stop_uid': stop, 'destination_stop_uid': 'SYNTHETIC_DESTINATION',
        'planned_departure_at': NOW.isoformat(), 'planned_arrival_at': (NOW + timedelta(minutes=10)).isoformat(),
        'reference_details': reference},
        {'leg_id': 2, 'journey_id': 1, 'leg_order': 2, 'transport_mode': 'intercity_bus',
        'route_uid': 'SYNTHETIC_COACH', 'direction': 0, 'origin_stop_uid': 'SYNTHETIC_TERMINAL',
        'destination_stop_uid': 'SYNTHETIC_END', 'reference_details': deepcopy(reference),
        'planned_departure_at': (NOW + timedelta(minutes=30)).isoformat(),
        'boarding_deadline_at': None, 'boarding_deadline_source': None}]
    return {'journey': {'journey_id': 1, 'journey_name': 'SYNTHETIC', 'service_date': '2026-11-18'},
        'transfer': {'transfer_id': 1, 'journey_id': 1, 'from_leg_id': 1, 'to_leg_id': 2,
            'min_transfer_minutes': 5, 'assumption_source': 'SYNTHETIC boarding allowance; excludes prior durations'},
        'legs': legs, 'observations': []}


def synthetic_observation(identity=1, *, source_age=30, fetch_age=10, status=0, estimate=300):
    source = (NOW - timedelta(seconds=source_age)).isoformat() if source_age is not None else None
    return {'observation_id': identity, 'run_id': identity, 'record_index': 0,
        'source_system': 'tdx_bus_n1', 'route_uid': 'SYNTHETIC_ROUTE', 'direction': 1,
        'stop_uid': 'SYNTHETIC_STOP', 'stop_status': status, 'estimate_seconds': estimate,
        'source_updated_at': source, 'fetched_at': (NOW - timedelta(seconds=fetch_age)).isoformat(),
        'ingested_at': NOW.isoformat(), 'raw_payload': {'RouteUID': 'SYNTHETIC_ROUTE',
            'Direction': 1, 'StopUID': 'SYNTHETIC_STOP', 'StopStatus': status,
            'EstimateTime': estimate, 'SrcUpdateTime': source, 'synthetic_test_only': True}}


def source_spec(**changes):
    values = dict(scenario='source_backed', observation_ids=(1,), evidence_leg_id=1,
        evidence_stop_uid='SYNTHETIC_STOP', interval_basis='validated',
        interval_source='SYNTHETIC TEST ONLY: explicitly validated interval fixture, not a real model')
    values.update(changes)
    return synthetic_spec(**values)


class AssessmentUnitTests(unittest.TestCase):
    def test_synthetic_feasible_risk_and_missing_interval_mapping(self):
        cases = [(synthetic_spec(), 'on_track', 'within_buffer'),
            (synthetic_spec(latest_arrival=NOW + timedelta(minutes=25)), 'at_risk', 'deadline_overlap'),
            (synthetic_spec(earliest_arrival=NOW + timedelta(minutes=26), latest_arrival=NOW + timedelta(minutes=27)), 'at_risk', 'deadline_missed'),
            (synthetic_spec(earliest_arrival=None, latest_arrival=None), 'insufficient_evidence', 'missing_arrival_interval')]
        for spec, status, reason in cases:
            with self.subTest(reason=reason):
                result = build_assessment(spec, synthetic_context())
                self.assertEqual((result['status'], result['reason_code']), (status, reason))
                self.assertNotEqual(result['status'], 'missed_deadline')

    def test_invalid_interval_is_explicit_error(self):
        for spec in (synthetic_spec(earliest_arrival=NOW + timedelta(hours=1)),
                     synthetic_spec(latest_arrival=NOW.replace(tzinfo=None))):
            with self.assertRaises(RiskInputError):
                build_assessment(spec, synthetic_context())

    def test_walking_not_added_twice_and_unknown_walking_is_unusable(self):
        context = synthetic_context()
        context['legs'][0].update(transport_mode='walking', estimated_duration_seconds=900,
                                  duration_assumption_source='SYNTHETIC already in interval')
        result = build_assessment(synthetic_spec(), context)
        self.assertEqual(result['input_snapshot']['result']['minimum_buffer'], 300000000)
        context['legs'][0]['estimated_duration_seconds'] = None
        result = build_assessment(synthetic_spec(), context)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIn('unknown_walking_duration', result['input_snapshot']['quality_issues'])

    def test_cutoff_uses_earlier_deadline_not_sum_and_unknown_semantics_are_missing(self):
        context = synthetic_context()
        context['legs'][1].update(boarding_deadline_at=(NOW + timedelta(minutes=24)).isoformat(),
                                  boarding_deadline_source='SYNTHETIC independent boarding cutoff')
        result = build_assessment(synthetic_spec(), context)
        self.assertEqual(result['input_snapshot']['result']['minimum_buffer'], 240000000)
        result = build_assessment(synthetic_spec(buffer_semantics='unknown'), context)
        self.assertEqual(result['reason_code'], 'missing_boarding_buffer')

    def test_fresh_normal_eta_never_constructs_downstream_interval(self):
        context = synthetic_context()
        context['observations'] = [synthetic_observation()]
        result = build_assessment(source_spec(earliest_arrival=None, latest_arrival=None), context)
        self.assertEqual(result['reason_code'], 'missing_arrival_interval')
        result = build_assessment(source_spec(interval_basis='unknown'), context)
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_status_three_missing_estimate_and_stale_or_missing_clocks(self):
        for changes, reason in [({'status': 3, 'estimate': None}, 'evidence_unusable'),
                                ({'source_age': 121}, 'evidence_stale'),
                                ({'source_age': None}, 'evidence_unusable')]:
            with self.subTest(changes=changes):
                context = synthetic_context()
                context['observations'] = [synthetic_observation(**changes)]
                result = build_assessment(source_spec(), context)
                self.assertEqual((result['status'], result['reason_code']), ('insufficient_evidence', reason))

    def test_freshness_boundary_is_configurable_not_an_official_guarantee(self):
        context = synthetic_context()
        context['observations'] = [synthetic_observation(source_age=120)]
        self.assertEqual(build_assessment(source_spec(), context)['status'], 'on_track')
        self.assertEqual(build_assessment(source_spec(freshness_seconds=119), context)['reason_code'], 'evidence_stale')

    def test_boarding_inspection_matches_assessment_evidence(self):
        wrong_route = synthetic_observation()
        wrong_route['route_uid'] = wrong_route['raw_payload']['RouteUID'] = 'WRONG_ROUTE'
        wrong_direction = synthetic_observation()
        wrong_direction['direction'] = wrong_direction['raw_payload']['Direction'] = 0
        wrong_stop = synthetic_observation()
        wrong_stop['stop_uid'] = wrong_stop['raw_payload']['StopUID'] = 'SYNTHETIC_DESTINATION'
        raw_clock_mismatch = synthetic_observation()
        raw_clock_mismatch['raw_payload']['SrcUpdateTime'] = NOW.isoformat()
        cases = (
            ('non-normal status', synthetic_observation(status=1), 'unusable', ['non_normal_stop_status']),
            ('missing ETA', synthetic_observation(estimate=None), 'unusable', ['missing_or_invalid_estimate']),
            ('fresh normal ETA', synthetic_observation(), 'usable', []),
            ('stale', synthetic_observation(source_age=121), 'stale', ['stale_source_time']),
            ('source after fetch', synthetic_observation(source_age=5), 'unusable', ['future_or_inconsistent_clock']),
            ('fetch after evaluation', synthetic_observation(fetch_age=-1), 'unusable', ['future_or_inconsistent_clock']),
            ('raw clock mismatch', raw_clock_mismatch, 'unusable', ['raw_source_time_mismatch']),
            ('wrong route', wrong_route, 'unusable', ['evidence_scope_mismatch']),
            ('wrong direction', wrong_direction, 'unusable', ['evidence_scope_mismatch']),
            ('wrong boarding stop', wrong_stop, 'unusable', ['evidence_scope_mismatch']),
        )
        for name, observation, quality, issues in cases:
            with self.subTest(name=name):
                context = synthetic_context()
                context['observations'] = [observation]
                snapshot = build_assessment(source_spec(), context)['input_snapshot']
                inspected = inspect_boarding_observation(
                    observation, route_uid='SYNTHETIC_ROUTE', direction=1,
                    boarding_stop_uid='SYNTHETIC_STOP', evaluated_at=NOW,
                )
                self.assertEqual(inspected, (quality, issues))
                self.assertEqual(inspected, (
                    snapshot['engine_input']['evidence_quality'], snapshot['quality_issues'],
                ))

    def test_boarding_inspection_defaults_to_saved_fetch_time(self):
        observation = synthetic_observation(source_age=130, fetch_age=10)
        scope = dict(route_uid='SYNTHETIC_ROUTE', direction=1, boarding_stop_uid='SYNTHETIC_STOP')
        self.assertEqual(inspect_boarding_observation(observation, **scope), ('usable', []))
        self.assertEqual(inspect_boarding_observation(
            observation, **scope, evaluated_at=NOW,
        ), ('stale', ['stale_source_time']))

    def test_latest_unusable_does_not_fall_back_and_delayed_conflicts_are_unusable(self):
        cases = [([synthetic_observation(1, source_age=60, fetch_age=50),
                   synthetic_observation(2, status=3, estimate=None)], 'non_normal_stop_status'),
                 ([synthetic_observation(1, source_age=30, fetch_age=20),
                   synthetic_observation(2, source_age=60, fetch_age=5)], 'delayed_observation'),
                 ([synthetic_observation(1), synthetic_observation(2, fetch_age=5, estimate=301)], 'conflicting_observations')]
        for rows, issue in cases:
            with self.subTest(issue=issue):
                context = synthetic_context()
                context['observations'] = rows
                result = build_assessment(source_spec(observation_ids=(1, 2)), context)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertEqual(result['evidence_observation_id'], 2)
                self.assertIn(issue, result['input_snapshot']['quality_issues'])

    def test_scope_future_and_raw_mismatch_do_not_become_usable(self):
        for field, value in [('stop_uid', 'WRONG'), ('source_system', 'other'),
                             ('fetched_at', (NOW + timedelta(seconds=1)).isoformat()),
                             ('estimate_seconds', 400)]:
            context = synthetic_context()
            row = synthetic_observation()
            row[field] = value
            context['observations'] = [row]
            with self.subTest(field=field):
                self.assertEqual(build_assessment(source_spec(), context)['status'], 'insufficient_evidence')

    def test_reference_validity_and_nonboarding_target(self):
        context = synthetic_context()
        context['legs'][1]['reference_details']['valid_to'] = '2026-11-17'
        self.assertEqual(build_assessment(synthetic_spec(), context)['status'], 'insufficient_evidence')
        context = synthetic_context()
        context['legs'][1]['transport_mode'] = 'walking'
        self.assertEqual(build_assessment(synthetic_spec(), context)['status'], 'insufficient_evidence')
        context['transfer']['from_leg_id'], context['transfer']['to_leg_id'] = 2, 1
        with self.assertRaises(AssessmentError):
            build_assessment(synthetic_spec(), context)

    def test_canonical_identity_is_stable_but_time_or_evidence_changes_history(self):
        first = build_assessment(synthetic_spec(), synthetic_context())
        same = synthetic_spec(evaluated_at=NOW.astimezone(timezone(timedelta(hours=8))))
        self.assertEqual(first, build_assessment(same, synthetic_context()))
        changed = build_assessment(synthetic_spec(evaluated_at=NOW + timedelta(seconds=1)), synthetic_context())
        self.assertNotEqual(first['assessment_key'], changed['assessment_key'])

    def test_missing_observation_and_unknown_identity(self):
        with self.assertRaises(AssessmentError):
            build_assessment(source_spec(), synthetic_context())
        result = build_assessment(source_spec(observation_ids=()), synthetic_context())
        self.assertEqual(result['reason_code'], 'evidence_missing')
        with self.assertRaises(AssessmentError):
            build_assessment(synthetic_spec(journey_id=99), synthetic_context())

    def test_unknown_reference_numbers_keep_decimal_precision(self):
        context = synthetic_context()
        context['legs'][0]['reference_details']['precise'] = Decimal('0.12345678901234567890123456789')
        record = build_assessment(synthetic_spec(), context)
        self.assertIn('0.12345678901234567890123456789', canonical_json(record))
        with self.assertRaises(ValueError):
            to_utc_datetime('2026-11-18T01:00:00.1234567Z')  # Reject, never silently truncate.
