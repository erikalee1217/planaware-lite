from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from planaware.journeys import load_synthetic_212_snapshot
from planaware.live import PipelineError
from planaware.live_observations import (
    LIVE_RECORD_LIMIT, acquire_live_boarding, list_live_artifacts, replay_live_boarding,
)
from planaware.n1 import FrozenSnapshot, parse_strict_json
from planaware.ui_helpers import live_evaluation_time, live_observation_prerequisites, observation_summary
from test_journey_ui import ROUTE, demo_observation, historical_journey
from test_live import CREDENTIALS, TOKEN, http_mock


def live_payload(**changes):
    row, = parse_strict_json(load_synthetic_212_snapshot().records_json)
    raw = dict(row["raw_payload"], **changes)
    del raw["synthetic_test_only"]
    return raw


class LivePilotTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "live-snapshots"
        self.store = Mock()
        self.store.execute_snapshot_sql.return_value = "17"
        self.store.import_snapshot.return_value = 17

    def test_missing_credentials_or_confirmation_does_not_request_or_create_files(self):
        with patch.dict(os.environ, dict.fromkeys(CREDENTIALS, "")), patch("planaware.live.build_opener") as opener:
            with self.assertRaisesRegex(PipelineError, "credentials are unavailable"):
                acquire_live_boarding(self.store, self.directory, confirmed=True)
        opener.assert_not_called()
        with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener") as opener:
            with self.assertRaisesRegex(PipelineError, "Confirm TDX access"):
                acquire_live_boarding(self.store, self.directory, confirmed=False)
        opener.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_fetch_is_scoped_bounded_and_preserves_empty_or_ineligible_snapshots(self):
        for raw in ([], [live_payload(StopStatus=1)]):
            with self.subTest(raw=raw):
                opener = http_mock(json.dumps(raw))
                with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener", return_value=opener):
                    run, fetched_at = acquire_live_boarding(self.store, self.directory, confirmed=True)
                self.assertEqual(run, 17)
                self.assertEqual(opener.open.call_count, 2)
                request = opener.open.call_args.args[0]
                parts = urlsplit(request.full_url)
                self.assertTrue(parts.path.endswith("/Taipei"))
                self.assertEqual(parse_qs(parts.query)["$filter"], [
                    "RouteUID eq 'TPE10912' and Direction eq 0 and (StopUID eq 'TPE10600')",
                ])
                self.assertEqual(parse_qs(parts.query)["$top"], [str(LIVE_RECORD_LIMIT + 1)])
                snapshots = [
                    FrozenSnapshot.from_json((self.directory / name).read_text())
                    for name in list_live_artifacts(self.directory)
                ]
                snapshot, = [saved for saved in snapshots if saved.fetched_at == fetched_at]
                self.assertEqual([row["raw_payload"] for row in parse_strict_json(snapshot.records_json)], raw)
        self.assertEqual(len(list_live_artifacts(self.directory)), 2)

    def test_over_limit_and_transport_failure_do_not_retry_or_expose_secrets(self):
        for error in (None, URLError(TOKEN)):
            with self.subTest(error=type(error).__name__):
                opener = http_mock(json.dumps([live_payload()] * (LIVE_RECORD_LIMIT + 1)))
                if error:
                    auth, _ = list(opener.open.side_effect)
                    opener.open.side_effect = [auth, error]
                with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener", return_value=opener):
                    with self.assertRaisesRegex(PipelineError, "not retried") as raised:
                        acquire_live_boarding(self.store, self.directory, confirmed=True)
                self.assertEqual(opener.open.call_count, 2)
                for secret in (*CREDENTIALS.values(), TOKEN):
                    self.assertNotIn(secret, str(raised.exception))
        self.store.execute_snapshot_sql.assert_not_called()
        self.assertEqual(list_live_artifacts(self.directory), [])

    def test_persistence_failure_leaves_replayable_artifact_and_never_refetches(self):
        opener = http_mock(json.dumps([live_payload()]))

        def lose_connection(statement):
            self.assertEqual(len(list_live_artifacts(self.directory)), 1)
            raise RuntimeError(TOKEN)

        self.store.execute_snapshot_sql.side_effect = lose_connection
        with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener", return_value=opener):
            with self.assertRaisesRegex(PipelineError, "Replay saved snapshot") as raised:
                acquire_live_boarding(self.store, self.directory, confirmed=True)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn(str(self.directory), str(raised.exception))
        self.assertEqual(opener.open.call_count, 2)
        name, = list_live_artifacts(self.directory)
        with patch.dict(os.environ, dict.fromkeys(CREDENTIALS, "")), patch("planaware.live.build_opener") as no_http:
            self.assertEqual(replay_live_boarding(self.store, self.directory, name),
                             (17, self.store.import_snapshot.call_args.args[0].fetched_at))
            with self.assertRaisesRegex(PipelineError, "Choose a saved"):
                replay_live_boarding(self.store, self.directory, "../escaped.json")
        no_http.assert_not_called()

    def test_evaluation_age_and_date_gate_use_saved_clocks(self):
        observation = demo_observation()
        observation.update(stop_status=0, raw_payload=dict(observation["raw_payload"], StopStatus=0))
        fetched = live_evaluation_time(observation["fetched_at"])
        later = fetched + timedelta(minutes=3)
        self.assertIn("Eligible as boarding-stop evidence", observation_summary(
            observation, ROUTE, evaluated_at=fetched)["Boarding evidence quality at evaluation"])
        card = observation_summary(observation, ROUTE, evaluated_at=later)
        self.assertEqual(card["Age at evaluation"], "185 s")
        self.assertIn("Rejected (stale)", card["Boarding evidence quality at evaluation"])
        journey = historical_journey()
        self.assertEqual(live_observation_prerequisites(journey, observation["fetched_at"], later), ())
        self.assertIn("reference", live_observation_prerequisites(
            journey, observation["fetched_at"], later + timedelta(days=1))[0])
