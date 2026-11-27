"""Offline provider fixtures use fixed timestamps and never acquire live data."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from planaware.journeys import load_synthetic_212_snapshot
from planaware.monitor_scheduler import (
    AcquisitionUncertain, Attempt, MonitorScheduler, frozen_boarding_observation, timeline_provider,
)
from planaware.monitor_state import MonitorState, transition
from planaware.monitor_store import MonitorSession
from planaware.n1 import dump_stable_json, freeze_n1_fixture, parse_strict_json
from test_monitor_state import clock, observed


class MonitorSchedulerAdapterTests(unittest.TestCase):
    def test_tape_restarts_and_marks_nonadjacent_duplicate(self):
        first = observed("first", 0, 1)
        stalled = observed("cached", 0, 90)
        late = observed("late", 10, 140)
        tape = [first, stalled, None, first, late]
        reserved = Attempt("session", 4, 8, "scheduler:acquisition:4", clock(135))
        one = timeline_provider(tape)
        two = timeline_provider(tape)
        self.assertEqual(one(reserved), two(reserved))
        self.assertEqual(one(reserved).original_attempt, 1)
        self.assertEqual(one(replace(reserved, number=2)).original_attempt, 2)
        self.assertIsNone(one(replace(reserved, number=3)))
        self.assertEqual(one(replace(reserved, number=5)).observation, late)

    def test_frozen_adapter_reuses_boarding_gate_and_keeps_scope(self):
        snapshot = load_synthetic_212_snapshot()
        boarding = snapshot.scope.stop_uids[0]
        observation = frozen_boarding_observation(snapshot, boarding)
        self.assertEqual((observation.origin, observation.result), ("synthetic", "ok"))
        self.assertIsNone(observation.source_updated_at)  # Bundled fixture is not departed.
        raw = parse_strict_json(snapshot.records_json)[0]["raw_payload"]
        fresh = freeze_n1_fixture(dump_stable_json([dict(raw, StopStatus=0)]), scope=snapshot.scope,
                                  requested_at=snapshot.requested_at, fetched_at=snapshot.fetched_at,
                                  run_key="synthetic-usable")
        self.assertEqual(frozen_boarding_observation(fresh, boarding).source_updated_at, raw["SrcUpdateTime"])
        self.assertEqual(observation.fetched_at, snapshot.fetched_at)
        self.assertEqual(frozen_boarding_observation(snapshot, boarding), observation)
        empty = freeze_n1_fixture("[]", scope=snapshot.scope, requested_at=snapshot.requested_at,
                                  fetched_at=snapshot.fetched_at, run_key="synthetic-empty")
        self.assertIsNone(frozen_boarding_observation(empty, boarding).source_updated_at)

    def test_tick_budget_and_uncertain_attempt_with_fixed_clock(self):
        state, _ = transition(MonitorState("session", "synthetic"), observed("first", 0, 1), clock(1))
        initial = MonitorSession(state, 1, 1, clock(0), None, 1, "first", None)
        store = Mock()
        connection = Mock()
        events = []

        def apply(_, session, item, at, sequence, identity, expected_version):
            self.assertEqual(expected_version, session.version)
            state, batch = transition(session.state, item, at)
            events.extend(event.kind for event in batch)
            return replace(session, state=state, version=session.version + 1,
                           last_input_sequence=sequence, last_input_identity=identity)

        store._apply_locked.side_effect = apply
        row = dict(deadline=clock(150), min_interval=20, max_acquisitions=1,
                   acquisitions_reserved=0, last_outcome=None)
        result = MonitorScheduler(store)._run_locked(connection, initial, row, clock(121))
        self.assertEqual((result.outcome, result.acquisitions_reserved, result.next_due_at),
                         ("tick", 0, clock(141)))
        self.assertEqual(events, ["evidence_became_stale"])

        def uncertain(attempt):
            self.assertIn("SET acquisitions_reserved", connection.execute.call_args.args[0])
            self.assertEqual(attempt.input_sequence, result.session.last_input_sequence + 2)
            raise AcquisitionUncertain()

        final = MonitorScheduler(store, uncertain)._run_locked(connection, result.session, row, clock(141))
        self.assertEqual((final.outcome, final.acquisitions_reserved, final.stop_reason),
                         ("uncertain", 1, "budget_exhausted"))
        self.assertEqual(final.session.state.lifecycle, "stopped")
        self.assertEqual(events, ["evidence_became_stale", "acquisition_failed"])
