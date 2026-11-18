"""Synthetic transfer intervals only; no live N1 record is an ETA fixture here."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone, tzinfo
import unittest
from zoneinfo import ZoneInfo

from planaware.risk import RiskInputError, TransferInput, evaluate_transfer

UTC = timezone.utc
DEPARTURE = datetime(2026, 11, 18, 1, 30, tzinfo=UTC)
DEADLINE = DEPARTURE - timedelta(minutes=5)


def synthetic_input(**changes):
    base = TransferInput(journey_id=1, transfer_id=2,
        earliest_arrival=DEADLINE - timedelta(minutes=4),
        latest_arrival=DEADLINE - timedelta(minutes=2),
        next_departure=DEPARTURE, boarding_buffer=timedelta(minutes=5),
        evidence_quality='usable', evaluated_at=DEPARTURE - timedelta(minutes=15))
    return replace(base, **changes)


class TransferRiskTests(unittest.TestCase):
    def test_feasible_transfer_preserves_identity_deadline_and_minimum_buffer(self):
        result = evaluate_transfer(synthetic_input())
        self.assertEqual((result.status, result.reason_code), ('connection_feasible', 'within_buffer'))
        self.assertEqual((result.journey_id, result.transfer_id), (1, 2))
        self.assertEqual(result.boarding_deadline, DEADLINE)
        self.assertEqual(result.minimum_buffer, timedelta(minutes=2))

    def test_crossing_interval_is_risk_not_insufficient_evidence(self):
        result = evaluate_transfer(synthetic_input(latest_arrival=DEADLINE + timedelta(minutes=1)))
        self.assertEqual((result.status, result.reason_code), ('connection_at_risk', 'deadline_overlap'))
        self.assertEqual(result.minimum_buffer, -timedelta(minutes=1))

    def test_entire_interval_too_late(self):
        result = evaluate_transfer(synthetic_input(earliest_arrival=DEADLINE + timedelta(seconds=1),
            latest_arrival=DEADLINE + timedelta(seconds=10)))
        self.assertEqual((result.status, result.reason_code), ('connection_at_risk', 'deadline_missed'))
        self.assertEqual(result.minimum_buffer, -timedelta(seconds=10))

    def test_exact_boundaries_and_zero_width_intervals(self):
        for earliest, latest, reason in [
            (DEADLINE - timedelta(seconds=1), DEADLINE, 'deadline_overlap'),
            (DEADLINE, DEADLINE, 'deadline_missed'),
            (DEADLINE, DEADLINE + timedelta(seconds=1), 'deadline_missed')]:
            with self.subTest(reason=reason, earliest=earliest):
                result = evaluate_transfer(synthetic_input(earliest_arrival=earliest, latest_arrival=latest))
                self.assertEqual((result.status, result.reason_code), ('connection_at_risk', reason))

    def test_missing_eta_bounds(self):
        for changes in ({'earliest_arrival': None}, {'latest_arrival': None},
                        {'earliest_arrival': None, 'latest_arrival': None}):
            with self.subTest(changes=changes):
                result = evaluate_transfer(synthetic_input(**changes))
                self.assertEqual((result.status, result.reason_code), ('insufficient_evidence', 'missing_arrival_interval'))
                self.assertIsNone(result.minimum_buffer)
                self.assertIsNone(result.boarding_deadline)

    def test_missing_unusable_and_stale_evidence_take_precedence_over_missing_times(self):
        for quality in ('missing', 'unusable', 'stale'):
            with self.subTest(quality=quality):
                result = evaluate_transfer(synthetic_input(evidence_quality=quality,
                    earliest_arrival=None, next_departure=None, boarding_buffer=None))
                self.assertEqual((result.status, result.reason_code), ('insufficient_evidence', 'evidence_' + quality))
                self.assertIsNone(result.minimum_buffer)

    def test_missing_departure_and_boarding_buffer(self):
        for field, reason in [('next_departure', 'missing_departure'), ('boarding_buffer', 'missing_boarding_buffer')]:
            with self.subTest(field=field):
                result = evaluate_transfer(synthetic_input(**{field: None}))
                self.assertEqual((result.status, result.reason_code), ('insufficient_evidence', reason))

    def test_missing_timing_precedence_is_stable(self):
        result = evaluate_transfer(synthetic_input(earliest_arrival=None, next_departure=None, boarding_buffer=None))
        self.assertEqual(result.reason_code, 'missing_arrival_interval')
        result = evaluate_transfer(synthetic_input(next_departure=None, boarding_buffer=None))
        self.assertEqual(result.reason_code, 'missing_departure')

    def test_invalid_interval_is_rejected_even_when_evidence_is_unusable(self):
        for quality in ('usable', 'stale'):
            with self.subTest(quality=quality), self.assertRaises(RiskInputError):
                evaluate_transfer(synthetic_input(earliest_arrival=DEPARTURE, evidence_quality=quality))

    def test_negative_or_wrong_type_buffer_is_rejected(self):
        for buffer in (timedelta(microseconds=-1), 5, True, '5'):
            with self.subTest(buffer=buffer), self.assertRaises(RiskInputError):
                evaluate_transfer(synthetic_input(boarding_buffer=buffer))

    def test_naive_or_wrong_type_datetimes_are_rejected(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None
        for field in ('earliest_arrival', 'latest_arrival', 'next_departure', 'evaluated_at'):
            for value in (DEPARTURE.replace(tzinfo=None), DEPARTURE.replace(tzinfo=NoOffset()), '2026-11-18'):
                with self.subTest(field=field, value=value), self.assertRaises(RiskInputError):
                    evaluate_transfer(synthetic_input(**{field: value}))

    def test_second_and_microsecond_precision_are_not_rounded(self):
        for offset, status, reason in [(-1, 'connection_feasible', 'within_buffer'),
                                      (0, 'connection_at_risk', 'deadline_overlap'),
                                      (1, 'connection_at_risk', 'deadline_overlap')]:
            with self.subTest(microseconds=offset):
                result = evaluate_transfer(synthetic_input(latest_arrival=DEADLINE + timedelta(microseconds=offset)))
                self.assertEqual((result.status, result.reason_code), (status, reason))
                self.assertEqual(result.minimum_buffer, timedelta(microseconds=-offset))
        result = evaluate_transfer(synthetic_input(latest_arrival=DEADLINE - timedelta(seconds=1)))
        self.assertEqual(result.minimum_buffer, timedelta(seconds=1))

    def test_fractional_buffer_and_zero_buffer(self):
        result = evaluate_transfer(synthetic_input(boarding_buffer=timedelta(minutes=5, microseconds=1)))
        self.assertEqual(result.boarding_deadline, DEADLINE - timedelta(microseconds=1))
        result = evaluate_transfer(synthetic_input(boarding_buffer=timedelta(0), latest_arrival=DEPARTURE))
        self.assertEqual((result.status, result.reason_code), ('connection_at_risk', 'deadline_overlap'))

    def test_equivalent_instants_across_offsets_produce_equal_results(self):
        value = synthetic_input()
        local = timezone(timedelta(hours=8))
        other = replace(value, **{field: getattr(value, field).astimezone(local)
            for field in ('earliest_arrival', 'latest_arrival', 'next_departure', 'evaluated_at')})
        self.assertEqual(evaluate_transfer(value), evaluate_transfer(other))
        self.assertIs(evaluate_transfer(other).boarding_deadline.tzinfo, UTC)

    def test_dst_fold_comparison_uses_instants_not_wall_clock_order(self):
        zone = ZoneInfo('America/New_York')
        value = synthetic_input(
            earliest_arrival=datetime(2026, 11, 1, 1, 50, tzinfo=zone, fold=0),
            latest_arrival=datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1),
            next_departure=datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1))
        result = evaluate_transfer(value)
        self.assertEqual(result.status, 'connection_feasible')
        self.assertEqual(result.minimum_buffer, timedelta(minutes=15))

    def test_identical_inputs_are_deterministic_and_immutable(self):
        value = synthetic_input()
        first = evaluate_transfer(value)
        self.assertEqual(first, evaluate_transfer(value))
        self.assertEqual(first.evaluated_at, value.evaluated_at)
        with self.assertRaises(FrozenInstanceError):
            first.status = 'connection_at_risk'

    def test_evaluation_clock_does_not_invent_progress_or_override_quality(self):
        result = evaluate_transfer(synthetic_input(evaluated_at=DEPARTURE + timedelta(days=1)))
        self.assertEqual(result.status, 'connection_feasible')

    def test_identity_quality_and_required_evaluation_timestamp_validation(self):
        for changes in ({'journey_id': 0}, {'transfer_id': True}, {'journey_id': '1'},
                        {'evidence_quality': 'unknown'}, {'evidence_quality': None}, {'evaluated_at': None}):
            with self.subTest(changes=changes), self.assertRaises(RiskInputError):
                evaluate_transfer(synthetic_input(**changes))
        with self.assertRaises(RiskInputError):
            evaluate_transfer(None)

    def test_deadline_overflow_is_explicit_input_error(self):
        with self.assertRaises(RiskInputError):
            evaluate_transfer(synthetic_input(next_departure=datetime.min.replace(tzinfo=UTC)))
