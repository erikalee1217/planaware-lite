"""Real PostgreSQL integration tests; no third-party Python packages required.

Creates and removes only its own temporary cluster. Never reads DATABASE_URL,
never connects to an existing server, and makes no network/API requests.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from planaware.n1 import FrozenSnapshot, N1Scope, ValidationError, freeze_n1_fixture, persist_snapshot
from planaware.live import PipelineError, fetch_once
from unittest.mock import patch
from test_live import CREDENTIALS, http_mock


ROOT = Path(__file__).resolve().parents[1]
DB = None


class DisposablePostgres:
    def __init__(self):
        bindir = os.environ.get("PG_TEST_BINDIR")
        psql = shutil.which("psql")
        if not bindir and psql:
            bindir = str(Path(psql).resolve().parent)
        if not bindir:
            raise RuntimeError("PostgreSQL server binaries required; set PG_TEST_BINDIR")
        self.bin = Path(bindir)
        for name in ("initdb", "pg_ctl", "psql"):
            if not (self.bin / name).is_file():
                raise RuntimeError(f"Missing PostgreSQL binary: {self.bin / name}")
        # Do not inherit an existing database/service/password configuration.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        work = ROOT / "work"
        work.mkdir(exist_ok=True)
        self.cluster_tmp = tempfile.TemporaryDirectory(prefix="postgres-test-", dir=work)
        # Unix socket paths have a small OS length limit; keep this path short.
        socket_root = "/tmp" if Path("/tmp").is_dir() else tempfile.gettempdir()
        self.socket_tmp = tempfile.TemporaryDirectory(prefix="pa-pg-", dir=socket_root)
        self.data = Path(self.cluster_tmp.name) / "data"
        self.started = False

    def command(self, name, *args, timeout=45):
        return subprocess.run(
            [str(self.bin / name), *map(str, args)], env=self.env,
            text=True, capture_output=True, timeout=timeout,
        )

    def start(self):
        result = self.command(
            "initdb", "-D", self.data, "-U", "planaware_test_owner",
            "-A", "trust", "--no-locale", "-E", "UTF8",
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        # No TCP listener. Each test invocation has a unique private socket dir.
        options = (
            f"-F -k {self.socket_tmp.name} -p 55432 "
            "-c listen_addresses='' -c unix_socket_permissions=0700"
        )
        result = self.command(
            "pg_ctl", "-D", self.data, "-l",
            Path(self.cluster_tmp.name) / "server.log", "-o", options, "-w", "start",
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        self.started = True

    def sql(self, sql, database="planaware_schema_test"):
        result = subprocess.run(
            [str(self.bin / "psql"), "-X", "-h", self.socket_tmp.name,
             "-p", "55432", "-U", "planaware_test_owner", "-d", database,
             "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-A", "-t", "-q"],
            input=sql, env=self.env, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        return result.stdout.strip()

    def close(self):
        if self.started:
            result = self.command("pg_ctl", "-D", self.data, "-m", "immediate", "-w", "stop")
            if result.returncode:
                raise RuntimeError("Could not stop owned test cluster: " + result.stderr)
            self.started = False
        self.socket_tmp.cleanup()
        self.cluster_tmp.cleanup()


class DisposableDockerPostgres:
    """Optional backend using an already-installed image; no pull or host ports."""
    def __init__(self):
        self.docker = shutil.which("docker")
        if not self.docker:
            raise RuntimeError("Docker CLI is required for PG_TEST_BACKEND=docker")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        self.image = os.environ.get("PG_TEST_IMAGE", "postgres:14")
        self.owner = uuid.uuid4().hex
        self.container = None

    def command(self, *args, input=None, timeout=30):
        return subprocess.run([self.docker, *args], input=input, env=self.env,
                              text=True, capture_output=True, timeout=timeout)

    def start(self):
        result = self.command("image", "inspect", self.image)
        if result.returncode:
            raise RuntimeError("Required local image unavailable; no image was pulled: " + self.image)
        result = self.command(
            "run", "--pull=never", "--detach", "--network", "none",
            "--name", "planaware-schema-test-" + self.owner,
            "--label", "planaware.schema-test=" + self.owner,
            "--tmpfs", "/var/lib/postgresql/data:rw,nosuid",
            "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env", "POSTGRES_USER=planaware_test_owner",
            "--env", "POSTGRES_DB=postgres", self.image,
            "postgres", "-c", "listen_addresses=",
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        self.container = result.stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.command("exec", self.container, "pg_isready", "-U", "planaware_test_owner")
            if result.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError("Owned disposable PostgreSQL container did not become ready")

    def sql(self, sql, database="planaware_schema_test"):
        result = self.command(
            "exec", "--interactive", self.container, "psql", "-X", "-U",
            "planaware_test_owner", "-d", database, "-v", "ON_ERROR_STOP=1",
            "-v", "VERBOSITY=verbose", "-A", "-t", "-q", input=sql,
        )
        if result.returncode:
            raise AssertionError(result.stderr + result.stdout)
        return result.stdout.strip()

    def close(self):
        if self.container is None:
            return
        result = self.command("inspect", "--format",
                              '{{index .Config.Labels "planaware.schema-test"}}', self.container)
        if result.returncode or result.stdout.strip() != self.owner:
            raise RuntimeError("Refusing to remove a container without the matching test ownership label")
        result = self.command("rm", "--force", self.container)
        if result.returncode:
            raise RuntimeError("Could not remove owned test container: " + result.stderr)
        self.container = None


def setUpModule():
    global DB
    backend = os.environ.get("PG_TEST_BACKEND", "local")
    if backend not in ("local", "docker"):
        raise RuntimeError("PG_TEST_BACKEND must be local or docker")
    DB = DisposableDockerPostgres() if backend == "docker" else DisposablePostgres()
    unittest.addModuleCleanup(DB.close)
    DB.start()
    print("Integration server: PostgreSQL " + DB.sql("SHOW server_version", database="postgres"))
    DB.sql("CREATE DATABASE planaware_schema_test", database="postgres")
    for migration in sorted((ROOT / "sql/migrations").glob("*.sql")):
        DB.sql(migration.read_text())
    DB.sql((ROOT / "sql/seeds/001_synthetic_journey.sql").read_text())


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def record(**changes):
    value = {
        "route_uid": "TPE11023", "direction": 0, "stop_uid": "TPE40828",
        "estimate_seconds": 120, "stop_status": 0,
        "source_updated_at": "2026-11-17T14:00:00+08:00",
        "raw_payload": {"synthetic_test_only": True, "StopUID": "TPE40828",
                        "EstimateTime": 120, "nested": {"text": "測試", "unknown": None}},
    }
    value.update(changes)
    return value


def snapshot(key="test-run", records=None, fetched="2026-11-17 14:00:05+08",
             requested="2026-11-17 14:00:01+08"):
    if records is None:
        records = [record()]
    return (
        "persist_tdx_n1_snapshot(" + quote(key)
        + ", " + quote(requested) + ", " + quote(fetched) + ", "
        + quote(json.dumps(records, ensure_ascii=False)) + "::jsonb)"
    )


FIXTURE = """
BEGIN;
CREATE FUNCTION pg_temp.assert_true(ok boolean, message text) RETURNS void
LANGUAGE plpgsql AS $$ BEGIN
    IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION '%', message; END IF;
END $$;
CREATE FUNCTION pg_temp.expect_error(statement text, expected_state text)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE actual_state text;
BEGIN
    BEGIN
        EXECUTE statement;
    EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS actual_state = RETURNED_SQLSTATE;
        IF actual_state <> expected_state THEN
            RAISE EXCEPTION 'Expected SQLSTATE %, got %: %', expected_state, actual_state, SQLERRM;
        END IF;
        RETURN;
    END;
    RAISE EXCEPTION 'Expected SQLSTATE %, statement succeeded', expected_state;
END $$;
CREATE TEMP TABLE context (name text PRIMARY KEY, id bigint NOT NULL);
WITH inserted AS (
    INSERT INTO journeys(journey_name, service_date)
    VALUES ('j1', '2026-11-17'), ('j2', '2026-11-17')
    RETURNING journey_name, journey_id
) INSERT INTO context SELECT journey_name, journey_id FROM inserted;
WITH inserted AS (
    INSERT INTO journey_legs(journey_id, leg_order, transport_mode)
    SELECT id, 1, 'bus' FROM context WHERE name='j1' RETURNING leg_id
) INSERT INTO context SELECT 'l1', leg_id FROM inserted;
WITH inserted AS (
    INSERT INTO journey_legs(journey_id, leg_order, transport_mode)
    SELECT id, 2, 'metro' FROM context WHERE name='j1' RETURNING leg_id
) INSERT INTO context SELECT 'l2', leg_id FROM inserted;
WITH inserted AS (
    INSERT INTO journey_legs(journey_id, leg_order, transport_mode)
    SELECT id, 1, 'bus' FROM context WHERE name='j2' RETURNING leg_id
) INSERT INTO context SELECT 'other_leg', leg_id FROM inserted;
"""


def ctx(name):
    return f"(SELECT id FROM context WHERE name={quote(name)})"


# Compare complete saved rows, including IDs, raw values and insertion/completion
# clocks. Rejected retries must preserve more than just the observation count.
CAPTURE_RUN = """
CREATE TEMP TABLE frozen_before AS SELECT
    (SELECT to_jsonb(r) FROM ingestion_runs r WHERE run_key='test-run') AS run_row,
    (SELECT jsonb_agg(to_jsonb(o) ORDER BY record_index) FROM source_observations o
     WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run')) AS observations;
"""
ASSERT_RUN_UNCHANGED = """
SELECT pg_temp.assert_true((SELECT
    run_row = (SELECT to_jsonb(r) FROM ingestion_runs r WHERE run_key='test-run')
    AND observations = (SELECT jsonb_agg(to_jsonb(o) ORDER BY record_index)
        FROM source_observations o WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run'))
    FROM frozen_before), 'all saved run/observation fields unchanged');
"""


class SchemaTests(unittest.TestCase):
    def case(self, body):
        return DB.sql(FIXTURE + body + "\nROLLBACK;")

    def rejects(self, statement, sqlstate):
        self.case(f"SELECT pg_temp.expect_error({quote(statement)}, '{sqlstate}');")

    def test_live_mocked_fetch_db_failure_then_fresh_process_replay_and_new_acquisition(self):
        # Real CLI/psql/PG across process boundaries. All HTTP is synthetic.
        (ROOT / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as directory:
            directory = Path(directory)
            artifact = directory / 'frozen.json'
            scope = N1Scope.from_json((ROOT / 'tests/fixtures/n1_298_scope.json').read_text())
            env = {key: value for key, value in os.environ.items() if not key.startswith(('PG', 'TDX_'))}
            if isinstance(DB, DisposableDockerPostgres):
                # Forward psql through the already-owned container, no host ports.
                wrapper = directory / 'psql'
                wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(DB.docker)
                    + ' exec -i ' + shlex.quote(DB.container)
                    + ' psql -U planaware_test_owner "$@"\n')
                wrapper.chmod(0o700)
                env['PATH'] = str(directory) + os.pathsep + env.get('PATH', '')
            else:
                env.update(PGHOST=DB.socket_tmp.name, PGPORT='55432', PGUSER='planaware_test_owner')
                env['PATH'] = str(DB.bin) + os.pathsep + env.get('PATH', '')
            keys = []
            try:
                def fail(_):
                    raise RuntimeError('synthetic database outage')
                with patch.dict(os.environ, CREDENTIALS), self.assertRaises(PipelineError):
                    fetch_once(scope=scope, city='Taipei', max_records=2, output=artifact,
                        execute_sql=fail, confirm_free_quota=True, opener=http_mock())
                saved = FrozenSnapshot.from_json(artifact.read_text())
                keys.append(saved.run_key)
                self.assertEqual(DB.sql('SELECT count(*) FROM ingestion_runs WHERE run_key=' + quote(saved.run_key)), '0')
                original = artifact.read_bytes()
                command = [os.sys.executable, '-m', 'planaware.n1', 'replay', str(artifact),
                           '--database', 'planaware_schema_test']
                first = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(first.returncode, 0, first.stderr)
                result = json.loads(first.stdout)
                self.assertEqual((result['authentication_requests'], result['data_requests']), (0, 0))
                before = DB.sql('SELECT jsonb_agg(to_jsonb(o) ORDER BY record_index) FROM source_observations o WHERE run_id=' + str(result['database_run_id']))
                second = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(second.stdout)['database_run_id'], result['database_run_id'])
                after = DB.sql('SELECT jsonb_agg(to_jsonb(o) ORDER BY record_index) FROM source_observations o WHERE run_id=' + str(result['database_run_id']))
                self.assertEqual(before, after)
                self.assertEqual(artifact.read_bytes(), original)
                with patch.dict(os.environ, CREDENTIALS):
                    acquired = fetch_once(scope=scope, city='Taipei', max_records=2,
                        output=directory / 'second.json', execute_sql=DB.sql,
                        confirm_free_quota=True, opener=http_mock())
                keys.append(acquired['run_key'])
                self.assertNotEqual(acquired['run_key'], saved.run_key)
                self.assertEqual((acquired['authentication_requests'], acquired['data_requests']), (1, 1))
                self.assertEqual(DB.sql('SELECT count(*) FROM source_observations WHERE run_id IN ('
                    + str(result['database_run_id']) + ',' + str(acquired['database_run_id']) + ')'), '4')
            finally:
                # Only delete the exact generated test runs in our disposable DB.
                for key in keys:
                    DB.sql('DELETE FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='
                           + quote(key) + '); DELETE FROM ingestion_runs WHERE run_key=' + quote(key))

    def test_live_mocked_empty_response_replays_in_postgres(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'work') as directory:
            output = Path(directory) / 'empty.json'
            scope = N1Scope.from_json((ROOT / 'tests/fixtures/n1_298_scope.json').read_text())
            def execute(statement):
                return self.case(statement + """
                    SELECT pg_temp.assert_true((SELECT snapshot_payload='[]'::jsonb AND status='completed'
                        FROM ingestion_runs), 'empty run retained');
                    SELECT pg_temp.assert_true((SELECT count(*)=0 FROM source_observations), 'no fabricated rows');
                """)
            with patch.dict(os.environ, CREDENTIALS):
                result = fetch_once(scope=scope, city='Taipei', max_records=2, output=output,
                    execute_sql=execute, confirm_free_quota=True, opener=http_mock('[]'))
            self.assertEqual(result['record_count'], 0)
            saved = FrozenSnapshot.from_json(output.read_text())
            ids = self.case(saved.to_sql() + saved.to_sql()).split()
            self.assertEqual(ids[0], ids[1])

    def n1_fixture(self, text=None, **changes):
        fixtures = ROOT / 'tests/fixtures'
        args = dict(scope=N1Scope.from_json((fixtures / 'n1_298_scope.json').read_text()),
                    requested_at='2026-11-17T19:42:00+08:00',
                    fetched_at='2026-11-17T19:42:06+08:00', run_key='test-run')
        args.update(changes)
        return freeze_n1_fixture(text if text is not None else (fixtures / 'n1_298_observed.json').read_text(), **args)

    def test_adapter_persists_original_records_order_and_three_clocks(self):
        saved = self.n1_fixture()
        rows = json.loads(saved.records_json)
        def execute(statement):
            return self.case(statement + f"""
                SELECT pg_temp.assert_true((SELECT count(*)=2 FROM source_observations), 'both records');
                SELECT pg_temp.assert_true((SELECT stop_uid='TPE40830' AND estimate_seconds=4535
                    AND stop_status=1 AND raw_payload={quote(json.dumps(rows[0]['raw_payload']))}::jsonb
                    AND source_updated_at='2026-11-17 11:41:55+00'::timestamptz
                    AND fetched_at='2026-11-17 11:42:06+00'::timestamptz
                    AND ingested_at >= transaction_timestamp() AND ingested_at <> fetched_at
                    FROM source_observations WHERE record_index=0), 'original raw/order/clocks');
                SELECT pg_temp.assert_true((SELECT snapshot_payload->0->>'tdx_updated_at'='2026-11-17T19:42:05+08:00'
                    AND snapshot_payload->0->'quality_flags' ? 'not_a_normal_status_prediction'
                    FROM ingestion_runs WHERE run_key='test-run'), 'extra clocks and quality retained');
            """)
        self.assertGreater(persist_snapshot(saved, execute), 0)

    def test_adapter_saved_artifact_replay_preserves_all_rows_and_clocks(self):
        saved = self.n1_fixture()
        replay = FrozenSnapshot.from_json(saved.to_json())
        result = self.case(saved.to_sql() + CAPTURE_RUN + replay.to_sql() + ASSERT_RUN_UNCHANGED)
        ids = result.split()
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])

    def test_adapter_conflicting_content_order_or_fetch_cannot_overwrite(self):
        saved = self.n1_fixture()
        raw = [row['raw_payload'] for row in json.loads(saved.records_json)]
        changed = json.loads(json.dumps(raw))
        changed[1]['EstimateTime'] = 20
        conflicts = [self.n1_fixture(json.dumps(changed)),
                     self.n1_fixture(json.dumps(list(reversed(raw)))),
                     self.n1_fixture(fetched_at='2026-11-17T19:42:07+08:00')]
        for conflict in conflicts:
            with self.subTest(conflict=conflict.fetched_at):
                # Adapter accepts valid source evidence; only the original SQL
                # function decides whether the logical key already owns it.
                self.case(saved.to_sql() + CAPTURE_RUN
                          + f"SELECT pg_temp.expect_error({quote(conflict.to_sql())}, '23514');"
                          + ASSERT_RUN_UNCHANGED)

    def test_adapter_distinct_acquisitions_keep_identical_records(self):
        first, second = self.n1_fixture(run_key=None), self.n1_fixture(run_key=None)
        self.assertNotEqual(first.run_key, second.run_key)
        self.case(first.to_sql() + second.to_sql() + """
            SELECT pg_temp.assert_true((SELECT count(*)=2 FROM ingestion_runs), 'two logical acquisitions');
            SELECT pg_temp.assert_true((SELECT count(*)=4 FROM source_observations), 'retain identical observations');
            SELECT pg_temp.assert_true((SELECT count(DISTINCT snapshot_payload)=1 FROM ingestion_runs), 'same values');
        """)

    def test_adapter_configurable_route_persists_without_schema_changes(self):
        scope = N1Scope('SYNTHETIC_ROUTE', 1, ('SYNTHETIC_STOP',))
        saved = self.n1_fixture(json.dumps([{'RouteUID': scope.route_uid,
            'Direction': scope.direction, 'StopUID': scope.stop_uids[0]}]), scope=scope)
        self.case(saved.to_sql() + """
            SELECT pg_temp.assert_true((SELECT route_uid='SYNTHETIC_ROUTE' AND direction=1
                AND stop_uid='SYNTHETIC_STOP' FROM source_observations), 'configured identity persists');
        """)

    def test_adapter_malformed_second_record_never_calls_database(self):
        records = [row['raw_payload'] for row in json.loads(self.n1_fixture().records_json)]
        records[1]['Direction'] = '0'
        calls = []
        def execute(statement):
            calls.append(statement)
            return DB.sql(statement)
        with self.assertRaises(ValidationError):
            persist_snapshot(self.n1_fixture(json.dumps(records)), execute)
        self.assertEqual(calls, [])
        self.assertEqual(DB.sql("SELECT count(*) FROM ingestion_runs WHERE run_key='test-run'"), '0')

    def test_adapter_missing_times_and_estimates_remain_null(self):
        records = [row['raw_payload'] for row in json.loads(self.n1_fixture().records_json)]
        del records[0]['SrcUpdateTime']
        del records[0]['EstimateTime']
        records[1]['SrcUpdateTime'] = None
        records[1]['EstimateTime'] = None
        self.case(self.n1_fixture(json.dumps(records)).to_sql() + """
            SELECT pg_temp.assert_true((SELECT count(*)=2 FROM source_observations
                WHERE source_updated_at IS NULL AND estimate_seconds IS NULL AND stop_status=1), 'no invented values');
            SELECT pg_temp.assert_true((SELECT NOT raw_payload ? 'SrcUpdateTime' AND NOT raw_payload ? 'EstimateTime'
                FROM source_observations WHERE record_index=0), 'missing raw keys');
            SELECT pg_temp.assert_true((SELECT raw_payload->'SrcUpdateTime'='null'::jsonb
                AND raw_payload->'EstimateTime'='null'::jsonb FROM source_observations WHERE record_index=1), 'explicit raw nulls');
        """)

    def test_adapter_sql_literals_and_raw_numeric_precision_survive_postgres(self):
        raw = [row['raw_payload'] for row in json.loads(self.n1_fixture().records_json)]
        raw[0]['unknown_text'] = "quote' slash\\ newline\n測試; DROP TABLE journeys; --"
        text = json.dumps(raw).replace('"EstimateTime": 4535,',
                '"unknown_number": 0.12345678901234567890123456789, "EstimateTime": 4535,')
        saved = self.n1_fixture(text, run_key="key'\\; DROP TABLE journeys; --")
        self.case(saved.to_sql() + f"""
            SELECT pg_temp.assert_true((SELECT raw_payload->>'unknown_text'={quote(raw[0]['unknown_text'])}
                AND (raw_payload->>'unknown_number')::numeric=0.12345678901234567890123456789
                FROM source_observations WHERE record_index=0), 'literal and decimal preservation');
            SELECT pg_temp.assert_true(to_regclass('journeys') IS NOT NULL, 'SQL stays literal data');
        """)

    def test_duplicate_leg_order_rejected(self):
        self.rejects(f"INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES ({ctx('j1')},1,'bus')", "23505")

    def test_order_reusable_across_journeys(self):
        self.case("SELECT pg_temp.assert_true((SELECT count(*)=2 FROM journey_legs WHERE leg_id IN (SELECT id FROM context WHERE name IN ('l1','other_leg')) AND leg_order=1), 'separate journeys share order');")

    def test_invalid_mode_rejected(self):
        self.rejects(f"INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES ({ctx('j1')},3,'ferry')", "23514")

    def test_arrival_before_or_equal_departure_rejected(self):
        for arrival in ("13:59:00", "14:00:00"):
            with self.subTest(arrival=arrival):
                self.rejects(f"UPDATE journey_legs SET planned_departure_at='2026-11-17 14:00+08', planned_arrival_at='2026-11-17 {arrival}+08' WHERE leg_id={ctx('l1')}", "23514")

    def test_optional_leg_times_and_missing_source_time(self):
        self.case(f"""
            UPDATE journey_legs SET planned_departure_at='2026-11-17 14:00+08' WHERE leg_id={ctx('l1')};
            UPDATE journey_legs SET planned_arrival_at='2026-11-17 14:00+08' WHERE leg_id={ctx('l2')};
            SELECT {snapshot(records=[record(source_updated_at=None, estimate_seconds=None, stop_status=None)])};
            SELECT pg_temp.assert_true((SELECT source_updated_at IS NULL AND estimate_seconds IS NULL AND stop_status IS NULL FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run')), 'nulls retained');
        """)

    def test_cross_journey_transfer_rejected_for_either_endpoint(self):
        for from_name, to_name in (("l1", "other_leg"), ("other_leg", "l2")):
            with self.subTest(from_name=from_name):
                self.rejects(f"INSERT INTO transfer_constraints(journey_id,from_leg_id,to_leg_id,min_transfer_minutes,assumption_source) VALUES ({ctx('j1')},{ctx(from_name)},{ctx(to_name)},5,'synthetic')", "23503")

    def test_transfer_self_negative_buffer_and_duplicate_rejected(self):
        base = f"INSERT INTO transfer_constraints(journey_id,from_leg_id,to_leg_id,min_transfer_minutes,assumption_source) VALUES ({ctx('j1')},{ctx('l1')},"
        self.rejects(base + f"{ctx('l1')},5,'synthetic')", "23514")
        self.rejects(base + f"{ctx('l2')},-1,'synthetic')", "23514")
        valid = base + f"{ctx('l2')},5,'synthetic')"
        self.case(valid + "; SELECT pg_temp.expect_error(" + quote(valid) + ", '23505');")

    def test_completed_run_replay_is_noop(self):
        self.case(f"SELECT {snapshot()};" + CAPTURE_RUN + f"""
            SELECT pg_temp.assert_true({snapshot()}=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run'), 'same run ID returned');
            SELECT pg_temp.assert_true((SELECT count(*)=1 FROM ingestion_runs WHERE run_key='test-run'), 'one run');
            SELECT pg_temp.assert_true((SELECT count(*)=1 FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run')), 'one observation');
        """ + ASSERT_RUN_UNCHANGED)

    def test_changed_snapshot_content_order_or_retrieval_rejected(self):
        records = [record(), record(stop_uid="TPE40830")]
        alternatives = [
            snapshot(records=[record(estimate_seconds=121), records[1]]),
            snapshot(records=list(reversed(records))),
            snapshot(records=records, fetched="2026-11-17 14:00:06+08"),
            snapshot(records=records, requested="2026-11-17 14:00:02+08"),
            # Same normalized ETA but changed original source payload.
            snapshot(records=[record(raw_payload={"synthetic_test_only": True, "changed": True}), records[1]]),
        ]
        for changed in alternatives:
            with self.subTest(changed=changed):
                self.case(f"SELECT {snapshot(records=records)};" + CAPTURE_RUN
                          + f"SELECT pg_temp.expect_error({quote('SELECT ' + changed)}, '23514');"
                          + ASSERT_RUN_UNCHANGED)

    def test_distinct_runs_preserve_identical_eta(self):
        new_fetch_same_key = snapshot('run-a', requested='2026-11-17 14:05:01+08', fetched='2026-11-17 14:05:05+08')
        new_fetch_new_key = snapshot('run-b', requested='2026-11-17 14:05:01+08', fetched='2026-11-17 14:05:05+08')
        self.case(f"""
            SELECT {snapshot('run-a')};
            SELECT pg_temp.expect_error({quote('SELECT ' + new_fetch_same_key)}, '23514');
            SELECT {new_fetch_new_key};
            SELECT pg_temp.assert_true((SELECT count(*)=2 AND count(DISTINCT o.run_id)=2 FROM source_observations o JOIN ingestion_runs r USING(run_id) WHERE r.run_key IN ('run-a','run-b')), 'two runs retained');
            SELECT pg_temp.assert_true((SELECT count(DISTINCT o.raw_payload)=1 AND count(DISTINCT o.source_updated_at)=1 AND count(DISTINCT o.fetched_at)=2 FROM source_observations o JOIN ingestion_runs r USING(run_id) WHERE r.run_key IN ('run-a','run-b')), 'same source observation, separate real retrieval times');
        """)

    def test_jsonb_and_three_clocks_retained_without_journey_reference(self):
        self.case(f"""
            SELECT {snapshot()};
            SELECT pg_temp.assert_true((SELECT raw_payload={quote(json.dumps(record()['raw_payload'], ensure_ascii=False))}::jsonb
                AND source_updated_at='2026-11-17 06:00:00+00'::timestamptz
                AND fetched_at='2026-11-17 06:00:05+00'::timestamptz
                AND ingested_at IS NOT NULL AND ingested_at<>fetched_at
                FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run')), 'payload and clocks retained');
            SELECT pg_temp.assert_true(NOT EXISTS (SELECT FROM information_schema.columns WHERE table_name='source_observations' AND column_name IN ('journey_id','leg_id')), 'observations independent');
        """)

    def test_invalid_batch_rolls_back_run_and_all_observations(self):
        bad = snapshot(records=[record(), record(stop_uid=None)])
        self.case(f"""
            SELECT pg_temp.expect_error({quote('SELECT ' + bad)}, '23502');
            SELECT pg_temp.assert_true(NOT EXISTS (SELECT FROM ingestion_runs WHERE run_key='test-run'), 'no half-written run');
        """)

    def test_invalid_batch_preserves_preexisting_started_run_then_replays(self):
        bad = snapshot(records=[record(), record(stop_uid=None)])
        self.case(f"""
            INSERT INTO ingestion_runs(run_key,requested_at)
            VALUES ('test-run','2026-11-17 14:00:01+08');
            CREATE TEMP TABLE started_before AS SELECT to_jsonb(r) AS run_row FROM ingestion_runs r WHERE run_key='test-run';
            SELECT pg_temp.expect_error({quote('SELECT ' + bad)}, '23502');
            SELECT pg_temp.assert_true((SELECT to_jsonb(r)=(SELECT run_row FROM started_before) FROM ingestion_runs r WHERE run_key='test-run'), 'started run unchanged after failed batch');
            SELECT pg_temp.assert_true(NOT EXISTS (SELECT FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run')), 'no partial observations');
            SELECT {snapshot()};
            SELECT pg_temp.assert_true((SELECT status='completed' FROM ingestion_runs WHERE run_key='test-run'), 'empty started run accepts frozen retry');
        """)

    def test_empty_snapshot_replay_and_changed_empty_snapshot(self):
        self.case(f"""
            SELECT {snapshot(records=[])}; SELECT {snapshot(records=[])};
            SELECT pg_temp.assert_true((SELECT status='completed' AND snapshot_payload='[]'::jsonb FROM ingestion_runs WHERE run_key='test-run'), 'empty completed snapshot');
            SELECT pg_temp.expect_error({quote('SELECT ' + snapshot())}, '23514');
        """)

    def test_run_states_and_single_source_restrictions(self):
        self.rejects("INSERT INTO ingestion_runs(run_key,source_system) VALUES ('wrong-source','other')", "23514")
        self.rejects("INSERT INTO ingestion_runs(run_key,status) VALUES ('incomplete','completed')", "23514")
        self.case(f"""
            INSERT INTO ingestion_runs(run_key,requested_at,status,completed_at)
            VALUES ('failed-run','2026-11-17 14:00:01+08','failed',clock_timestamp());
            SELECT pg_temp.expect_error({quote('SELECT ' + snapshot('failed-run'))}, '23514');
        """)

    def test_record_index_unique_inside_run(self):
        self.case(f"""
            SELECT {snapshot()};
            SELECT pg_temp.expect_error('INSERT INTO source_observations(run_id,record_index,route_uid,direction,stop_uid,fetched_at,raw_payload) SELECT run_id,0,''TPE11023'',0,''TPE40828'',clock_timestamp(),''{{}}''::jsonb FROM ingestion_runs WHERE run_key=''test-run''', '23505');
        """)

    def test_history_evidence_versions_and_nullable_missing_evidence(self):
        insert = f"""INSERT INTO impact_assessments (
            journey_id,assessment_key,status,reason_code,model_version,
            reference_data_version,assumption_version,input_snapshot,evidence_observation_id,evaluated_at)
            SELECT {ctx('j1')}, %s, %s, 'synthetic-test', 'model-v1', 'reference-v1',
            'assumptions-v1', '{{"synthetic_test_only":true,"walking_minutes":5}}', %s, %s"""
        evidence = "(SELECT observation_id FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='test-run'))"
        first = insert % ("'assessment-1'", "'on_track'", evidence, "'2026-11-17 14:01+08'")
        second = insert % ("'assessment-2'", "'insufficient_evidence'", evidence, "'2026-11-17 14:10+08'")
        missing = insert % ("'assessment-3'", "'insufficient_evidence'", "NULL", "'2026-11-17 14:11+08'")
        self.case(f"""
            SELECT {snapshot()}; {first}; {second}; {missing};
            SELECT pg_temp.assert_true((SELECT count(*)=3 AND count(DISTINCT status)=2 FROM impact_assessments WHERE journey_id={ctx('j1')}), 'history retained');
            SELECT pg_temp.assert_true((SELECT count(*)=2 FROM impact_assessments a JOIN source_observations o ON o.observation_id=a.evidence_observation_id WHERE a.journey_id={ctx('j1')}), 'evidence linked');
            SELECT pg_temp.assert_true((SELECT assumption_version='assumptions-v1' AND reference_data_version='reference-v1' AND input_snapshot->>'walking_minutes'='5' FROM impact_assessments WHERE assessment_key='assessment-1'), 'versions and inputs retained');
            SELECT pg_temp.expect_error({quote(first)}, '23505');
            SELECT pg_temp.expect_error({quote(insert % ("'bad-evidence'", "'insufficient_evidence'", "-1", "clock_timestamp()"))}, '23503');
            SELECT pg_temp.expect_error('DELETE FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key=''test-run'')', '23503');
        """)

    def test_latest_source_state_not_latest_fetch_or_last_usable(self):
        self.case(f"""
            SELECT {snapshot('newer', [record(source_updated_at='2026-11-17T14:05:00+08:00', stop_status=1)])};
            SELECT {snapshot('late-old', [record(source_updated_at='2026-11-17T14:00:00+08:00')], fetched='2026-11-17 14:10+08')};
            SELECT {snapshot('unknown-time', [record(source_updated_at=None)], fetched='2026-11-17 14:11+08')};
            SELECT pg_temp.assert_true((SELECT stop_status=1 FROM source_observations
                WHERE run_id IN (SELECT run_id FROM ingestion_runs WHERE run_key IN ('newer','late-old','unknown-time'))
                ORDER BY source_updated_at DESC NULLS LAST, fetched_at DESC, observation_id DESC LIMIT 1), 'newest source state, even when unusable');
        """)

    def test_synthetic_seed_contains_only_reference_journey(self):
        self.case("""
            SELECT pg_temp.assert_true((SELECT count(*)=5 FROM journey_legs l JOIN journeys j USING(journey_id) WHERE j.journey_name LIKE 'SYNTHETIC DEMO:%'), 'three transit plus two walking legs');
            SELECT pg_temp.assert_true((SELECT count(*)=4 FROM transfer_constraints t JOIN journeys j USING(journey_id) WHERE j.journey_name LIKE 'SYNTHETIC DEMO:%'), 'four adjacent demo transfers');
            SELECT pg_temp.assert_true((SELECT sum(estimated_duration_seconds)=1140 FROM journey_legs l JOIN journeys j USING(journey_id) WHERE j.journey_name LIKE 'SYNTHETIC DEMO:%' AND transport_mode='walking'), '19 minutes of explicit synthetic walking');
            SELECT pg_temp.assert_true((SELECT sum(min_transfer_minutes)=7 FROM transfer_constraints t JOIN journeys j USING(journey_id) WHERE j.journey_name LIKE 'SYNTHETIC DEMO:%'), 'additional buffers exclude walking');
            SELECT pg_temp.assert_true((SELECT count(*)=2 FROM journey_legs l JOIN journeys j USING(journey_id) WHERE j.journey_name LIKE 'SYNTHETIC DEMO:%' AND route_uid IN ('TPE11023','THB1822')), 'verified bus reference IDs');
            SELECT pg_temp.assert_true(NOT EXISTS (SELECT FROM source_observations), 'seed invents no ETA');
        """)

    def test_concurrent_replay_preserves_one_snapshot(self):
        # Only this test commits; it still targets the owned disposable cluster.
        call = "SELECT " + snapshot("concurrent-replay") + ";"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: DB.sql(call), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(DB.sql("SELECT count(*) FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='concurrent-replay')"), "1")
        DB.sql("DELETE FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key='concurrent-replay'); DELETE FROM ingestion_runs WHERE run_key='concurrent-replay';")

    def test_concurrent_conflicting_retries_cannot_mix_snapshots(self):
        key = 'concurrent-conflict'
        versions = [
            [record(), record(stop_uid='TPE40830', estimate_seconds=180)],
            [record(estimate_seconds=121), record(stop_uid='TPE40830', estimate_seconds=181)],
        ]
        barrier = threading.Barrier(2)

        def persist(records):
            barrier.wait(timeout=10)
            try:
                DB.sql('SELECT ' + snapshot(key, records) + ';')
                return 'saved'
            except AssertionError as error:
                if '23514' in str(error) and 'ingestion_snapshot_conflict' in str(error):
                    return 'conflict'
                raise

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
                outcomes = list(workers.map(persist, versions))
            self.assertCountEqual(outcomes, ['saved', 'conflict'])
            # Both persisted rows must agree with the one winning run payload.
            self.assertEqual(DB.sql(f"""SELECT count(*)=2 AND bool_and(
                o.raw_payload=r.snapshot_payload->o.record_index->'raw_payload'
                AND o.estimate_seconds=(r.snapshot_payload->o.record_index->>'estimate_seconds')::integer)
                FROM source_observations o JOIN ingestion_runs r USING(run_id)
                WHERE r.run_key={quote(key)}"""), 't')
        finally:
            DB.sql(f"DELETE FROM source_observations WHERE run_id=(SELECT run_id FROM ingestion_runs WHERE run_key={quote(key)}); DELETE FROM ingestion_runs WHERE run_key={quote(key)};")

    def test_six_transport_modes_can_be_represented(self):
        for mode in ('walking', 'bus', 'metro', 'intercity_bus', 'train', 'high_speed_rail'):
            with self.subTest(mode=mode):
                self.case(f"INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES ({ctx('j1')},3,{quote(mode)});")

    def test_single_metro_journey_with_target_and_no_transfer(self):
        self.case("""
            DO $$ DECLARE j bigint; l bigint;
            BEGIN
                INSERT INTO journeys(journey_name,service_date,target_arrival_at)
                VALUES ('SYNTHETIC metro-only','2026-11-17','2026-11-17 09:00+08')
                RETURNING journey_id INTO j;
                INSERT INTO journey_legs(journey_id,leg_order,transport_mode,planned_departure_at,planned_arrival_at)
                VALUES (j,1,'metro','2026-11-17 08:30+08','2026-11-17 08:50+08') RETURNING leg_id INTO l;
                PERFORM pg_temp.assert_true((SELECT count(*)=1 FROM journey_legs WHERE journey_id=j), 'single-leg journey');
                PERFORM pg_temp.assert_true(NOT EXISTS (SELECT FROM transfer_constraints WHERE journey_id=j), 'no invented connection');
                INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot)
                VALUES (j,'metro-only-assessment','on_track','synthetic_arrival_deadline_fixture','test-v1','test-ref-v1','test-assumptions-v1',
                        '{"synthetic_test_only":true,"target_arrival_at":"2026-11-17T09:00:00+08:00"}');
                PERFORM pg_temp.assert_true((SELECT target_leg_id IS NULL AND target_transfer_id IS NULL FROM impact_assessments WHERE assessment_key='metro-only-assessment'), 'whole-journey target');
            END $$;
        """)

    def test_bus_walk_metro_has_explicit_walk_and_additional_buffer(self):
        self.case("""
            DO $$ DECLARE j bigint; b bigint; w bigint; m bigint;
            BEGIN
                INSERT INTO journeys(journey_name,service_date) VALUES ('SYNTHETIC bus-walk-metro','2026-11-17') RETURNING journey_id INTO j;
                INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES (j,1,'bus') RETURNING leg_id INTO b;
                INSERT INTO journey_legs(journey_id,leg_order,transport_mode,estimated_duration_seconds,duration_assumption_source)
                VALUES (j,2,'walking',480,'SYNTHETIC eight-minute walk; not measured') RETURNING leg_id INTO w;
                INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES (j,3,'metro') RETURNING leg_id INTO m;
                INSERT INTO transfer_constraints(journey_id,from_leg_id,to_leg_id,min_transfer_minutes,assumption_source)
                VALUES (j,b,w,0,'SYNTHETIC no extra allowance'), (j,w,m,2,'SYNTHETIC access buffer excluding walk');
                PERFORM pg_temp.assert_true((SELECT sum(estimated_duration_seconds)=480 FROM journey_legs WHERE journey_id=j), 'walk stored once');
                PERFORM pg_temp.assert_true((SELECT sum(min_transfer_minutes)=2 FROM transfer_constraints WHERE journey_id=j), 'additional minutes separate');
            END $$;
        """)

    def test_fixed_hsr_trip_and_boarding_deadline_are_static_references(self):
        self.case(f"""
            INSERT INTO journey_legs(journey_id,leg_order,transport_mode,planned_departure_at,boarding_deadline_at,boarding_deadline_source,reference_details)
            VALUES ({ctx('j1')},3,'high_speed_rail','2026-11-17 09:30+08','2026-11-17 09:25+08',
                    'SYNTHETIC cutoff for schema test; NOT an operator rule',
                    '{{"service_trip_id":"SYNTHETIC-HSR-0930","valid_from":"2026-11-17","valid_to":"2026-11-17","synthetic_test_only":true}}');
            SELECT pg_temp.assert_true((SELECT reference_details->>'service_trip_id'='SYNTHETIC-HSR-0930'
                AND boarding_deadline_at='2026-11-17 09:25+08'::timestamptz AND planned_arrival_at IS NULL
                FROM journey_legs WHERE journey_id={ctx('j1')} AND leg_order=3), 'trip and cutoff retained without invented arrival');
        """)

    def test_walking_duration_requires_positive_value_and_basis(self):
        base = f"INSERT INTO journey_legs(journey_id,leg_order,transport_mode,estimated_duration_seconds,duration_assumption_source) VALUES ({ctx('j1')},3,'walking',"
        for suffix in ("0,'synthetic')", "-1,'synthetic')", "480,NULL)", "480,'   ')", "NULL,'synthetic')"):
            with self.subTest(suffix=suffix):
                self.rejects(base + suffix, '23514')
        # An incomplete walking leg remains representable, explicitly unknown.
        self.case(base + f"""NULL,NULL);
            SELECT pg_temp.assert_true((SELECT estimated_duration_seconds IS NULL
                AND duration_assumption_source IS NULL FROM journey_legs
                WHERE journey_id={ctx('j1')} AND leg_order=3), 'unknown duration is not defaulted to zero');
        """)

    def test_boarding_deadline_basis_time_order_and_walking_restriction(self):
        base = f"INSERT INTO journey_legs(journey_id,leg_order,transport_mode,planned_departure_at,boarding_deadline_at,boarding_deadline_source) VALUES ({ctx('j1')},3,"
        for suffix in (
            "'train','2026-11-17 09:30+08','2026-11-17 09:31+08','synthetic')",
            "'train','2026-11-17 09:30+08','2026-11-17 09:25+08',NULL)",
            "'train','2026-11-17 09:30+08','2026-11-17 09:25+08','   ')",
            "'train','2026-11-17 09:30+08',NULL,'synthetic')",
            "'walking','2026-11-17 09:30+08','2026-11-17 09:25+08','synthetic')",
        ):
            with self.subTest(suffix=suffix):
                self.rejects(base + suffix, '23514')
        self.case(base + "'train','2026-11-17 09:30+08','2026-11-17 09:30+08','SYNTHETIC equality example');")
        self.case(base + "'train',NULL,'2026-11-17 09:25+08','SYNTHETIC incomplete plan');")

    def test_assessment_scope_cannot_reference_another_journey(self):
        transfers = f"""
            INSERT INTO journey_legs(journey_id,leg_order,transport_mode) VALUES ({ctx('j2')},2,'metro');
            INSERT INTO transfer_constraints(journey_id,from_leg_id,to_leg_id,min_transfer_minutes,assumption_source)
            VALUES ({ctx('j1')},{ctx('l1')},{ctx('l2')},2,'synthetic'),
                   ({ctx('j2')},{ctx('other_leg')},(SELECT leg_id FROM journey_legs WHERE journey_id={ctx('j2')} AND leg_order=2),2,'synthetic');
        """
        t1 = f"(SELECT transfer_id FROM transfer_constraints WHERE journey_id={ctx('j1')})"
        t2 = f"(SELECT transfer_id FROM transfer_constraints WHERE journey_id={ctx('j2')})"

        def assessment(key, leg, transfer):
            return f"""INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot,target_leg_id,target_transfer_id)
            VALUES ({ctx('j1')},{quote(key)},'insufficient_evidence','synthetic','test-v1','test-ref-v1','test-assumptions-v1','{{}}',{leg},{transfer})"""

        self.case(transfers + assessment('leg-target', ctx('l1'), 'NULL') + ';'
                  + assessment('transfer-target', 'NULL', t1) + ';'
                  + "SELECT pg_temp.expect_error(" + quote(assessment('wrong-leg', ctx('other_leg'), 'NULL')) + ", '23503');"
                  + "SELECT pg_temp.expect_error(" + quote(assessment('wrong-transfer', 'NULL', t2)) + ", '23503');"
                  + "SELECT pg_temp.expect_error(" + quote(assessment('two-targets', ctx('l1'), t1)) + ", '23514');")

    def test_general_assessment_statuses_and_rejection_of_legacy_writes(self):
        for status in ('on_track','at_risk','missed_deadline','insufficient_evidence',
                       'connection_feasible','connection_at_risk','guaranteed'):
            statement = f"""INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot)
                VALUES ({ctx('j1')},'status-fixture',{quote(status)},'synthetic_only','test-v1','test-ref-v1','test-assumptions-v1','{{"synthetic_test_only":true}}')"""
            with self.subTest(status=status):
                if status in ('connection_feasible','connection_at_risk','guaranteed'):
                    self.rejects(statement, '23514')
                else:
                    self.case(statement + ';')

    def test_upgrade_preserves_existing_assessment_history_and_inputs(self):
        # Separate database inside this runner's owned disposable server only.
        database = 'planaware_upgrade_test'
        DB.sql('CREATE DATABASE ' + database, database='postgres')
        try:
            for name in ('001_schema.sql', '002_snapshot_write.sql'):
                DB.sql((ROOT / 'sql/migrations' / name).read_text(), database=database)
            DB.sql('SELECT ' + snapshot('legacy-evidence') + ';', database=database)
            DB.sql("""
                INSERT INTO journeys(journey_name,service_date) VALUES ('legacy-fixture','2026-11-17');
                INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot,evidence_observation_id,evaluated_at)
                SELECT journey_id, 'legacy-feasible', 'connection_feasible', 'old-reason', 'old-model', 'old-reference', 'old-assumptions', '{"original_input":true}', (SELECT observation_id FROM source_observations), '2026-11-17 14:00+08' FROM journeys;
                INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot,evidence_observation_id,evaluated_at)
                SELECT journey_id, 'legacy-risk', 'connection_at_risk', 'old-reason', 'old-model', 'old-reference', 'old-assumptions', '{"original_input":true}', (SELECT observation_id FROM source_observations), '2026-11-17 14:01+08' FROM journeys;
                INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,model_version,reference_data_version,assumption_version,input_snapshot,evaluated_at)
                SELECT journey_id, 'legacy-insufficient', 'insufficient_evidence', 'missing-source', 'old-model', 'old-reference', 'old-assumptions', '{"original_input":true}', '2026-11-17 14:02+08' FROM journeys;
            """, database=database)
            assessment_query = 'SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a'
            before = json.loads(DB.sql(assessment_query, database=database))
            evidence_query = 'SELECT jsonb_agg(to_jsonb(o) ORDER BY observation_id) FROM source_observations o'
            evidence_before = json.loads(DB.sql(evidence_query, database=database))
            DB.sql((ROOT / 'sql/migrations/003_journey_timing_scenarios.sql').read_text(), database=database)
            after = json.loads(DB.sql(assessment_query, database=database))
            expected = []
            labels = {'connection_feasible': 'on_track', 'connection_at_risk': 'at_risk'}
            for original in before:
                updated = dict(original)
                updated['legacy_status'] = original['status'] if original['status'] in labels else None
                updated['status'] = labels.get(original['status'], original['status'])
                updated.update(target_leg_id=None, target_transfer_id=None)
                expected.append(updated)
            # Exact comparison covers IDs, keys, reason, versions, input payload,
            # non-null evidence FK and each individual evaluation timestamp.
            self.assertEqual(after, expected)
            self.assertEqual(json.loads(DB.sql(evidence_query, database=database)), evidence_before)
        finally:
            DB.sql('DROP DATABASE ' + database, database='postgres')


if __name__ == "__main__":
    unittest.main(verbosity=2)
