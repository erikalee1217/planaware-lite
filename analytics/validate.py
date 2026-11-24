"""Offline demo/validation using a disposable PostgreSQL database and local dbt."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# Direct execution uses the checkout even when only analytics dependencies are installed.
sys.path.insert(0, str(ROOT))
from planaware.n1 import N1Scope, freeze_n1_fixture
from planaware.assessment import demonstrate
from planaware.live import replay
from planaware.brief import build_brief, format_brief


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline PostgreSQL + dbt validation; no API requests')
    parser.add_argument('--demo', action='store_true', help='Show assessments/briefs using the committed historical N1 sample')
    args = parser.parse_args(argv)
    docker = shutil.which('docker')
    dbt = Path(sys.executable).parent / 'dbt'
    if not docker or not dbt.is_file():
        raise RuntimeError('Run with the dbt virtualenv Python and an available Docker CLI')
    owner, password = uuid4().hex, uuid4().hex
    run_directory = ROOT / 'work' / (('demo-' if args.demo else 'dbt-validation-') + owner)
    run_directory.mkdir(parents=True)
    image = os.environ.get('PG_TEST_IMAGE', 'postgres:14')
    env = {key: value for key, value in os.environ.items() if not key.startswith(('TDX_', 'PG', 'DBT_'))}
    env.update(POSTGRES_PASSWORD=password, PGPASSWORD=password,
               DBT_SEND_ANONYMOUS_USAGE_STATS='false', PYTHONDONTWRITEBYTECODE='1',
               DBT_TARGET_PATH=str(run_directory / 'dbt-target'),
               DBT_LOG_PATH=str(run_directory / 'dbt-logs'))
    container = None

    def command(args, *, sql=None, timeout=45):
        return subprocess.run(args, input=sql, env=env, cwd=ROOT,
            capture_output=True, text=True, timeout=timeout)

    def execute(sql):
        result = command([docker, 'exec', '-i', '-e', 'PGPASSWORD', container,
            'psql', '-X', '-w', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1',
            '-U', 'planaware_analytics_test', '-d', 'planaware_analytics_test'], sql=sql)
        if result.returncode:
            raise RuntimeError('Disposable database SQL failed')
        return result.stdout.strip()

    def build(*args):
        result = command([str(dbt), *args, '--project-dir', str(ROOT / 'analytics'),
            '--profiles-dir', str(ROOT / 'analytics')], timeout=180)
        print(result.stdout, end='')
        if result.returncode and result.stderr:
            print(result.stderr, file=sys.stderr)
        return result

    try:
        if command([docker, 'image', 'inspect', image]).returncode:
            raise RuntimeError('Required local PostgreSQL image missing; no image pulled')
        run = command([docker, 'run', '--pull=never', '-d', '--name', 'planaware-dbt-' + owner,
            '--label', 'planaware.analytics-test=' + owner, '--publish', '127.0.0.1::5432',
            '--tmpfs', '/var/lib/postgresql/data:rw,nosuid', '-e', 'POSTGRES_PASSWORD',
            '-e', 'POSTGRES_USER=planaware_analytics_test', '-e', 'POSTGRES_DB=planaware_analytics_test', image])
        if run.returncode:
            raise RuntimeError('Could not start disposable analytics database')
        container = run.stdout.strip()
        ready = False
        for _ in range(60):
            if not command([docker, 'exec', container, 'pg_isready', '-U', 'planaware_analytics_test']).returncode:
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            raise RuntimeError('Disposable database startup timed out')
        port = command([docker, 'port', container, '5432/tcp']).stdout.strip()
        if not port.startswith('127.0.0.1:'):
            raise RuntimeError('Refusing a non-loopback test database')
        env.update(DBT_HOST='127.0.0.1', DBT_PORT=port.rsplit(':', 1)[1],
            DBT_USER='planaware_analytics_test', DBT_DATABASE='planaware_analytics_test',
            DBT_ENV_SECRET_PASSWORD=password, DBT_SCHEMA='planaware_analytics', DBT_SOURCE_SCHEMA='public')
        for migration in sorted((ROOT / 'sql/migrations').glob('*.sql')):
            execute(migration.read_text())
        execute((ROOT / 'sql/seeds/001_synthetic_journey.sql').read_text())
        scope = N1Scope.from_json((ROOT / 'tests/fixtures/n1_298_scope.json').read_text())
        template = json.loads((ROOT / 'tests/fixtures/n1_298_observed.json').read_text())[1]
        base = dict(template, StopStatus=0, EstimateTime=120,
            SrcUpdateTime='2026-11-17T09:00:00+08:00', UpdateTime='2026-11-17T09:00:02+08:00',
            synthetic_test_only=True)
        cases = [base, base, dict(base, EstimateTime=None), dict(base, StopStatus=3, EstimateTime=None),
            dict(base, SrcUpdateTime=None), dict(base, SrcUpdateTime='2026-11-17T08:00:00+08:00'),
            dict(base, StopStatus=99), dict(base, EstimateTime=-1),
            dict(base, SrcUpdateTime='2026-11-17T09:01:00+08:00')]
        for index, raw in enumerate(cases):
            frozen = freeze_n1_fixture(json.dumps([raw]), scope=scope, run_key=f'synthetic-dbt-{index}',
                requested_at='2026-11-17T09:00:03+08:00', fetched_at='2026-11-17T09:00:05+08:00')
            execute(frozen.to_sql())
        (ROOT / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=run_directory) as directory:
            raw = dict(base, StopStatus=3, EstimateTime=None)
            raw.pop('EstimateTime')
            # The demo preserves the committed historical records unchanged.
            # Only acquisition metadata is synthetic; no live request is made.
            payload = (ROOT / 'tests/fixtures/n1_298_observed.json').read_text() if args.demo else json.dumps([raw])
            frozen = freeze_n1_fixture(payload, scope=scope,
                run_key='offline-demo-observed-n1' if args.demo else 'synthetic-dbt-demo',
                requested_at='2026-11-17T19:42:00+08:00' if args.demo else '2026-11-17T09:00:03+08:00',
                fetched_at='2026-11-17T19:42:06+08:00' if args.demo else '2026-11-17T09:00:05+08:00')
            artifact = Path(directory) / 'offline-snapshot.json'
            artifact.write_text(frozen.to_json())
            jid = int(execute('SELECT max(journey_id) FROM journeys'))
            tid = int(execute(f'SELECT max(transfer_id) FROM transfer_constraints WHERE journey_id={jid}'))
            saved_run = replay(artifact, execute)
            results = demonstrate(jid, tid, artifact, execute)
            history_before = execute('SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a')
            assert replay(artifact, execute) == saved_run
            assert demonstrate(jid, tid, artifact, execute) == results
            assert history_before == execute('SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a')
            rows = json.loads(history_before)
            briefs = [build_brief(row) for row in rows]
            assert [brief['status'] for brief in briefs] == ['on_track', 'at_risk', 'insufficient_evidence']
            assert [brief['minimum_buffer'] for brief in briefs] == ['+1 min', '-1 min', None]
            assert briefs[2]['boarding_deadline'] is None
            if args.demo:
                assert 'Saved TDX N1 observation' in briefs[2]['evidence_source']
                assert 'Synthetic journey' in briefs[2]['assumptions']
                expected_text = '\n\n'.join(format_brief(row) for row in rows) + '\n'
                export = run_directory / 'assessments.json'
                export.write_text(history_before + '\n')
                # Exercise both public presentation CLI formats against saved rows.
                brief_command = [sys.executable, '-m', 'planaware.brief', '--input', str(export)]
                text_output = command(brief_command)
                json_output = command(brief_command + ['--format', 'json'])
                assert text_output.returncode == json_output.returncode == 0
                assert text_output.stdout == expected_text
                assert json.loads(json_output.stdout) == briefs
                (run_directory / 'briefs.txt').write_text(text_output.stdout)
                (run_directory / 'briefs.json').write_text(json_output.stdout)
                print(text_output.stdout)
            assert history_before == execute('SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a')
        # Legacy/incomplete JSON remains unknown, never fabricated as zero timing.
        execute(f"""INSERT INTO impact_assessments(journey_id,assessment_key,status,reason_code,
            model_version,reference_data_version,assumption_version,input_snapshot,evaluated_at)
            VALUES ({jid},'synthetic-legacy','insufficient_evidence','synthetic_legacy',
            'legacy-model','synthetic-ref','synthetic-assumptions','{{}}','2026-11-17T14:30:00+08:00');""")
        primary_keys = {'source_observations': 'observation_id', 'ingestion_runs': 'run_id',
                        'impact_assessments': 'assessment_id'}
        def source_rows(table):
            return execute(f'SELECT jsonb_agg(to_jsonb(t) ORDER BY {primary_keys[table]}) FROM {table} t')
        before = {table: source_rows(table) for table in primary_keys}
        if build('build').returncode:
            raise RuntimeError('dbt build failed')
        quality = json.loads(execute("SELECT json_object_agg(quality_category,n) FROM (SELECT quality_category,count(*) n FROM planaware_analytics.int_n1_observation_quality GROUP BY 1) q"))
        expected_observations = 11 if args.demo else 10
        assert quality == {'normal_eta_candidate': 2, 'missing_eta': 1, 'non_normal_status': 3 if args.demo else 2,
            'missing_source_time': 1, 'stale_at_retrieval': 1, 'unsupported_status': 1,
            'invalid_raw_eta': 1, 'source_after_retrieval': 1}, quality
        assert int(execute('SELECT count(*) FROM planaware_analytics.int_n1_observation_quality')) == expected_observations
        assert execute('SELECT count(*) FROM planaware_analytics.mart_transfer_assessments') == '4'
        assert execute("SELECT minimum_buffer_microseconds=60000000 AND minimum_buffer_seconds=60 FROM planaware_analytics.mart_transfer_assessments WHERE assessment_status='on_track'") == 't'
        assert execute("SELECT minimum_buffer_seconds=-60 FROM planaware_analytics.mart_transfer_assessments WHERE assessment_status='at_risk'") == 't'
        assert execute("SELECT bool_and(boarding_deadline IS NULL AND minimum_buffer_microseconds IS NULL) FROM planaware_analytics.mart_transfer_assessments WHERE assessment_status='insufficient_evidence'") == 't'
        assert all(source_rows(table) == saved for table, saved in before.items())
        # Prove the timing contract test catches a falsely feasible stored record.
        if not args.demo:
            execute("UPDATE impact_assessments SET input_snapshot=jsonb_set(jsonb_set(input_snapshot,'{result,boarding_deadline}','null'),'{result,minimum_buffer}','null') WHERE status='on_track'")
            negative = build('test', '--select', 'mart_assessment_contract')
            negative_results = json.loads((run_directory / 'dbt-target/run_results.json').read_text())
            assert negative.returncode != 0 and any(row['status'] == 'fail' for row in negative_results['results'])
            # Restore only that exact fixture, then leave final build results passing.
            good = next(row for row in json.loads(before['impact_assessments']) if row['status'] == 'on_track')
            payload = json.dumps(good['input_snapshot']).replace("'", "''")
            execute(f"UPDATE impact_assessments SET input_snapshot='{payload}'::jsonb WHERE assessment_id={good['assessment_id']}")
            if build('build').returncode:
                raise RuntimeError('Final dbt build failed')
        assert all(source_rows(table) == saved for table, saved in before.items())
        result = json.loads((run_directory / 'dbt-target/run_results.json').read_text())
        report = {'postgres_version': execute('SHOW server_version'),
            'dbt_core_version': result['metadata']['dbt_version'], 'results':
                [{'unique_id': row['unique_id'], 'status': row['status']} for row in result['results']],
            'observation_rows': expected_observations, 'assessment_rows': 4, 'quality_categories': quality,
            'snapshot_replay': saved_run, 'assessment_results': results,
            'fixtures': 'historical N1 sample + synthetic timings and quality cases' if args.demo else 'synthetic only',
            'freshness_policy': '120 seconds illustrative, not a TDX guarantee', 'tdx_requests': 0}
    finally:
        if container:
            label = command([docker, 'inspect', '--format', '{{index .Config.Labels "planaware.analytics-test"}}', container])
            if label.returncode or label.stdout.strip() != owner:
                raise RuntimeError('Refusing cleanup without matching ownership label')
            if command([docker, 'rm', '--force', container]).returncode:
                raise RuntimeError('Owned analytics container cleanup failed')
    (run_directory / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    print('Saved local results:', run_directory.relative_to(ROOT))


if __name__ == '__main__':
    main()
