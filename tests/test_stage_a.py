"""Synthetic, documentation-derived responses. Never contact TDX."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
import fcntl
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

from planaware import stage_a as stage
from planaware import stage_a_analysis as analysis


def response(value, status=200, headers=None):
    return status, headers or {}, json.dumps(value).encode()


TOKEN = response({"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": 3600})


def row(**changes):
    return dict({"RouteUID": "TPE10912", "Direction": 0, "StopUID": stage.STOPS[0]}, **changes)


class StageATests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name).resolve()
        self.credentials = Mock(return_value=("synthetic-client", "synthetic-secret"))

    def collect(self, replies, **kwargs):
        http = Mock(side_effect=replies)
        stage.collect(self.directory, http, self.credentials, **kwargs)
        return http

    def test_dry_run_and_live_gate(self):
        http = Mock(side_effect=AssertionError("unexpected HTTP"))
        with patch.dict(os.environ, {}, clear=True), patch.object(stage, "DIRECTORY", self.directory):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(stage.main([], http=http), 0)
            self.assertEqual(json.loads(output.getvalue())["planned_data_requests"], 3)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(stage.main(["--execute-live"], http=http), 2)
        http.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_scopes_match_catalog(self):
        from planaware.catalog import load_catalog

        variant = load_catalog().route
        self.assertEqual((stage.SCOPE.route_uid, stage.SCOPE.direction, stage.STOPS),
                         (variant.route_uid, variant.direction, tuple(stop.stop_uid for stop in variant.stops)))
        for name, url in stage.request_specs().items():
            parsed = urlsplit(url)
            self.assertEqual(parsed.hostname, "tdx.transportdata.tw")
            self.assertTrue(parsed.path.endswith("/City/Taipei"))
            query = parse_qs(parsed.query)
            route = "RouteUID eq 'TPE10912' and Direction eq 0"
            stops = " and (StopUID eq 'TPE10600' or StopUID eq 'TPE10606')"
            self.assertEqual(query["$filter"], [route + ("" if name == "A1" else stops)])
            self.assertEqual(query["$top"], ["26"])

    def test_budgets_survive_second_invocation(self):
        pause = Mock()
        http = self.collect([TOKEN] + [response([])] * 6, second_round_after=1, pause=pause)
        self.assertEqual(http.call_count, 7)
        pause.assert_called_once_with(1)
        ledger = self.directory / "requests.ledger"
        self.assertEqual(ledger.read_text().splitlines(), ["oauth"] + ["data"] * 6)
        http.reset_mock()
        with self.assertRaisesRegex(stage.StopCollection, "oauth budget exhausted"):
            stage.collect(self.directory, http, self.credentials)
        http.assert_not_called()
        with ledger.open("a+") as saved:
            with self.assertRaisesRegex(stage.StopCollection, "data budget exhausted"):
                stage.spend(saved, "data")

    def test_restart_and_truncated_ledger_fail_before_http(self):
        check = """
import sys
from pathlib import Path
from planaware.stage_a import collect, StopCollection

def never(*args, **kwargs):
    raise AssertionError("HTTP called after ledger rejection")

try:
    collect(Path(sys.argv[1]), never, lambda: ("synthetic-client", "synthetic-secret"))
except StopCollection as error:
    assert str(error) == sys.argv[2], str(error)
else:
    raise AssertionError("ledger was accepted")
"""
        self.collect([TOKEN, response([]), response([]), response([])])
        subprocess.run([sys.executable, "-c", check, str(self.directory), "oauth budget exhausted"],
                       check=True, env={})
        with (self.directory / "requests.ledger").open("r") as ledger:
            fcntl.flock(ledger, fcntl.LOCK_EX)
            subprocess.run([sys.executable, "-c", check, str(self.directory),
                            "another Stage A session is active"], check=True, env={})
        for truncated in (b"oauth\ndata", b"oauth\r"):
            (self.directory / "requests.ledger").write_bytes(truncated)
            subprocess.run([sys.executable, "-c", check, str(self.directory), "invalid ledger"],
                           check=True, env={})

    def test_http_transport_rejects_redirect_and_bounds_read(self):
        target = "https://elsewhere.invalid/"
        url = stage.request_specs()["N1"]
        visited = []
        stream = io.BytesIO(b"abcdef")
        stream.read = Mock(wraps=stream.read)

        class FakeHTTPS(HTTPSHandler):
            def https_open(self, request):
                visited.append(request.full_url)
                headers = Message()
                headers["Location"] = target
                if len(visited) == 1:
                    reply = addinfourl(io.BytesIO(), headers, request.full_url, code=302)
                    reply.msg = "Found"
                    return reply
                reply = addinfourl(stream, Message(), request.full_url, code=200)
                reply.msg = "OK"
                return reply

        with patch.object(stage, "build_opener",
                          side_effect=lambda *handlers: build_opener(*handlers, FakeHTTPS())):
            http = stage.http_client()
        with self.assertRaisesRegex(stage.StopCollection, "unexpected Stage A URL"):
            http(target, headers={}, body=None, limit=3)
        self.assertEqual(http(url, headers={"Authorization": "Bearer synthetic-token"},
                              body=None, limit=3)[0], 302)
        self.assertEqual(visited, [url])
        self.assertEqual(http(url, headers={}, body=None, limit=3)[2], b"abcd")
        stream.read.assert_called_once_with(4)

    def test_empty_missing_plate_and_offline_analysis(self):
        self.collect([TOKEN, response([row()]), response([]), response([])])
        captures = list(self.directory.glob("*.capture.json"))
        with patch.object(stage, "http_client", side_effect=AssertionError("unexpected HTTP")):
            report = analysis.analyze(captures)
        self.assertEqual(report["n1_row_matches"]["synthetic"]["unmatched"], 1)
        coverage = {item["endpoint"]: item for item in report["coverage"]}
        self.assertEqual(coverage["N1"]["plate_fraction"], 0)
        self.assertIsNone(coverage["A1"]["plate_fraction"])
        self.assertEqual(report["n1_unique_pair_fraction"]["synthetic"], 0)
        self.assertIn("empty", [item["result"] for item in report["capture_outcomes"]])
        for artifact in self.directory.iterdir():
            contents = artifact.read_text()
            for forbidden in ("synthetic-token", "synthetic-secret", "synthetic-client", "Authorization"):
                self.assertNotIn(forbidden, contents)

    def test_failures_stop_without_retry(self):
        cases = [
            (response([], 429), "HTTP 429"), (response([], 503), "HTTP 503"),
            (TimeoutError(), "timeout"), (response([row(StopUID="foreign")]), "out-of-scope"),
            (response([row()] * 26), "record cap"), ((200, {}, b"x" * (stage.MAX_BYTES + 1)), "byte limit"),
            (response([], headers={"Link": '<test>; rel="next"'}), "truncation"),
            (response([], headers={"Content-Length": "3"}), "truncation"),
            (response({"value": [], "@odata.nextLink": "test"}), "truncation"),
            (response([row(Authorization="Bearer synthetic-token")]), "credential-like"),
        ]
        for bad, expected in cases:
            with self.subTest(expected=expected), TemporaryDirectory() as directory:
                http = Mock(side_effect=[TOKEN, bad])
                root = Path(directory).resolve()
                with self.assertRaisesRegex(stage.StopCollection, expected):
                    stage.collect(root, http, self.credentials)
                self.assertEqual(http.call_count, 2)
                self.assertEqual((root / "requests.ledger").read_text(), "oauth\ndata\n")
                capture = json.loads(next(root.glob("*.capture.json")).read_text())
                self.assertIn(expected, capture["result"])
                for artifact in root.glob("*.json"):
                    self.assertNotIn("synthetic-token", artifact.read_text())

    def test_vehicle_trip_and_ambiguous_evidence(self):
        base = datetime(2026, 11, 24, 4, tzinfo=timezone.utc)

        def observed(endpoint, stop, seconds, value="DOC-001", **extra):
            timestamp = (base + timedelta(seconds=seconds)).isoformat()
            time_field = "source_updated_at" if endpoint == "N1" else "gps_time"
            return dict({"endpoint": endpoint, "plate": value, "stop_uid": stop,
                         time_field: timestamp, "fetched_at": timestamp}, **extra)

        board = observed("N1", stage.STOPS[0], 0, estimate_seconds=30, stop_status=0)
        alight = observed("N1", stage.STOPS[1], 25, estimate_seconds=65, stop_status=0)
        first = observed("A2", stage.STOPS[0], 30, event_type=1)
        last = observed("A2", stage.STOPS[1], 90, event_type=1)
        self.assertEqual(analysis.classify(board, [alight]), "vehicle_matched")
        self.assertEqual(analysis.classify(board, [alight, first, last]), "vehicle_matched")
        trip_start = (base - timedelta(minutes=1)).isoformat()
        first = dict(first, trip_start_time=trip_start, trip_start_type=0)
        last = dict(last, trip_start_time=trip_start, trip_start_type=0)
        self.assertEqual(analysis.classify(board, [alight, first, last]), "trip_candidate")
        self.assertEqual(analysis.classify(
            board, [alight, first, dict(last, trip_start_time=base.isoformat())]), "ambiguous")
        self.assertEqual(analysis.classify(
            board, [alight, first, dict(last, trip_start_type=1)]), "vehicle_matched")
        other_trip = observed("A2", stage.STOPS[0], 100, event_type=1)
        self.assertEqual(analysis.classify(board, [alight, first, last, other_trip]), "ambiguous")
        self.assertEqual(analysis.classify(dict(board, plate=None), [first, last]), "unmatched")
        self.assertEqual(analysis.classify(board, [dict(alight, plate="DOC-002")]), "unmatched")
        self.assertEqual(analysis.classify(board, [dict(alight, fetched_at=board["fetched_at"])]), "unmatched")
