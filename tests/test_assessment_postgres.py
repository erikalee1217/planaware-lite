"""Real PostgreSQL via the existing disposable setup; no API requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

import test_postgres_schema as pg
from test_assessment import NOW, synthetic_context, synthetic_spec, synthetic_observation, source_spec
from planaware.assessment import (AssessmentError, assess_saved_transfer, build_assessment,
    load_transfer_context, persist_assessment, demonstrate)


setUpModule = pg.setUpModule


def create_journey(route='SYNTHETIC_ROUTE', stop='SYNTHETIC_STOP'):
    context = synthetic_context(route, stop)
    journey_id = int(pg.DB.sql("INSERT INTO journeys(journey_name,service_date) VALUES ('SYNTHETIC integration case','2026-11-18') RETURNING journey_id"))
    ids = []
    for leg in context['legs']:
        leg = dict(leg, journey_id=journey_id)
        ids.append(int(pg.DB.sql("INSERT INTO journey_legs(journey_id,leg_order,transport_mode,route_uid,direction,origin_stop_uid,destination_stop_uid,planned_departure_at,planned_arrival_at,reference_details) SELECT journey_id,leg_order,transport_mode,route_uid,direction,origin_stop_uid,destination_stop_uid,planned_departure_at,planned_arrival_at,reference_details FROM jsonb_populate_record(NULL::journey_legs,"
            + pg.quote(json.dumps(leg)) + '::jsonb) RETURNING leg_id')))
    transfer_id = int(pg.DB.sql(f"INSERT INTO transfer_constraints(journey_id,from_leg_id,to_leg_id,min_transfer_minutes,assumption_source) VALUES ({journey_id},{ids[0]},{ids[1]},5,'SYNTHETIC extra boarding only; walking excluded') RETURNING transfer_id"))
    return journey_id, transfer_id, ids


class AssessmentPostgresTests(unittest.TestCase):
    def setUp(self):
        self.journeys = []
        self.runs = []
        self.jid, self.tid, self.legs = self.journey()

    def journey(self, *args):
        result = create_journey(*args)
        self.journeys.append(result[0])
        return result

    def tearDown(self):
        for jid in self.journeys:
            pg.DB.sql(f'DELETE FROM impact_assessments WHERE journey_id={jid}; DELETE FROM transfer_constraints WHERE journey_id={jid}; DELETE FROM journey_legs WHERE journey_id={jid}; DELETE FROM journeys WHERE journey_id={jid};')
        for run in self.runs:
            pg.DB.sql(f'DELETE FROM source_observations WHERE run_id={run}; DELETE FROM ingestion_runs WHERE run_id={run};')

    def spec(self, **changes):
        values = dict(journey_id=self.jid, transfer_id=self.tid)
        values.update(changes)
        return synthetic_spec(**values)

    def row(self, identity):
        return json.loads(pg.DB.sql(f'SELECT row_to_json(a) FROM impact_assessments a WHERE assessment_id={identity}'))

    def test_three_synthetic_statuses_and_deadline_missed_are_persisted(self):
        for changes, status in [({}, 'on_track'),
                ({'latest_arrival': NOW + timedelta(minutes=25)}, 'at_risk'),
                ({'earliest_arrival': NOW + timedelta(minutes=26), 'latest_arrival': NOW + timedelta(minutes=27)}, 'at_risk'),
                ({'earliest_arrival': None, 'latest_arrival': None}, 'insufficient_evidence')]:
            with self.subTest(status=status):
                result = assess_saved_transfer(self.spec(**changes), pg.DB.sql)
                row = self.row(result['assessment_id'])
                self.assertEqual(row['status'], status)
                self.assertEqual(row['target_transfer_id'], self.tid)
                self.assertIsNone(row['legacy_status'])
                self.assertIn(row['input_snapshot']['result']['status'], ('connection_feasible', 'connection_at_risk', 'insufficient_evidence'))

    def test_identical_retry_and_conflict_preserve_complete_row(self):
        spec = self.spec()
        record = build_assessment(spec, load_transfer_context(spec, pg.DB.sql))
        identity = persist_assessment(record, pg.DB.sql)
        before = self.row(identity)
        self.assertEqual(persist_assessment(record, pg.DB.sql), identity)
        changed = deepcopy(record)
        changed['input_snapshot']['result']['minimum_buffer'] = 1
        with self.assertRaises(AssessmentError):
            persist_assessment(changed, pg.DB.sql)
        self.assertEqual(self.row(identity), before)
        self.assertEqual(pg.DB.sql(f'SELECT count(*) FROM impact_assessments WHERE journey_id={self.jid}'), '1')

    def test_concurrent_identical_and_conflicting_retries(self):
        spec = self.spec()
        record = build_assessment(spec, load_transfer_context(spec, pg.DB.sql))
        with ThreadPoolExecutor(max_workers=2) as pool:
            identities = list(pool.map(lambda _: persist_assessment(record, pg.DB.sql), range(2)))
        self.assertEqual(identities[0], identities[1])
        original = self.row(identities[0])
        changed = deepcopy(record)
        changed['status'] = 'at_risk'
        with ThreadPoolExecutor(max_workers=2) as pool:
            good = pool.submit(persist_assessment, record, pg.DB.sql)
            bad = pool.submit(persist_assessment, changed, pg.DB.sql)
            self.assertEqual(good.result(), identities[0])
            with self.assertRaises(AssessmentError):
                bad.result()
        self.assertEqual(self.row(identities[0]), original)

    def test_new_evaluation_time_and_two_different_configured_journeys(self):
        first = assess_saved_transfer(self.spec(), pg.DB.sql)
        next_time = assess_saved_transfer(self.spec(evaluated_at=NOW + timedelta(seconds=1)), pg.DB.sql)
        jid, tid, _ = self.journey('ANOTHER_SYNTHETIC_ROUTE', 'ANOTHER_SYNTHETIC_STOP')
        other = assess_saved_transfer(synthetic_spec(journey_id=jid, transfer_id=tid), pg.DB.sql)
        self.assertEqual(len({first['assessment_id'], next_time['assessment_id'], other['assessment_id']}), 3)
        self.assertEqual(other['status'], 'on_track')
        with self.assertRaises(AssessmentError):
            assess_saved_transfer(self.spec(transfer_id=tid), pg.DB.sql)

    def test_source_observations_and_new_evidence_preserve_history(self):
        identities = []
        for index in (1, 2):
            row = synthetic_observation(index)
            normalized = {'route_uid': row['route_uid'], 'direction': row['direction'],
                'stop_uid': row['stop_uid'], 'stop_status': row['stop_status'],
                'estimate_seconds': row['estimate_seconds'], 'source_updated_at': row['source_updated_at'],
                'raw_payload': row['raw_payload']}
            run = int(pg.DB.sql('SELECT ' + pg.snapshot(key=f'assessment-source-{self.jid}-{index}', records=[normalized],
                requested=(NOW - timedelta(seconds=15)).isoformat(), fetched=row['fetched_at'])))
            self.runs.append(run)
            observation_id = int(pg.DB.sql(f'SELECT observation_id FROM source_observations WHERE run_id={run}'))
            spec = source_spec(journey_id=self.jid, transfer_id=self.tid,
                evidence_leg_id=self.legs[0], observation_ids=(observation_id,),
                earliest_arrival=None, latest_arrival=None)
            result = assess_saved_transfer(spec, pg.DB.sql)
            self.assertEqual(result['reason_code'], 'missing_arrival_interval')
            self.assertEqual(self.row(result['assessment_id'])['evidence_observation_id'], observation_id)
            identities.append(result['assessment_id'])
        self.assertNotEqual(*identities)

    def test_sql_literals_in_snapshot_cannot_escape_transaction(self):
        pg.DB.sql(f"UPDATE journey_legs SET reference_details=reference_details || '{{\"precise\":0.12345678901234567890123456789}}'::jsonb WHERE leg_id={self.legs[0]}")
        spec = self.spec(interval_source="SYNTHETIC quote' \\ $assessment$; DROP TABLE journeys; --")
        result = assess_saved_transfer(spec, pg.DB.sql)
        self.assertEqual(self.row(result['assessment_id'])['input_snapshot']['request']['interval_source'], spec.interval_source)
        self.assertEqual(pg.DB.sql("SELECT (input_snapshot#>>'{context,legs,0,reference_details,precise}')::numeric=0.12345678901234567890123456789 FROM impact_assessments WHERE assessment_id=" + str(result['assessment_id'])), 't')

    def test_demo_on_synthetic_seed_and_saved_status_three_fixture(self):
        # The fixture is explicitly synthetic. The separate manual demo uses the
        # saved real artifact, never an API request.
        from planaware.n1 import N1Scope, freeze_n1_fixture
        pg.DB.sql((pg.ROOT / 'sql/seeds/001_synthetic_journey.sql').read_text())
        jid = int(pg.DB.sql('SELECT max(journey_id) FROM journeys'))
        self.journeys.append(jid)
        tid = int(pg.DB.sql(f'SELECT max(transfer_id) FROM transfer_constraints WHERE journey_id={jid}'))
        fixture = json.loads((pg.ROOT / 'tests/fixtures/n1_298_observed.json').read_text())
        for row in fixture:
            row['StopStatus'] = 3
            row.pop('EstimateTime', None)
            row['synthetic_test_only'] = True
        saved = freeze_n1_fixture(json.dumps(fixture), scope=N1Scope.from_json((pg.ROOT / 'tests/fixtures/n1_298_scope.json').read_text()),
            requested_at='2026-11-17T19:42:00+08:00', fetched_at='2026-11-17T19:42:06+08:00')
        (pg.ROOT / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pg.ROOT / 'work') as directory:
            artifact = Path(directory) / 'synthetic-status-three.json'
            artifact.write_text(saved.to_json())
            results = demonstrate(jid, tid, artifact, pg.DB.sql)
            run = int(pg.DB.sql('SELECT run_id FROM ingestion_runs WHERE run_key=' + pg.quote(saved.run_key)))
            self.runs.append(run)
            self.assertEqual([result['status'] for result in results], ['on_track', 'at_risk', 'insufficient_evidence'])
            self.assertEqual(results[-1]['reason_code'], 'evidence_unusable')
            again = demonstrate(jid, tid, artifact, pg.DB.sql)
            self.assertEqual(results, again)
            # Presentation reads actual persisted rows and must leave all saved
            # decisions, clocks and evidence unchanged.
            from planaware.brief import build_brief, format_brief, load_assessment
            before = pg.DB.sql(f'SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a WHERE journey_id={jid}')
            rows = [load_assessment(result['assessment_id'], pg.DB.sql) for result in results]
            briefs = [build_brief(row) for row in rows]
            self.assertEqual([brief['status'] for brief in briefs], ['on_track', 'at_risk', 'insufficient_evidence'])
            self.assertEqual([brief['minimum_buffer'] for brief in briefs], ['+1 min', '-1 min', None])
            self.assertIn('15:23:00', briefs[0]['arrival_interval']['earliest'])
            self.assertIn('15:25:00', briefs[0]['boarding_deadline'])
            self.assertIsNone(briefs[2]['boarding_deadline'])
            self.assertIn('Synthetic N1 observation fixture', format_brief(rows[2]))
            self.assertEqual(before, pg.DB.sql(f'SELECT jsonb_agg(to_jsonb(a) ORDER BY assessment_id) FROM impact_assessments a WHERE journey_id={jid}'))
