"""Fixed-clock synthetic replay for the offline monitoring contract."""
from datetime import datetime, timedelta, timezone
import unittest

from planaware.monitor_state import MonitorState, Observation, SavedAssessment, transition

BASE = datetime(2026, 11, 25, 4, tzinfo=timezone.utc)


def clock(seconds):
    return BASE + timedelta(seconds=seconds)


def observed(identity, source, fetched, *, origin="synthetic", assessment=None):
    return Observation(identity, origin, "ok", clock(source), clock(fetched), assessment)


def replay(session_id="212-demo"):
    initial_risk = SavedAssessment(1, "on_track")
    recovery = observed("obs-3", 138, 140, assessment=SavedAssessment(2, "at_risk"))
    timeline = (
        (0, None),
        (1, observed("obs-1", 0, 1, assessment=initial_risk)),
        (60, observed("obs-2", 0, 60)),
        (120, None),
        (121, None),
        (130, Observation("failed-run", "synthetic", "failed", fetched_at=clock(130))),
        (140, recovery),
        (141, recovery),
        (142, observed("late", 137, 142)),
        (150, "stop"),
    )
    state = MonitorState(session_id, "synthetic")
    steps = []
    for at, item in timeline:
        state, events = transition(state, item, clock(at))
        steps.append((state, events))
    return steps


class MonitorStateTests(unittest.TestCase):
    def test_timeline_replay_and_event_identity(self):
        steps = replay()
        self.assertEqual(steps, replay())
        self.assertEqual([state.freshness for state, _ in steps],
                         ["none", "fresh", "fresh", "fresh", "stale",
                          "stale", "fresh", "fresh", "fresh", "fresh"])
        self.assertEqual(steps[5][0].acquisition, "failed")
        self.assertEqual(steps[5][0].risk, "on_track")
        self.assertEqual(steps[6][0].acquisition, "ok")
        self.assertEqual(steps[7], (steps[6][0], ()))
        self.assertEqual(steps[8], (steps[6][0], ()))
        self.assertEqual(steps[-1][0].lifecycle, "stopped")
        self.assertFalse(steps[-1][0].is_active)
        events = [event for _, batch in steps for event in batch]
        self.assertEqual([event.kind for event in events], [
            "evidence_became_stale", "acquisition_failed", "evidence_recovered", "risk_changed",
        ])
        ids = {event.event_id for event in events}
        self.assertEqual(len(ids), 4)
        self.assertTrue(ids.isdisjoint(event.event_id for _, batch in replay("another") for event in batch))

    def test_acquisition_and_risk_are_independent(self):
        state = MonitorState("live", "live")
        first = SavedAssessment(1, "at_risk")
        state, events = transition(state, first, clock(0))
        self.assertEqual((state.risk, events), ("at_risk", ()))
        state, events = transition(state, Observation("1", "live", "failed"), clock(1))
        self.assertEqual([event.kind for event in events], ["acquisition_failed"])
        state, events = transition(state, Observation("2", "live", "failed"), clock(2))
        self.assertEqual((state.freshness, state.risk, events), ("none", "at_risk", ()))
        state, events = transition(state, observed("3", 2, 3, origin="live"), clock(3))
        self.assertEqual((state.acquisition, state.freshness, state.risk, events), ("ok", "fresh", "at_risk", ()))
        state, events = transition(state, SavedAssessment(2, "insufficient_evidence"), clock(4))
        self.assertEqual([event.kind for event in events], ["risk_changed"])
        self.assertEqual((state.acquisition, state.freshness), ("ok", "fresh"))
        self.assertEqual(transition(state, first, clock(5)), (state, ()))

    def test_rejects_other_source_late_replay_and_clock_conflict(self):
        initial = MonitorState("live", "live")
        self.assertEqual(transition(initial, observed("obs", 0, 1), clock(1)), (initial, ()))
        state, _ = transition(initial, observed("obs", 0, 1, origin="live"), clock(1))
        state, _ = transition(state, observed("new", 8, 9, origin="live"), clock(9))
        for item in (observed("obs", 9, 10, origin="live"),
                     observed("older", 7, 10, origin="live"),
                     observed("future", 11, 10, origin="live"),
                     observed("future-fetch", 9, 11, origin="live")):
            self.assertEqual(transition(state, item, clock(10)), (state, ()))
        self.assertEqual(transition(state, None, clock(8)), (state, ()))

    def test_paused_evidence_ages_and_stop_is_terminal(self):
        state, _ = transition(MonitorState("session", "synthetic"), observed("obs", 0, 1), clock(1))
        state, _ = transition(state, "pause", clock(2))
        self.assertEqual(state.lifecycle, "paused")
        self.assertEqual(transition(state, observed("new", 120, 121), clock(121)), (state, ()))
        state, events = transition(state, None, clock(121))
        self.assertEqual([event.kind for event in events], ["evidence_became_stale"])
        state, _ = transition(state, "resume", clock(122))
        self.assertEqual((state.lifecycle, state.freshness), ("active", "stale"))
        state, _ = transition(state, "stop", clock(123))
        for item in ("resume", "stop", "pause", None, SavedAssessment(1, "on_track"),
                     observed("new", 123, 124)):
            self.assertEqual(transition(state, item, clock(124)), (state, ()))

    def test_empty_success_keeps_last_source_clock_and_ages(self):
        state, _ = transition(MonitorState("session", "synthetic"), observed("obs", 0, 1), clock(1))
        state, events = transition(state, Observation("empty-run", "synthetic", "ok"), clock(121))
        self.assertEqual((state.source_updated_at, state.acquisition, state.freshness), (clock(0), "ok", "stale"))
        self.assertEqual([event.kind for event in events], ["evidence_became_stale"])
        state, events = transition(state, observed("cached", 0, 130), clock(130))
        self.assertEqual((state.freshness, events), ("stale", ()))

    def test_distinct_stale_cycles_and_stopped_activity(self):
        def run():
            state = MonitorState("cycles", "synthetic")
            events = []
            for at, item in ((1, observed("first", 0, 1)), (121, None),
                             (130, observed("second", 129, 130)), (250, None),
                             (251, "stop")):
                state, batch = transition(state, item, clock(at))
                events.extend(batch)
            return state, events

        final, events = run()
        self.assertEqual((final, events), run())
        stale_ids = [event.event_id for event in events if event.kind == "evidence_became_stale"]
        self.assertEqual(len(set(stale_ids)), 2)
        self.assertEqual(final.freshness, "stale")
        self.assertFalse(final.is_active)
        self.assertTrue(MonitorState("active", "synthetic").is_active)
