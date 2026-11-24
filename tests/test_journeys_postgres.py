"""Opt-in tests for a Docker PostgreSQL instance owned by this suite."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from planaware.assessment import AssessmentError, build_assessment
from planaware.brief import build_brief, format_brief
from planaware.journeys import JourneyError, JourneyStore, load_synthetic_212_snapshot, observation_assessment_spec
from planaware.live import PipelineError
from planaware.live_observations import acquire_live_boarding, list_live_artifacts, replay_live_boarding
from planaware.n1 import FrozenSnapshot, freeze_n1_fixture, parse_strict_json
from planaware.ui_helpers import TAIPEI, store_error_message
from test_assessment import synthetic_spec
from test_journeys import DRAFT, TIMED
from test_pilot_observations import REPLAY_DRAFT
from test_live import CREDENTIALS, http_mock
from test_live_observations import live_payload

ROOT = Path(__file__).resolve().parents[1]
LABEL = "planaware.journey-test"


@unittest.skipUnless(os.environ.get("PLANAWARE_JOURNEY_TEST") == "docker", "Journey Docker tests are opt-in")
class JourneyPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import make_conninfo
        except ImportError:
            raise unittest.SkipTest("Journey optional dependency unavailable") from None
        docker = shutil.which("docker")
        if not docker:
            raise unittest.SkipTest("Docker CLI unavailable")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PG", "TDX_"))}

        def command(*args, sql=None):
            return subprocess.run([docker, *args], input=sql, text=True,
                                  capture_output=True, timeout=40, env=env)

        if command("image", "inspect", "postgres:14").returncode:
            raise unittest.SkipTest("Docker or local postgres:14 image unavailable")
        owner = uuid4().hex
        name = f"planaware-journey-test-{owner}"
        database = f"planaware_pilot_{owner}"
        password = uuid4().hex
        env["POSTGRES_PASSWORD"] = password

        launched = False

        def cleanup():
            result = command("inspect", "--format", '{{index .Config.Labels "planaware.journey-test"}}', name)
            if result.returncode == 0:
                if result.stdout.strip() != owner or command("rm", "--force", name).returncode:
                    raise RuntimeError("Unable to clean up the owned Journey PostgreSQL container")
            elif launched:
                raise RuntimeError("Unable to verify cleanup of the owned Journey PostgreSQL container")

        cls.addClassCleanup(cleanup)
        result = command("run", "--detach", "--pull=never", "--name", name,
                         "--label", f"{LABEL}={owner}", "--publish", "127.0.0.1::5432",
                         "--tmpfs", "/var/lib/postgresql/data:rw,nosuid",
                         "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_USER=journey_test",
                         "-e", f"POSTGRES_DB={database}", "postgres:14")
        if result.returncode:
            raise unittest.SkipTest("Could not launch an owned Docker PostgreSQL instance")
        launched = True
        port = command("port", name, "5432/tcp").stdout.strip()
        if not port.startswith("127.0.0.1:") or not port.rsplit(":", 1)[-1].isdigit():
            raise unittest.SkipTest("Disposable PostgreSQL has no verified loopback port")
        cls.dsn = make_conninfo(host="127.0.0.1", port=port.rsplit(":", 1)[1], dbname=database,
                                user="journey_test", password=password, connect_timeout=2)
        deadline = time.monotonic() + 20
        while True:
            try:
                with psycopg.connect(cls.dsn) as connection:
                    if connection.execute("SELECT current_database()").fetchone()[0] != database:
                        raise unittest.SkipTest("Disposable PostgreSQL database identity did not match")
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise unittest.SkipTest("Could not reach isolated PostgreSQL over loopback") from None
                time.sleep(0.25)
        for migration in sorted((ROOT / "sql/migrations").glob("*.sql")):
            result = command("exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                             "-U", "journey_test", "-d", database, sql=migration.read_text())
            if result.returncode:
                raise RuntimeError(f"Existing schema setup failed at {migration.name}")
        with psycopg.connect(cls.dsn) as connection:
            connection.execute(sql.SQL("CREATE ROLE journey_writer LOGIN PASSWORD {}").format(sql.Literal(password)))
            connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO journey_writer")
            connection.execute("GRANT INSERT ON journeys, journey_legs, transfer_constraints, impact_assessments TO journey_writer")
            connection.execute("GRANT UPDATE ON journeys, journey_legs, transfer_constraints TO journey_writer")
            connection.execute("GRANT DELETE ON transfer_constraints TO journey_writer")
            connection.execute("GRANT INSERT ON ingestion_runs, source_observations TO journey_writer")
            connection.execute("GRANT UPDATE ON ingestion_runs TO journey_writer")
            connection.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO journey_writer")
        cls.psycopg = psycopg
        cls.writer_dsn = make_conninfo(cls.dsn, user="journey_writer")
        cls.store = JourneyStore(cls.writer_dsn)

    def test_round_trip_order_references_and_equivalent_token(self):
        saved = self.store.create_draft(TIMED)
        self.assertEqual(self.store.get_journey(saved.journey_id), saved)
        self.assertEqual(saved.service_date, TIMED.service_date)
        self.assertEqual([leg["leg_order"] for leg in saved.legs], [1, 2, 3])
        self.assertEqual([leg["transport_mode"] for leg in saved.legs], ["bus", "walking", "high_speed_rail"])
        self.assertEqual(saved.legs[0]["reference_details"]["catalog_version"], "taipei-212-v1")
        self.assertEqual(saved.legs[1]["estimated_duration_seconds"], 300)
        self.assertEqual(saved.legs[2]["reference_details"]["timetable_source"], "Synthetic test timetable")
        self.assertEqual(saved.transfer["from_leg_id"], saved.legs[1]["leg_id"])
        self.assertEqual(saved.transfer["to_leg_id"], saved.legs[2]["leg_id"])
        self.assertEqual(saved.transfer["min_transfer_minutes"], 2)
        self.assertFalse(saved.has_assessments)
        equivalent = self.store.create_draft(replace(
            TIMED, name=" Pilot draft ",
            train=replace(TIMED.train, departure_at=TIMED.train.departure_at.astimezone(timezone.utc))))
        self.assertNotEqual(equivalent.journey_id, saved.journey_id)
        self.assertEqual(equivalent.edit_token, saved.edit_token)

    def test_unknown_timing_stays_null(self):
        saved = self.store.create_draft(DRAFT)
        self.assertEqual(self.store.get_journey(saved.journey_id), saved)
        self.assertIsNone(saved.legs[1]["estimated_duration_seconds"])
        self.assertIsNone(saved.legs[2]["planned_departure_at"])
        self.assertIsNone(saved.transfer)

    def test_multiple_transfers_are_rejected_with_a_displayable_error(self):
        saved = self.store.create_draft(TIMED)
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("""
                INSERT INTO transfer_constraints
                    (journey_id, from_leg_id, to_leg_id, min_transfer_minutes, assumption_source)
                VALUES (%s, %s, %s, 0, 'Synthetic second transfer')
            """, (saved.journey_id, saved.legs[0]["leg_id"], saved.legs[1]["leg_id"]))

        with self.assertRaisesRegex(JourneyError, "^Journey has an unsupported transfer$") as caught:
            self.store.get_journey(saved.journey_id)
        self.assertEqual(store_error_message(caught.exception), "Journey has an unsupported transfer")

    def test_child_insert_failure_rolls_back_the_aggregate(self):
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            count_sql = "SELECT (SELECT count(*) FROM journeys), (SELECT count(*) FROM journey_legs), (SELECT count(*) FROM transfer_constraints)"
            before = conn.execute(count_sql).fetchone()
            conn.execute("REVOKE INSERT ON transfer_constraints FROM journey_writer")
            try:
                with self.assertRaisesRegex(JourneyError, "Could not create"):
                    self.store.create_draft(TIMED)
                self.assertEqual(conn.execute(count_sql).fetchone(), before)
            finally:
                conn.execute("GRANT INSERT ON transfer_constraints TO journey_writer")

    def test_existing_foreign_key_and_timing_checks_remain_effective(self):
        left = self.store.create_draft(DRAFT)
        right = self.store.create_draft(DRAFT)
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            with self.assertRaises(self.psycopg.errors.ForeignKeyViolation):
                conn.execute("""
                    INSERT INTO transfer_constraints
                    (journey_id, from_leg_id, to_leg_id, min_transfer_minutes, assumption_source)
                    VALUES (%s, %s, %s, 0, 'test constraint')
                """, (left.journey_id, left.legs[1]["leg_id"], right.legs[2]["leg_id"]))
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                conn.execute("""
                    UPDATE journey_legs SET planned_departure_at = now(), planned_arrival_at = now()
                    WHERE leg_id = %s
                """, (left.legs[0]["leg_id"],))

    def test_assessment_flag_does_not_change_edit_token(self):
        saved = self.store.create_draft(DRAFT)
        with self.psycopg.connect(self.dsn) as connection:
            connection.execute("""
                INSERT INTO impact_assessments (
                    journey_id, assessment_key, status, reason_code, model_version,
                    reference_data_version, assumption_version, input_snapshot
                ) VALUES (%s, %s, 'insufficient_evidence', 'test_fixture',
                          'test', 'test', 'test', '{}'::jsonb)
            """, (saved.journey_id, f"test-fixture-{saved.journey_id}"))
        read = self.store.get_journey(saved.journey_id)
        self.assertTrue(read.has_assessments)
        self.assertEqual(read.edit_token, saved.edit_token)

    def test_in_place_edit_preserves_leg_ids_and_adds_updates_and_clears_transfer(self):
        original = self.store.create_draft(DRAFT)
        added = self.store.save_journey(original.journey_id, TIMED, original.edit_token)
        self.assertEqual((added.outcome, added.journey_id, added.source_journey_id),
                         ("updated", original.journey_id, None))
        self.assertEqual([leg["leg_id"] for leg in added.journey.legs],
                         [leg["leg_id"] for leg in original.legs])
        transfer = added.journey.transfer
        self.assertEqual(transfer["from_leg_id"], added.journey.legs[1]["leg_id"])
        self.assertEqual(transfer["to_leg_id"], added.journey.legs[2]["leg_id"])
        changed = self.store.save_journey(original.journey_id, replace(
            TIMED, allowance=replace(TIMED.allowance, minutes=4)), added.edit_token)
        self.assertEqual(changed.journey.transfer["transfer_id"], transfer["transfer_id"])
        self.assertEqual(changed.journey.transfer["min_transfer_minutes"], 4)
        cleared = self.store.save_journey(original.journey_id, DRAFT, changed.edit_token)
        self.assertEqual(cleared.outcome, "updated")
        self.assertIsNone(cleared.journey.transfer)
        self.assertEqual(self.store.get_journey(original.journey_id), cleared.journey)

    def test_canonical_noop_does_not_update_rows(self):
        original = self.store.create_draft(TIMED)
        equivalent = replace(TIMED, name=" Pilot draft ", train=replace(
            TIMED.train, departure_at=TIMED.train.departure_at.astimezone(timezone.utc)))
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            def revisions():
                return [
                    conn.execute(query, (original.journey_id,)).fetchall()
                    for query in (
                        "SELECT xmin FROM journeys WHERE journey_id = %s",
                        "SELECT xmin FROM journey_legs WHERE journey_id = %s ORDER BY leg_order",
                        "SELECT xmin FROM transfer_constraints WHERE journey_id = %s",
                    )
                ]

            before = revisions()
            result = self.store.save_journey(original.journey_id, equivalent, original.edit_token)
            self.assertEqual((result.outcome, result.journey), ("unchanged", original))
            self.assertEqual(revisions(), before)

    def test_concurrent_conflicting_saves_reject_stale_token(self):
        original = self.store.create_draft(DRAFT)
        barrier = Barrier(2)

        def save(name):
            barrier.wait(timeout=10)
            try:
                return self.store.save_journey(original.journey_id, replace(DRAFT, name=name),
                                               original.edit_token)
            except JourneyError as error:
                self.assertIn("Stale journey edit", str(error))
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ("First edit", "Second edit")))
        self.assertEqual(results.count(None), 1)
        winner = next(result for result in results if result is not None)
        self.assertEqual(winner.outcome, "updated")
        self.assertEqual(self.store.get_journey(original.journey_id), winner.journey)
        with self.assertRaisesRegex(JourneyError, "Stale journey edit"):
            self.store.save_journey(original.journey_id, DRAFT, original.edit_token)

    def test_clone_preserves_assessed_journey_snapshot_and_brief(self):
        from psycopg.types.json import Jsonb

        for status in ("on_track", "insufficient_evidence"):
            with self.subTest(status=status), self.psycopg.connect(self.dsn) as conn:
                original = self.store.create_draft(TIMED)
                departure = TIMED.train.departure_at
                spec = synthetic_spec(
                    journey_id=original.journey_id, transfer_id=original.transfer["transfer_id"],
                    evaluated_at=departure - timedelta(minutes=30),
                    earliest_arrival=departure - timedelta(minutes=15),
                    latest_arrival=departure - timedelta(minutes=10),
                )
                if status == "on_track":
                    for leg in original.legs[1:]:
                        reference = dict(leg["reference_details"],
                                         valid_from=TIMED.service_date.isoformat(),
                                         valid_to=TIMED.service_date.isoformat(),
                                         reference_data_version=spec.reference_version,
                                         assumption_version=spec.assumption_version)
                        conn.execute("UPDATE journey_legs SET reference_details = %s WHERE leg_id = %s",
                                     (Jsonb(reference), leg["leg_id"]))
                    conn.commit()
                assessment = self.store.assess_journey(spec)
                self.assertEqual(assessment["status"], status)
                self.assertEqual(self.store.assess_journey(spec), assessment)
                assessment_id = assessment["assessment_id"]
                query = "SELECT to_jsonb(a) FROM impact_assessments a WHERE assessment_id = %s"
                persisted = conn.execute(query, (assessment_id,)).fetchone()[0]
                brief = format_brief(persisted)
                original = self.store.get_journey(original.journey_id)
                self.assertTrue(original.has_assessments)
                if status == "insufficient_evidence":
                    unchanged = self.store.save_journey(original.journey_id, TIMED, original.edit_token)
                    self.assertEqual(unchanged.outcome, "unchanged")
                result = self.store.save_journey(original.journey_id, replace(
                    TIMED, name="Independent draft"), original.edit_token)
                self.assertEqual((result.outcome, result.source_journey_id), ("cloned", original.journey_id))
                self.assertNotEqual(result.journey_id, original.journey_id)
                self.assertTrue({leg["leg_id"] for leg in result.journey.legs}.isdisjoint(
                    leg["leg_id"] for leg in original.legs))
                self.assertEqual(result.journey.transfer["from_leg_id"], result.journey.legs[1]["leg_id"])
                self.assertEqual(result.journey.transfer["to_leg_id"], result.journey.legs[2]["leg_id"])
                self.assertFalse(result.journey.has_assessments)
                self.assertEqual(self.store.get_journey(original.journey_id), original)
                self.assertEqual(conn.execute(query, (assessment_id,)).fetchone()[0], persisted)
                self.assertEqual(format_brief(conn.execute(query, (assessment_id,)).fetchone()[0]), brief)

    def test_child_write_failure_rolls_back_save(self):
        original = self.store.create_draft(DRAFT)
        with self.psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("REVOKE INSERT ON transfer_constraints FROM journey_writer")
            try:
                with self.assertRaisesRegex(JourneyError, "Could not save"):
                    self.store.save_journey(original.journey_id, replace(TIMED, name="Failed edit"),
                                            original.edit_token)
                self.assertEqual(self.store.get_journey(original.journey_id), original)
            finally:
                conn.execute("GRANT INSERT ON transfer_constraints TO journey_writer")

    def test_locked_assessment_rejects_conflict_and_keeps_original_record(self):
        saved = self.store.create_draft(TIMED)
        spec = synthetic_spec(journey_id=saved.journey_id, transfer_id=saved.transfer["transfer_id"])
        assessment = self.store.assess_journey(spec)

        def conflicting_result(spec, context):
            return dict(build_assessment(spec, context), reason_code="conflicting_result")

        with patch("planaware.journeys.build_assessment", side_effect=conflicting_result):
            with self.assertRaisesRegex(AssessmentError, "Assessment key already belongs"):
                self.store.assess_journey(spec)
        self.assertEqual(self.store.assess_journey(spec), assessment)
        with self.psycopg.connect(self.dsn) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM impact_assessments WHERE journey_id = %s",
                (saved.journey_id,),
            ).fetchone()[0], 1)

    def test_locked_assessment_rejects_missing_journey_or_transfer(self):
        saved = self.store.create_draft(DRAFT)
        with self.assertRaisesRegex(JourneyError, "Journey not found"):
            self.store.assess_journey(synthetic_spec(journey_id=9_223_372_036_854_775_807))
        with self.assertRaisesRegex(AssessmentError, "Transfer must exist"):
            self.store.assess_journey(synthetic_spec(journey_id=saved.journey_id))
        self.assertFalse(self.store.get_journey(saved.journey_id).has_assessments)

    def test_assessment_waits_for_edit_commit_before_reading_context(self):
        from psycopg.conninfo import make_conninfo

        original = self.store.create_draft(TIMED)
        spec = synthetic_spec(journey_id=original.journey_id, transfer_id=original.transfer["transfer_id"])
        worker_name = "planaware-assessment-lock-" + uuid4().hex
        worker_store = JourneyStore(make_conninfo(self.writer_dsn, application_name=worker_name))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.psycopg.connect(self.dsn, autocommit=True) as observer:
                with self.psycopg.connect(self.dsn) as editor:
                    editor.execute("SELECT 1 FROM journeys WHERE journey_id = %s FOR UPDATE",
                                   (original.journey_id,))
                    editor.execute("UPDATE journeys SET journey_name = %s WHERE journey_id = %s",
                                   ("Committed concurrent edit", original.journey_id))
                    editor.execute("UPDATE transfer_constraints SET min_transfer_minutes = 7 WHERE transfer_id = %s",
                                   (spec.transfer_id,))
                    blocker = editor.info.backend_pid
                    assessment = pool.submit(worker_store.assess_journey, spec)
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        if assessment.done():
                            assessment.result()
                            self.fail("Assessment finished before the edit committed")
                        blocked = observer.execute("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_stat_activity
                                WHERE application_name = %s AND %s = ANY(pg_blocking_pids(pid))
                            )
                        """, (worker_name, blocker)).fetchone()[0]
                        if blocked:
                            break
                    else:
                        self.fail("PostgreSQL did not report the assessment waiting on the edit lock")
                result = assessment.result(timeout=10)
                context = observer.execute("""
                    SELECT input_snapshot->'context' FROM impact_assessments WHERE assessment_id = %s
                """, (result["assessment_id"],)).fetchone()[0]
                self.assertEqual(context["journey"]["journey_name"], "Committed concurrent edit")
                self.assertEqual(context["transfer"]["min_transfer_minutes"], 7)
                self.assertEqual(self.store.assess_journey(spec), result)

    def test_assumption_form_assessment_history_and_clone_end_to_end(self):
        from planaware.ui_helpers import assessment_spec_from_form

        draft = replace(TIMED, confirm_service_date_assumptions=True)
        journey = self.store.create_draft(draft)
        self.assertEqual(self.store.list_assessments(journey.journey_id), [])
        spec = assessment_spec_from_form(journey, "11:45", "11:50", "11:30", "Synthetic test bounds")
        older = self.store.assess_journey(spec)
        newer = self.store.assess_journey(replace(spec, evaluated_at=spec.evaluated_at + timedelta(minutes=1)))
        same_time = self.store.assess_journey(replace(spec, interval_source="Another synthetic test assumption",
                                                      evaluated_at=spec.evaluated_at + timedelta(minutes=1)))
        rows = self.store.list_assessments(journey.journey_id)
        self.assertEqual([row["assessment_id"] for row in rows],
                         [same_time["assessment_id"], newer["assessment_id"], older["assessment_id"]])
        briefs = [format_brief(row) for row in rows]
        self.assertEqual([row["status"] for row in rows], ["on_track"] * 3)
        refreshed = self.store.get_journey(journey.journey_id)
        self.assertTrue(refreshed.has_assessments)
        clone = self.store.save_journey(journey.journey_id, replace(draft, name="After assessment"),
                                       refreshed.edit_token)
        self.assertEqual(clone.outcome, "cloned")
        self.assertEqual(self.store.list_assessments(clone.journey_id), [])
        self.assertEqual(self.store.list_assessments(journey.journey_id), rows)
        self.assertEqual([format_brief(row) for row in self.store.list_assessments(journey.journey_id)], briefs)

    def test_list_journeys_orders_by_creation_then_id_and_reports_assessment_flag(self):
        first, second, third = [self.store.create_draft(TIMED) for _ in range(3)]
        self.store.assess_journey(synthetic_spec(journey_id=second.journey_id,
                                                transfer_id=second.transfer["transfer_id"]))
        with self.psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE journeys SET created_at = '2200-01-02T00:00:00Z' WHERE journey_id = ANY(%s)",
                         ([first.journey_id, second.journey_id],))
            conn.execute("UPDATE journeys SET created_at = '2200-01-01T00:00:00Z' WHERE journey_id = %s",
                         (third.journey_id,))
        rows = self.store.list_journeys(limit=2)
        self.assertEqual([row["journey_id"] for row in rows], [second.journey_id, first.journey_id])
        self.assertEqual(rows[0], dict(journey_id=second.journey_id, journey_name=second.name,
                                       service_date=second.service_date, has_assessments=True))
        self.assertFalse(rows[1]["has_assessments"])

    def test_import_replay_preserves_identity_and_conflicting_payload_rolls_back(self):
        snapshot = load_synthetic_212_snapshot()
        run = self.store.import_snapshot(snapshot)
        scope = ("TPE10912", 0, "TPE10600")
        rows = self.store.list_run_observations(run, *scope)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.import_snapshot(snapshot), run)
        self.assertEqual(self.store.list_run_observations(run, *scope), rows)
        self.assertEqual(self.store.list_run_observations(run, "TPE10912", 0, "TPE10606"), [])
        raw = dict(rows[0]["raw_payload"], EstimateTime=481)
        conflict = freeze_n1_fixture(json.dumps([raw]), scope=snapshot.scope,
                                     requested_at=snapshot.requested_at, fetched_at=snapshot.fetched_at,
                                     run_key=snapshot.run_key)
        with self.assertRaisesRegex(JourneyError, "different frozen snapshot"):
            self.store.import_snapshot(conflict)
        self.assertEqual(self.store.list_run_observations(run, *scope), rows)
        with self.psycopg.connect(self.dsn) as connection:
            payload, = connection.execute(
                "SELECT snapshot_payload::text FROM ingestion_runs WHERE run_id = %s", (run,),
            ).fetchone()
            self.assertEqual(parse_strict_json(payload), parse_strict_json(snapshot.records_json))

    def test_completed_empty_run_has_no_observations(self):
        snapshot = replace(load_synthetic_212_snapshot(), records_json="[]", run_key="synthetic-212-empty-test")
        run = self.store.import_snapshot(snapshot)
        self.assertGreater(run, 0)
        self.assertEqual(self.store.list_run_observations(run, "TPE10912", 0, "TPE10600"), [])
        with self.psycopg.connect(self.dsn) as connection:
            self.assertEqual(connection.execute(
                "SELECT status, jsonb_array_length(snapshot_payload) FROM ingestion_runs WHERE run_id = %s",
                (run,),
            ).fetchone(), ("completed", 0))

    def test_offline_observation_persists_linked_insufficient_evidence_brief(self):
        snapshot = load_synthetic_212_snapshot()
        run = self.store.import_snapshot(snapshot)
        observation, = self.store.list_run_observations(run, "TPE10912", 0, "TPE10600")
        journey = self.store.create_draft(REPLAY_DRAFT)
        spec = observation_assessment_spec(journey, observation)
        assessment = self.store.assess_journey(spec)
        self.assertEqual(self.store.assess_journey(spec), assessment)
        row, = self.store.list_assessments(journey.journey_id)
        self.assertEqual(row["assessment_id"], assessment["assessment_id"])
        self.assertEqual(row["evidence_observation_id"], observation["observation_id"])
        self.assertEqual(datetime.fromisoformat(row["evaluated_at"]), datetime.fromisoformat(snapshot.fetched_at))
        context_observation, = row["input_snapshot"]["context"]["observations"]
        for clock in ("source_updated_at", "fetched_at", "ingested_at"):
            self.assertEqual(datetime.fromisoformat(context_observation[clock]),
                             datetime.fromisoformat(observation[clock]))
        frozen, = parse_strict_json(snapshot.records_json)
        self.assertEqual(datetime.fromisoformat(observation["source_updated_at"]),
                         datetime.fromisoformat(frozen["source_updated_at"]))
        self.assertEqual(datetime.fromisoformat(observation["fetched_at"]),
                         datetime.fromisoformat(snapshot.fetched_at))
        self.assertEqual(context_observation["raw_payload"], frozen["raw_payload"])
        self.assertIn("non_normal_stop_status", row["input_snapshot"]["quality_issues"])
        brief = build_brief(row)
        self.assertEqual((brief["status"], brief["reason_code"]), ("insufficient_evidence", "evidence_unusable"))
        self.assertEqual(brief["arrival_interval"], {"earliest": None, "latest": None})
        self.assertIsNone(brief["boarding_deadline"])
        self.assertIsNone(brief["minimum_buffer"])
        self.assertIn("Synthetic N1 observation fixture", brief["evidence_source"])
        with self.psycopg.connect(self.dsn) as connection:
            requested, fetched = connection.execute("""
                SELECT requested_at, snapshot_fetched_at FROM ingestion_runs WHERE run_id = %s
            """, (run,)).fetchone()
        self.assertEqual(requested, datetime.fromisoformat(snapshot.requested_at))
        self.assertEqual(fetched, datetime.fromisoformat(snapshot.fetched_at))

    def test_mocked_live_persistence_failure_replay_and_no_downstream_prediction(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        raw = live_payload(StopStatus=0, SrcUpdateTime=timestamp, UpdateTime=timestamp)
        opener = http_mock(json.dumps([raw]))
        with TemporaryDirectory() as temporary, self.psycopg.connect(self.dsn, autocommit=True) as admin:
            directory = Path(temporary) / "live-snapshots"
            admin.execute("REVOKE INSERT ON source_observations FROM journey_writer")
            try:
                with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener", return_value=opener):
                    with self.assertRaisesRegex(PipelineError, "Replay saved snapshot"):
                        acquire_live_boarding(self.store, directory, confirmed=True)
            finally:
                admin.execute("GRANT INSERT ON source_observations TO journey_writer")
            self.assertEqual(opener.open.call_count, 2)
            artifact, = list_live_artifacts(directory)
            frozen = FrozenSnapshot.from_json((directory / artifact).read_text())
            self.assertEqual(admin.execute("SELECT count(*) FROM ingestion_runs WHERE run_key = %s",
                                           (frozen.run_key,)).fetchone()[0], 0)
            with patch.dict(os.environ, dict.fromkeys(CREDENTIALS, "")), patch("planaware.live.build_opener") as no_http:
                run_id, fetched_at = replay_live_boarding(self.store, directory, artifact)
                observation, = self.store.list_run_observations(run_id, "TPE10912", 0, "TPE10600")
                self.assertEqual(replay_live_boarding(self.store, directory, artifact), (run_id, fetched_at))
            no_http.assert_not_called()
            self.assertEqual(self.store.list_run_observations(run_id, "TPE10912", 0, "TPE10600"), [observation])
            self.assertEqual(observation["raw_payload"], raw)
            self.assertEqual(datetime.fromisoformat(observation["fetched_at"]), datetime.fromisoformat(fetched_at))

            second_opener = http_mock(json.dumps([raw]))
            with patch.dict(os.environ, CREDENTIALS), patch("planaware.live.build_opener", return_value=second_opener):
                second_run, _ = acquire_live_boarding(self.store, directory, confirmed=True)
            self.assertEqual(second_opener.open.call_count, 2)
            self.assertNotEqual(second_run, run_id)
            self.assertEqual(len(self.store.list_run_observations(second_run, "TPE10912", 0, "TPE10600")), 1)

            service_date = datetime.fromisoformat(fetched_at).astimezone(TAIPEI).date()
            departure = datetime.combine(service_date, TIMED.train.departure_at.timetz())
            draft = replace(TIMED, service_date=service_date, train=replace(TIMED.train, departure_at=departure),
                            confirm_service_date_assumptions=True)
            journey = self.store.create_draft(draft)
            result = self.store.assess_journey(observation_assessment_spec(journey, observation))
            self.assertEqual((result["status"], result["reason_code"]),
                             ("insufficient_evidence", "missing_arrival_interval"))
            row, = self.store.list_assessments(journey.journey_id)
            brief = build_brief(row)
            self.assertEqual(brief["arrival_interval"], {"earliest": None, "latest": None})
            self.assertIsNone(brief["boarding_deadline"])
            self.assertIsNone(brief["minimum_buffer"])
