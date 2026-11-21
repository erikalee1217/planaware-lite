from dataclasses import replace
from datetime import date, datetime
import unittest

from planaware.assessment import build_assessment
from planaware.journeys import (
    JourneyError, JourneyStore, load_synthetic_212_snapshot, observation_assessment_spec,
)
from planaware.n1 import N1Scope, _read_json, freeze_n1_fixture
from test_journey_ui import CONFIRMED, saved_journey


REPLAY_DAY = date(2026, 11, 20)
REPLAY_DRAFT = replace(CONFIRMED, service_date=REPLAY_DAY, train=replace(
    CONFIRMED.train, departure_at=CONFIRMED.train.departure_at.replace(day=REPLAY_DAY.day),
))


class PilotObservationTests(unittest.TestCase):
    def test_loads_bundled_synthetic_boarding_snapshot(self):
        snapshot = load_synthetic_212_snapshot()
        self.assertEqual(snapshot.run_key, "synthetic-212-boarding-replay-v1")
        self.assertEqual(snapshot.scope, N1Scope("TPE10912", 0, ("TPE10600",)))
        row, = _read_json(snapshot.records_json)
        self.assertIs(row["raw_payload"]["synthetic_test_only"], True)
        self.assertEqual(row["stop_status"], 1)
        self.assertLessEqual(row["source_updated_at"], row["tdx_updated_at"])
        self.assertLessEqual(row["tdx_updated_at"], snapshot.requested_at)
        self.assertLessEqual(snapshot.requested_at, snapshot.fetched_at)

    def test_wrong_corridor_is_rejected_before_connecting(self):
        snapshot = load_synthetic_212_snapshot()
        for scope in (
            N1Scope("TPE11023", 0, ("TPE10600",)),
            N1Scope("TPE10912", 1, ("TPE10600",)),
            N1Scope("TPE10912", 0, ("TPE10606",)),
            N1Scope("TPE10912", 0, ("TPE10600", "TPE99999")),
        ):
            empty = freeze_n1_fixture("[]", scope=scope, requested_at=snapshot.requested_at,
                                      fetched_at=snapshot.fetched_at, run_key=snapshot.run_key)
            with self.subTest(scope=scope), self.assertRaisesRegex(JourneyError, "pilot boarding corridor"):
                JourneyStore("unused").import_snapshot(empty)

    def test_status_one_and_normal_without_bounds_both_yield_insufficient_evidence(self):
        snapshot = load_synthetic_212_snapshot()
        normalized, = _read_json(snapshot.records_json)
        journey = saved_journey(REPLAY_DRAFT)
        for status, reason in ((1, "evidence_unusable"), (0, "missing_arrival_interval")):
            with self.subTest(status=status):
                observation = dict(
                    normalized, observation_id=42, stop_status=status, source_system="tdx_bus_n1",
                    fetched_at=snapshot.fetched_at, raw_payload=dict(normalized["raw_payload"], StopStatus=status),
                )
                spec = observation_assessment_spec(journey, observation)
                self.assertEqual((spec.scenario, spec.buffer_semantics), ("source_backed", "boarding_only"))
                self.assertEqual((spec.observation_ids, spec.evidence_leg_id, spec.evidence_stop_uid),
                                 ((42,), journey.legs[0]["leg_id"], "TPE10600"))
                self.assertEqual(spec.evaluated_at, datetime.fromisoformat(snapshot.fetched_at))
                self.assertIsNone(spec.earliest_arrival)
                self.assertIsNone(spec.latest_arrival)
                context = dict(journey=dict(journey_id=journey.journey_id,
                                            service_date=journey.service_date.isoformat()),
                               transfer=dict(journey.transfer, journey_id=journey.journey_id),
                               legs=journey.legs, observations=[observation])
                assessment = build_assessment(spec, context)
                self.assertEqual((assessment["status"], assessment["reason_code"]),
                                 ("insufficient_evidence", reason))
                self.assertEqual(assessment["evidence_observation_id"], 42)
                self.assertIsNone(assessment["input_snapshot"]["result"]["boarding_deadline"])
                self.assertIsNone(assessment["input_snapshot"]["result"]["minimum_buffer"])
                with self.assertRaisesRegex(JourneyError, "does not match"):
                    observation_assessment_spec(journey, dict(observation, stop_uid="TPE10606"))
