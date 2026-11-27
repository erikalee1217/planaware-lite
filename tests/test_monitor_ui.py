from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from planaware.monitor_demo import DemoSession, local_pilot_dsn
from planaware.monitor_state import MonitorState
from planaware.monitor_store import MonitorError, MonitorSession, StoredMonitorEvent
from planaware.monitor_ui_helpers import HISTORICAL, RECORDED_TIME, SIMULATED_TIME, SIMULATION, event_history, monitoring_summary, step_summary

AT = datetime(2026, 11, 25, 1, tzinfo=timezone.utc)
PAGE = Path(__file__).resolve().parents[1] / "pages" / "1_Offline_Monitoring.py"


def demo(lifecycle="active"):
    state = MonitorState("offline-demo:1", "synthetic", lifecycle=lifecycle, acquisition="ok",
                         freshness="fresh", risk="at_risk", evaluated_at=AT)
    return DemoSession(MonitorSession(state, 1, 1, AT, AT if lifecycle == "stopped" else None,
                                      1, "demo", None), AT + timedelta(minutes=1))


class MonitorPresentationTests(unittest.TestCase):
    def test_provenance_clock_and_historical_risk(self):
        self.assertEqual((SIMULATION, HISTORICAL), ("Simulation", "Historical evidence"))
        view = monitoring_summary(demo("stopped").session.state, {"status": "on_track", "evaluated_at": AT})
        self.assertEqual(view["Evidence freshness"], "fresh (last recorded)")
        self.assertEqual(view["Latest saved assessment risk"], "on_track")
        self.assertEqual(view["Last processed (Taipei)"], "2026-11-25 09:00:00")
        self.assertFalse(demo("stopped").can_step)
        self.assertFalse(demo("paused").can_step)
        self.assertTrue(demo().can_step)
        for reason, label in (("budget_exhausted", "Acquisition budget exhausted"),
                              ("deadline", "Deadline reached"), ("user_stopped", "Stopped by user"),
                              (None, "No stop reason recorded")):
            value = step_summary(replace(demo("stopped"), stop_reason=reason,
                                         last_outcome="empty", acquisitions_remaining=0))
            self.assertEqual(value, {"Stop reason": label, "Last offline step": "No observation",
                                     "Acquisitions remaining": 0})

    def test_orders_by_occurrence_then_event_id(self):
        event = StoredMonitorEvent("b", "session", "second", "basis", "ok", "failed",
                                   AT, AT + timedelta(days=1))
        earlier = replace(event, event_id="z", kind="earlier", occurred_at=AT - timedelta(seconds=1))
        tied = replace(event, event_id="a", kind="first")
        rows = event_history([event, earlier, tied])
        self.assertEqual([row["Event type"] for row in rows], ["earlier", "first", "second"])
        self.assertEqual(rows[0][SIMULATED_TIME], "2026-11-25 08:59:59")
        self.assertEqual(rows[0][RECORDED_TIME], "2026-11-26 09:00:00")

    @unittest.skipUnless(find_spec("psycopg"), "Pilot database dependency unavailable")
    def test_requires_local_endpoint_before_any_connection(self):
        for dsn in ("dbname=planaware_pilot host=example.invalid",
                    "dbname=planaware_pilot host=127.0.0.1 hostaddr=192.0.2.1",
                    "dbname=planaware_pilot host=127.0.0.1,127.0.0.1",
                    "dbname=planaware_pilot host=127.0.0.1 service=example"):
            with self.subTest(dsn=dsn), self.assertRaises(MonitorError):
                local_pilot_dsn(dsn)
        self.assertIn("hostaddr=127.0.0.1", local_pilot_dsn("dbname=planaware_pilot host=127.0.0.1"))


@unittest.skipUnless(find_spec("streamlit") and find_spec("psycopg"), "Optional UI or pilot dependency unavailable")
class MonitorPageTests(unittest.TestCase):
    def test_explicit_controls_and_read_only_rerun(self):
        from streamlit.testing.v1 import AppTest

        service = Mock()
        service.list_journeys.return_value = [
            {"journey_id": 1, "journey_name": "Saved pilot", "service_date": date(2026, 11, 25)}
        ]
        service.latest_assessment.return_value = None
        service.events.return_value = []
        loaded = [None]
        history = {}
        service.sessions.side_effect = lambda _: tuple((key, value.session.state.lifecycle)
                                                        for key, value in reversed(tuple(history.items())))
        service.load.side_effect = lambda _, identity: history[identity]

        def create(_, *, after=None):
            identity = "offline-demo:1" if after is None else "offline-demo:1:00000000000000000002"
            value = demo()
            value = replace(value, session=replace(value.session, state=replace(value.session.state,
                                                                                session_id=identity)))
            history[identity] = loaded[0] = value
            return value

        def stop(value):
            identity = value.session.state.session_id
            history[identity] = loaded[0] = replace(demo("stopped"), stop_reason="user_stopped")
        service.create.side_effect = create
        service.stop.side_effect = stop
        with patch("planaware.monitor_demo.MonitoringDemo", return_value=service), patch.dict(
            "os.environ", {"PLANAWARE_PILOT_DSN": "isolated-test-placeholder"}, clear=True,
        ):
            app = AppTest.from_file(str(PAGE)).run()
            app.selectbox[0].select(1).run()
            app.run()
            service.create.assert_not_called()
            service.step.assert_not_called()
            app.button[0].click().run()
            service.create.assert_called_once_with(1)
            app.run()
            service.step.assert_not_called()
            self.assertEqual([heading.value for heading in app.subheader],
                             ["Simulation", "Historical evidence", "Domain event history"])
            app.button[0].click().run()
            service.step.assert_called_once_with(demo())
            app.button[1].click().run()
            service.stop.assert_called_once_with(demo())
            self.assertTrue(all(button.disabled for button in app.button[:2]))
            self.assertTrue(any(item.value == "Stop reason: Stopped by user" for item in app.text))
            app.button[2].click().run()
            self.assertEqual(app.selectbox[1].value, "offline-demo:1:00000000000000000002")
            app.selectbox[1].select("offline-demo:1").run()
            self.assertTrue(all(button.disabled for button in app.button))
            app.run()
            self.assertEqual(service.create.call_count, 2)
            self.assertEqual(service.step.call_count, 1)
            self.assertFalse(app.exception)
