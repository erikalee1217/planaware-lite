"""Single-transfer integration, explicit assumptions and transactional history."""
import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Literal
from zoneinfo import ZoneInfo

from .n1 import _json_text, _read_json, _timestamp
from .risk import TransferInput, evaluate_transfer

STATUS_MAP = {'connection_feasible': 'on_track', 'connection_at_risk': 'at_risk',
              'insufficient_evidence': 'insufficient_evidence'}
MODEL_VERSION = 'single-transfer-integration-v1'


class AssessmentError(ValueError):
    pass


@dataclass(frozen=True)
class AssessmentSpec:
    journey_id: int
    transfer_id: int
    evaluated_at: datetime
    reference_version: str
    assumption_version: str
    scenario: Literal['synthetic', 'source_backed']
    earliest_arrival: datetime | None = None
    latest_arrival: datetime | None = None
    interval_basis: Literal['synthetic', 'validated', 'unknown'] = 'unknown'
    interval_source: str | None = None
    buffer_semantics: Literal['boarding_only', 'unknown'] = 'unknown'
    observation_ids: tuple[int, ...] = ()
    evidence_leg_id: int | None = None
    evidence_stop_uid: str | None = None
    freshness_seconds: int = 120
    service_timezone: str = 'Asia/Taipei'


def _time(value):
    if value is None:
        return None
    value = _timestamp(value, 'assessment timestamp') if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AssessmentError('Expected an offset-aware datetime')
    return value.astimezone(timezone.utc)


def _micros(value):
    return None if value is None else (value.days * 86400 + value.seconds) * 1000000 + value.microseconds


def _canonical(value):
    if isinstance(value, datetime):
        return _time(value).isoformat(timespec='microseconds')
    if isinstance(value, timedelta):
        return _micros(value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def canonical_json(value):
    return _json_text(_canonical(value))


def _literal(value):
    return "E'" + value.replace('\\', '\\\\').replace("'", "''") + "'"


def _validate_spec(spec):
    if not isinstance(spec, AssessmentSpec):
        raise AssessmentError('Expected AssessmentSpec')
    ids = [spec.journey_id, spec.transfer_id, *spec.observation_ids]
    if spec.evidence_leg_id is not None:
        ids.append(spec.evidence_leg_id)
    if any(type(value) is not int or value <= 0 for value in ids):
        raise AssessmentError('Identities must be positive integers')
    if not isinstance(spec.observation_ids, tuple) or len(set(spec.observation_ids)) != len(spec.observation_ids):
        raise AssessmentError('Observation identities must be a unique tuple')
    if any(not isinstance(value, str) or not value.strip() for value in (spec.reference_version, spec.assumption_version)):
        raise AssessmentError('Explicit reference and assumption versions are required')
    if spec.scenario not in ('synthetic', 'source_backed') or spec.interval_basis not in ('synthetic', 'validated', 'unknown') or spec.buffer_semantics not in ('boarding_only', 'unknown'):
        raise AssessmentError('Unknown scenario or timing semantics')
    if type(spec.freshness_seconds) is not int or spec.freshness_seconds <= 0:
        raise AssessmentError('Freshness threshold must be positive integer seconds')
    if spec.evaluated_at is None:
        raise AssessmentError('Fixed evaluation time is required')
    _time(spec.evaluated_at)
    ZoneInfo(spec.service_timezone)


def load_transfer_context(spec, execute_sql):
    _validate_spec(spec)
    ids = ','.join(str(value) for value in spec.observation_ids) or 'NULL'
    statement = f"""SELECT json_build_object(
        'journey',(SELECT row_to_json(j) FROM journeys j WHERE journey_id={spec.journey_id}),
        'transfer',(SELECT row_to_json(t) FROM transfer_constraints t
            WHERE transfer_id={spec.transfer_id} AND journey_id={spec.journey_id}),
        'legs',COALESCE((SELECT json_agg(l ORDER BY leg_order) FROM journey_legs l
            WHERE journey_id={spec.journey_id}),'[]'::json),
        'observations',COALESCE((SELECT json_agg(o ORDER BY observation_id) FROM source_observations o
            WHERE observation_id IN ({ids})),'[]'::json));"""
    return _read_json(execute_sql(statement))


def _evidence(spec, context):
    observations = context['observations']
    if spec.scenario == 'synthetic':
        if observations or spec.observation_ids:
            raise AssessmentError('Synthetic scenarios must not masquerade as dynamic evidence')
        return 'usable', None, []
    if {row['observation_id'] for row in observations} != set(spec.observation_ids):
        raise AssessmentError('Requested observation does not exist')
    if not observations:
        return 'missing', None, ['missing_observation']
    legs = {leg['leg_id']: leg for leg in context['legs']}
    leg = legs.get(spec.evidence_leg_id)
    issues = []
    if not leg or leg['transport_mode'] != 'bus' or spec.evidence_stop_uid not in (
        leg.get('origin_stop_uid'), leg.get('destination_stop_uid')) or not spec.evidence_stop_uid:
        issues.append('invalid_evidence_leg_or_stop')
    selected = max(observations, key=lambda row: (_time(row['fetched_at']), row['observation_id']))
    for row in observations:
        if row['source_system'] != 'tdx_bus_n1' or not leg or (
            row['route_uid'], row['direction'], row['stop_uid']) != (
                leg.get('route_uid'), leg.get('direction'), spec.evidence_stop_uid):
            issues.append('evidence_scope_mismatch')
        raw = row['raw_payload']
        if type(raw.get('Direction')) is not int or type(raw.get('StopStatus')) is not int or (
            raw.get('EstimateTime') is not None and type(raw['EstimateTime']) is not int):
            issues.append('invalid_raw_codes')
        if (raw.get('RouteUID'), raw.get('Direction'), raw.get('StopUID'), raw.get('StopStatus'),
            raw.get('EstimateTime')) != (row['route_uid'], row['direction'], row['stop_uid'],
                                        row['stop_status'], row['estimate_seconds']):
            issues.append('raw_normalized_mismatch')
        try:
            if _time(raw.get('SrcUpdateTime')) != _time(row['source_updated_at']):
                issues.append('raw_source_time_mismatch')
        except (ValueError, TypeError):
            issues.append('invalid_raw_source_time')
    source = _time(selected['source_updated_at'])
    fetched, evaluated = _time(selected['fetched_at']), _time(spec.evaluated_at)
    if source is None:
        issues.append('missing_source_time')
    else:
        if source > evaluated or fetched > evaluated or source > fetched:
            issues.append('future_or_inconsistent_clock')
        known = [_time(row['source_updated_at']) for row in observations if row['source_updated_at']]
        if known and source < max(known):
            issues.append('delayed_observation')
        same_clock = [row for row in observations if row['source_updated_at'] and _time(row['source_updated_at']) == source]
        if len({(row['estimate_seconds'], row['stop_status']) for row in same_clock}) > 1:
            issues.append('conflicting_observations')
    if type(selected['stop_status']) is not int or selected['stop_status'] != 0:
        issues.append('non_normal_stop_status')
    if type(selected['estimate_seconds']) is not int or selected['estimate_seconds'] < 0:
        issues.append('missing_or_invalid_estimate')
    if issues:
        return 'unusable', selected['observation_id'], sorted(set(issues))
    if evaluated - source > timedelta(seconds=spec.freshness_seconds):
        return 'stale', selected['observation_id'], ['stale_source_time']
    return 'usable', selected['observation_id'], []


def build_assessment(spec, context):
    _validate_spec(spec)
    journey, transfer = context['journey'], context['transfer']
    if not journey or not transfer or journey['journey_id'] != spec.journey_id or transfer['journey_id'] != spec.journey_id or transfer['transfer_id'] != spec.transfer_id:
        raise AssessmentError('Transfer must exist in the specified journey')
    legs = sorted(context['legs'], key=lambda row: row['leg_order'])
    leg_ids = [row['leg_id'] for row in legs]
    if transfer['from_leg_id'] not in leg_ids or transfer['to_leg_id'] not in leg_ids:
        raise AssessmentError('Transfer legs must belong to the journey')
    index = leg_ids.index(transfer['from_leg_id'])
    if index + 1 >= len(legs) or legs[index + 1]['leg_id'] != transfer['to_leg_id']:
        raise AssessmentError('Only forward adjacent legs are supported')
    origin, target = legs[index:index + 2]
    quality, evidence_id, issues = _evidence(spec, context)
    if spec.scenario == 'source_backed' and spec.evidence_leg_id in leg_ids and leg_ids.index(spec.evidence_leg_id) > index:
        issues.append('evidence_leg_after_transfer_origin')
        quality = 'unusable'
    if target['transport_mode'] == 'walking':
        issues.append('target_is_not_boarding_leg')
        quality = 'unusable'
    dates = [date.fromisoformat(journey['service_date']),
             _time(spec.evaluated_at).astimezone(ZoneInfo(spec.service_timezone)).date()]
    for leg in (origin, target):
        reference = leg['reference_details']
        try:
            valid_from, valid_to = date.fromisoformat(reference['valid_from']), date.fromisoformat(reference['valid_to'])
            valid = valid_from <= min(dates) <= max(dates) <= valid_to
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid or reference.get('reference_data_version') != spec.reference_version or reference.get('assumption_version') != spec.assumption_version:
            issues.append('missing_invalid_or_expired_reference')
            quality = 'unusable'
    if not (transfer.get('assumption_source') or '').strip():
        issues.append('missing_transfer_provenance')
        quality = 'unusable'
    if origin['transport_mode'] == 'walking' and (
        not origin.get('estimated_duration_seconds') or not (origin.get('duration_assumption_source') or '').strip()):
        issues.append('unknown_walking_duration')
        quality = 'unusable'
    has_interval = spec.earliest_arrival is not None or spec.latest_arrival is not None
    expected_basis = 'synthetic' if spec.scenario == 'synthetic' else 'validated'
    if has_interval and (spec.interval_basis != expected_basis or not isinstance(spec.interval_source, str) or not spec.interval_source.strip()):
        issues.append('unvalidated_arrival_interval')
        quality = 'unusable'
    departure = _time(target['planned_departure_at'])
    buffer = None
    if spec.buffer_semantics == 'boarding_only':
        buffer = timedelta(minutes=transfer['min_transfer_minutes'])
    else:
        issues.append('unknown_buffer_semantics')
    cutoff = _time(target.get('boarding_deadline_at'))
    if cutoff is not None:
        if departure is None or buffer is None or not (target.get('boarding_deadline_source') or '').strip():
            buffer = None
            issues.append('unreconciled_boarding_cutoff')
        else:
            if cutoff > departure:
                raise AssessmentError('Boarding cutoff cannot follow departure')
            buffer = max(buffer, departure - cutoff)  # Earlier threshold, never sum.
    inputs = TransferInput(spec.journey_id, spec.transfer_id, spec.earliest_arrival,
        spec.latest_arrival, departure, buffer, quality, spec.evaluated_at)
    assessment = evaluate_transfer(inputs)
    canonical_spec = asdict(spec)
    canonical_spec['observation_ids'] = sorted(spec.observation_ids)
    frozen_context = _read_json(canonical_json(context))
    frozen_context['legs'].sort(key=lambda row: row['leg_order'])
    frozen_context['observations'].sort(key=lambda row: row['observation_id'])
    for row in [frozen_context['journey'], *frozen_context['legs'], *frozen_context['observations']]:
        for field in ('created_at', 'planned_departure_at', 'planned_arrival_at',
                      'boarding_deadline_at', 'source_updated_at', 'fetched_at', 'ingested_at'):
            if row.get(field) is not None:
                row[field] = _time(row[field])
    snapshot = {'contract_version': MODEL_VERSION, 'request': canonical_spec,
        'context': frozen_context, 'engine_input': asdict(inputs), 'quality_issues': sorted(set(issues)),
        'provenance': {'arrival_location': 'target_boarding_location',
            'arrival_interval_already_includes_prior_durations': True}}
    # Identity hashes INPUTS, not the result: changed output for identical inputs
    # is a conflict, not a new history record.
    key = hashlib.sha256(canonical_json(snapshot).encode('utf-8')).hexdigest()
    snapshot['result'] = asdict(assessment)
    return {'assessment_key': key, 'journey_id': spec.journey_id,
        'target_transfer_id': spec.transfer_id, 'target_leg_id': None,
        'status': STATUS_MAP[assessment.status], 'reason_code': assessment.reason_code,
        'model_version': MODEL_VERSION, 'reference_data_version': spec.reference_version,
        'assumption_version': spec.assumption_version, 'evidence_observation_id': evidence_id,
        'evaluated_at': _time(spec.evaluated_at), 'input_snapshot': _canonical(snapshot)}


def persist_assessment(record, execute_sql):
    expected = canonical_json(record)
    key = _literal(record['assessment_key'])
    # Separate statements avoid the concurrent ON CONFLICT/single-CTE visibility
    # trap at READ COMMITTED. A conflict rolls back and never changes saved rows.
    sql = f"""BEGIN ISOLATION LEVEL READ COMMITTED;
        INSERT INTO impact_assessments (journey_id,assessment_key,status,reason_code,
            model_version,reference_data_version,assumption_version,input_snapshot,
            evidence_observation_id,evaluated_at,target_transfer_id,target_leg_id)
        SELECT journey_id,assessment_key,status,reason_code,model_version,
            reference_data_version,assumption_version,input_snapshot,evidence_observation_id,
            evaluated_at,target_transfer_id,target_leg_id
        FROM jsonb_populate_record(NULL::impact_assessments,{_literal(expected)}::jsonb)
        ON CONFLICT (assessment_key) DO NOTHING;
        SELECT assessment_id / CASE WHEN (to_jsonb(a)-'assessment_id') =
            ((SELECT to_jsonb(e) FROM jsonb_populate_record(NULL::impact_assessments,
              {_literal(expected)}::jsonb) e)-'assessment_id') THEN 1 ELSE 0 END
        FROM impact_assessments a WHERE assessment_key={key};
        COMMIT;"""
    # Division by zero intentionally aborts the transaction on a conflicting key.
    # No dollar-quoted block embeds source JSON or allows delimiters to escape it.
    try:
        identity = int(execute_sql(sql).strip())
        if identity <= 0:
            raise ValueError()
        return identity
    except Exception:
        raise AssessmentError('Assessment persistence failed or key conflicts; retry identical inputs to resolve the outcome') from None


def assess_saved_transfer(spec, execute_sql):
    record = build_assessment(spec, load_transfer_context(spec, execute_sql))
    identity = persist_assessment(record, execute_sql)
    return {'assessment_id': identity, 'status': record['status'],
            'reason_code': record['reason_code'], 'assessment_key': record['assessment_key']}


def psql_json_executor(database):
    # Same CLI/libpq approach as ingestion; assessment reads also return JSON,
    # whereas the unchanged ingestion executor deliberately accepts scalar IDs.
    import re
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', database):
        raise AssessmentError('Use a plain database name')
    def execute(sql):
        try:
            result = subprocess.run(['psql', '-X', '-w', '-A', '-t', '-q', '-v',
                'ON_ERROR_STOP=1', '--dbname', database], input=sql, text=True,
                capture_output=True, timeout=30,
                env={key: value for key, value in os.environ.items() if not key.startswith('TDX_')})
        except (OSError, subprocess.TimeoutExpired):
            raise AssessmentError('Database execution unavailable; retry the same assessment inputs') from None
        if result.returncode:
            raise AssessmentError('Database rejected assessment operation; no overwrite performed')
        return result.stdout.strip()
    return execute


def demonstrate(journey_id, transfer_id, artifact_path, execute_sql):
    """Saved synthetic seed + two explicit intervals + one saved observation case."""
    from .live import replay
    from .n1 import FrozenSnapshot
    saved = FrozenSnapshot.from_json(Path(artifact_path).read_text())
    initial = AssessmentSpec(journey_id, transfer_id, _time(saved.fetched_at),
        'demo-reference-v2', 'synthetic-assumptions-v2', 'synthetic')
    context = load_transfer_context(initial, execute_sql)
    if not context['transfer']:
        raise AssessmentError('Specify an existing synthetic seed transfer')
    target = next(leg for leg in context['legs'] if leg['leg_id'] == context['transfer']['to_leg_id'])
    if target['transport_mode'] == 'walking' or target.get('boarding_deadline_at') is not None:
        raise AssessmentError('Demo needs a boarding transfer without a custom cutoff')
    departure = _time(target['planned_departure_at'])
    deadline = departure - timedelta(minutes=context['transfer']['min_transfer_minutes'])
    results = []
    for label, earliest, latest in [('synthetic_on_track', deadline - timedelta(minutes=2), deadline - timedelta(minutes=1)),
                                    ('synthetic_at_risk', deadline - timedelta(minutes=1), deadline + timedelta(minutes=1))]:
        spec = AssessmentSpec(journey_id, transfer_id, departure - timedelta(hours=1),
            initial.reference_version, initial.assumption_version, 'synthetic', earliest, latest,
            'synthetic', 'SYNTHETIC demo interval at target boarding location; includes walking', 'boarding_only')
        results.append({'case': label, **assess_saved_transfer(spec, execute_sql)})
    matching = [leg for leg in context['legs'] if leg['transport_mode'] == 'bus'
        and leg.get('route_uid') == saved.scope.route_uid and leg.get('direction') == saved.scope.direction
        and leg.get('origin_stop_uid') in saved.scope.stop_uids]
    if len(matching) != 1:
        raise AssessmentError('Saved artifact must match exactly one journey bus leg')
    evidence_leg = matching[0]
    run = replay(artifact_path, execute_sql)  # Existing ingestion persistence, zero HTTP.
    ids = json.loads(execute_sql('SELECT COALESCE(json_agg(observation_id ORDER BY observation_id),\'[]\'::json) '
        + 'FROM source_observations WHERE run_id=' + str(run['database_run_id'])
        + ' AND stop_uid=' + _literal(evidence_leg['origin_stop_uid'])))
    spec = AssessmentSpec(journey_id, transfer_id, _time(saved.fetched_at),
        initial.reference_version, initial.assumption_version, 'source_backed',
        buffer_semantics='boarding_only', observation_ids=tuple(ids),
        evidence_leg_id=evidence_leg['leg_id'], evidence_stop_uid=evidence_leg['origin_stop_uid'])
    results.append({'case': 'saved_observation_with_synthetic_journey_reference',
                    **assess_saved_transfer(spec, execute_sql)})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Assess one saved transfer without HTTP')
    commands = parser.add_subparsers(dest='command', required=True)
    assess = commands.add_parser('assess')
    assess.add_argument('input', type=Path, help='Explicit assessment JSON')
    assess.add_argument('--database', required=True)
    demo = commands.add_parser('demo')
    demo.add_argument('--journey-id', type=int, required=True)
    demo.add_argument('--transfer-id', type=int, required=True)
    demo.add_argument('--real-artifact', type=Path, required=True)
    demo.add_argument('--database', required=True)
    args = parser.parse_args(argv)
    try:
        execute = psql_json_executor(args.database)
        if args.command == 'demo':
            print(json.dumps(demonstrate(args.journey_id, args.transfer_id, args.real_artifact, execute)))
            return 0
        raw = _read_json(args.input.read_text())
        for field in ('evaluated_at', 'earliest_arrival', 'latest_arrival'):
            if raw.get(field) is not None:
                raw[field] = _time(raw[field])
        raw['observation_ids'] = tuple(raw.get('observation_ids', []))
        print(json.dumps(assess_saved_transfer(AssessmentSpec(**raw), execute)))
    except (ValueError, TypeError, KeyError, OSError):
        print('Assessment failed: check explicit inputs, saved context and database access.', file=__import__('sys').stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
