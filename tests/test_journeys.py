from dataclasses import replace
from datetime import date, datetime
from importlib.util import find_spec
import unittest
from unittest.mock import patch

from planaware.journeys import (
    BoardingAllowance, JourneyDraft, JourneyError, JourneyStore, TrainDeparture, Walk, _draft_values,
)


DRAFT = JourneyDraft("Pilot draft", date(2026, 11, 23), "TPE10912", "TPE10912", 0,
                     ("TPE10600", 10), ("TPE10606", 13))
# Synthetic inputs for testing only, not timetable or duration evidence.
TIMED = replace(DRAFT, walk=Walk(300, "Test walk", "Alighting curb", "Platform"),
                train=TrainDeparture("TEST", datetime.fromisoformat("2026-11-23T12:00:00+08:00"),
                                     "Synthetic test timetable"),
                allowance=BoardingAllowance(2, "Test boarding allowance"))


class JourneyDraftTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("psycopg"), "psycopg optional dependency unavailable")
    def test_connection_log_keeps_only_driver_class_and_known_sqlstate(self):
        from psycopg.errors import ConnectionFailure

        secret = "postgresql://user:secret@example.test/db token=SECRET raw={payload}"
        store = JourneyStore("host=localhost dbname=planaware_pilot password=SECRET")
        for sqlstate, message in (
            ("08006", "pilot_db.connect_failed error_type=ConnectionFailure sqlstate=08006"),
            ("ABCDE", "pilot_db.connect_failed error_type=ConnectionFailure"),
        ):
            error = ConnectionFailure(secret)
            error.sqlstate = sqlstate
            with self.subTest(sqlstate=sqlstate), patch("psycopg.connect", side_effect=error):
                with self.assertLogs("planaware.journeys", level="WARNING") as logs:
                    with self.assertRaisesRegex(JourneyError, "^Cannot connect to the pilot database$"):
                        store.get_journey(1)
                record, = logs.records
                self.assertEqual(record.getMessage(), message)
                self.assertEqual(record.levelname, "WARNING")
                self.assertIsNone(record.exc_info)
                self.assertIsNone(record.stack_info)

    def test_valid_catalog_draft_preserves_unknowns(self):
        name, legs, allowance = _draft_values(DRAFT)
        self.assertEqual(name, DRAFT.name)
        self.assertEqual([leg[0] for leg in legs], ["bus", "walking", "high_speed_rail"])
        self.assertEqual(legs[0][1:5], ("TPE10912", 0, "TPE10600", "TPE10606"))
        self.assertEqual(legs[0][-1]["origin_sequence"], 10)
        self.assertEqual(legs[0][-1]["destination_sequence"], 13)
        self.assertIsNone(legs[1][6])
        self.assertIsNone(legs[2][5])
        self.assertIsNone(allowance)

    def test_rejects_invalid_selection_before_connecting(self):
        store = JourneyStore("unused")
        for changes in ({"direction": 1}, {"subroute_uid": "TPE10911"},
                        {"boarding": ("TPE10606", 13), "alighting": ("TPE10600", 10)},
                        {"boarding": ("TPE10600", 13)}):
            with self.subTest(changes=changes), self.assertRaises(JourneyError):
                store.create_draft(replace(DRAFT, **changes))

    def test_rejects_invalid_dates_negative_values_and_missing_sources(self):
        for changes in (
            {"service_date": "2026-11-23"},
            {"service_date": date(2026, 11, 24)},
            {"walk": replace(TIMED.walk, duration_seconds=-1)},
            {"walk": replace(TIMED.walk, source=" ")},
            {"train": replace(TIMED.train, departure_at=datetime(2026, 11, 23, 12))},
            {"train": replace(TIMED.train, source="")},
            {"allowance": replace(TIMED.allowance, minutes=-1)},
            {"allowance": replace(TIMED.allowance, source="")},
        ):
            with self.subTest(changes=changes), self.assertRaises(JourneyError):
                _draft_values(replace(TIMED, **changes))

    def test_save_rejects_train_on_another_taipei_service_date_before_connecting(self):
        draft = replace(TIMED, service_date=date(2026, 11, 24))
        with self.assertRaisesRegex(JourneyError, "match the Taipei service date"):
            JourneyStore("unused").save_journey(1, draft, "unused")
