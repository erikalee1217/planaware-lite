"""Presentation of saved assessment results. No risk evaluation or DB writes."""
import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .assessment import STATUS_MAP, AssessmentError, psql_json_executor, to_utc_datetime
from .n1 import ValidationError, parse_strict_json

EXPLANATIONS = {
    'within_buffer': 'Conditionally feasible based on saved evidence and assumptions (boarding is not guaranteed).',
    'deadline_overlap': 'The saved arrival window reaches or crosses the boarding deadline. The connection is at risk.',
    'deadline_missed': 'The earliest saved arrival is at or after the boarding deadline. The actual boarding outcome is unknown.',
    'evidence_missing': 'No usable source evidence was selected for this assessment.',
    'evidence_unusable': 'The saved evidence or timing assumptions do not support a reliable assessment.',
    'evidence_stale': 'The selected source observation was stale under the saved freshness policy.',
    'missing_arrival_interval': 'The arrival interval at the target boarding location is missing or incomplete.',
    'missing_departure': 'The next scheduled departure is missing.',
    'missing_boarding_buffer': 'The required boarding allowance is missing or cannot be reconciled.',
}
ISSUES = {
    'missing_observation': 'No source observation is available.',
    'non_normal_stop_status': 'Source status does not support a normal in-service prediction.',
    'missing_or_invalid_estimate': 'ETA is missing or invalid.',
    'missing_source_time': 'Source update time is missing.',
    'stale_source_time': 'Source update time is stale.',
    'delayed_observation': 'The latest retrieved observation contains an older source state.',
    'conflicting_observations': 'Observations at the same source time conflict.',
    'future_or_inconsistent_clock': 'Source or retrieval timestamps are inconsistent with evaluation time.',
    'evidence_scope_mismatch': 'Evidence does not match the configured route, direction or stop.',
    'invalid_evidence_leg_or_stop': 'The evidence leg or stop is not a valid match.',
    'evidence_leg_after_transfer_origin': 'Evidence belongs to a leg after the assessed transfer origin.',
    'invalid_raw_codes': 'Source numeric codes are invalid.',
    'raw_normalized_mismatch': 'Raw and normalized observation fields disagree.',
    'invalid_raw_source_time': 'Raw source update time is invalid.',
    'raw_source_time_mismatch': 'Raw and normalized source update times disagree.',
    'missing_invalid_or_expired_reference': 'Reference data is missing, mismatched or outside its validity dates.',
    'missing_transfer_provenance': 'The transfer allowance lacks provenance.',
    'unknown_walking_duration': 'A required walking duration is unknown.',
    'unvalidated_arrival_interval': 'The supplied arrival interval lacks an accepted validation basis.',
    'unknown_buffer_semantics': 'The boarding allowance is not distinguished from prior walking/access time.',
    'unreconciled_boarding_cutoff': 'The boarding cutoff and allowance cannot be reconciled.',
    'target_is_not_boarding_leg': 'The target is a walking leg, not a boarding connection.',
}


class BriefError(ValueError):
    pass


def format_buffer(microseconds):
    """Format an existing signed duration exactly; never calculate risk/slack."""
    if microseconds is None:
        return None
    if type(microseconds) is not int:
        raise BriefError('Saved minimum buffer must be integer microseconds or null')
    sign = '-' if microseconds < 0 else '+'
    seconds, fraction = divmod(abs(microseconds), 1000000)
    if not fraction and seconds and seconds % 60 == 0:
        return f'{sign}{seconds // 60} min'
    digits = f'{seconds}.{fraction:06d}'.rstrip('0') if fraction else str(seconds)
    return f'{sign}{digits} s'


def _leg_label(leg):
    modes = {'walking': 'Walk', 'bus': 'Bus', 'metro': 'MRT', 'intercity_bus': 'Intercity bus',
             'train': 'Train', 'high_speed_rail': 'High-speed rail'}
    label = modes.get(leg['transport_mode'], leg['transport_mode'])
    reference = leg.get('reference_details', {})
    if leg['transport_mode'] == 'walking' and reference.get('destination_name'):
        return label + ' to ' + reference['destination_name']
    route = reference.get('route_name') or leg.get('route_uid')
    return label + (' ' + route if route else '')


def build_brief(row):
    """Read only persisted inputs/results; return a new JSON-serializable view."""
    try:
        snapshot = row['input_snapshot']
        result, inputs, request = snapshot['result'], snapshot['engine_input'], snapshot['request']
        context = snapshot['context']
        status, reason = row['status'], row['reason_code']
        if status not in STATUS_MAP.values() or STATUS_MAP.get(result['status']) != status or result['reason_code'] != reason or reason not in EXPLANATIONS:
            raise BriefError('Inconsistent or unsupported saved assessment result')
        if (status == 'on_track' and reason != 'within_buffer') or (
            status == 'at_risk' and reason not in ('deadline_overlap', 'deadline_missed')) or (
            status == 'insufficient_evidence' and reason in ('within_buffer', 'deadline_overlap', 'deadline_missed')):
            raise BriefError('Saved status and reason do not follow the supported result contract')
        transfer = context['transfer']
        if row['journey_id'] != context['journey']['journey_id'] or row['target_transfer_id'] != transfer['transfer_id']:
            raise BriefError('Inconsistent saved journey/transfer identity')
        zone_name = request['service_timezone']
        zone = ZoneInfo(zone_name)
        def local(value):
            return None if value is None else to_utc_datetime(value).astimezone(zone).isoformat(sep=' ')
        legs = {leg['leg_id']: leg for leg in context['legs']}
        origin, target = legs[transfer['from_leg_id']], legs[transfer['to_leg_id']]
        # Only explicit saved provenance is described. Absence of a synthetic
        # marker is not a claim of independently verified timetable accuracy.
        references_synthetic = request['scenario'] == 'synthetic' or any('synthetic' in str(value).lower() for value in (
            request['assumption_version'], context['journey']['journey_name'],
            origin.get('reference_details', {}), target.get('reference_details', {})))
        if request['scenario'] == 'synthetic':
            evidence = 'Synthetic arrival interval; no source observation used.'
        elif request['scenario'] == 'source_backed':
            selected = next((observation for observation in context['observations']
                if observation['observation_id'] == row['evidence_observation_id']), None)
            if selected is None:
                evidence = 'No saved source observation selected.'
            else:
                fixture = selected['raw_payload'].get('synthetic_test_only') is True
                evidence = ('Synthetic N1 observation fixture' if fixture else 'Saved TDX N1 observation (source-backed)')
                evidence += f"; StopStatus={selected['stop_status']}; "
                evidence += 'ETA missing.' if selected['estimate_seconds'] is None else f"source ETA={selected['estimate_seconds']} s (not a downstream arrival interval)."
        else:
            raise BriefError('Unsupported saved scenario provenance')
        provenance = 'Synthetic journey/timing assumptions.' if references_synthetic else 'Based on saved reference assumptions.'
        missing = [ISSUES.get(issue, 'Reported evidence issue: ' + issue.replace('_', ' ') + '.')
                   for issue in snapshot['quality_issues']]
        if inputs['earliest_arrival'] is None or inputs['latest_arrival'] is None:
            missing.append('Arrival interval at the target boarding location is missing or incomplete.')
        if inputs['next_departure'] is None:
            missing.append('Scheduled departure is missing.')
        if inputs['boarding_buffer'] is None:
            missing.append('Required boarding allowance is unavailable or unreconciled.')
        if status == 'on_track':
            next_check = 'Confirm the next service\'s published departure and boarding requirements.'
        elif status == 'at_risk':
            next_check = 'Check the next service\'s departure and boarding requirements.'
        elif 'non_normal_stop_status' in snapshot['quality_issues']:
            next_check = 'Check whether the bus is operating before relying on an ETA.'
        elif reason == 'evidence_stale':
            next_check = 'Check for a newer usable source observation.'
        elif 'target_is_not_boarding_leg' in snapshot['quality_issues']:
            next_check = 'Check that the selected target is a boarding connection.'
        elif 'missing_invalid_or_expired_reference' in snapshot['quality_issues']:
            next_check = 'Confirm the reference data and its validity dates.'
        elif reason == 'missing_boarding_buffer' or 'unknown_walking_duration' in snapshot['quality_issues']:
            next_check = 'Verify the missing walking or boarding-time assumption.'
        else:
            next_check = 'Provide an arrival interval for the target boarding location.'
        return {'assessment_id': row['assessment_id'], 'journey_id': row['journey_id'],
            'journey_name': context['journey']['journey_name'], 'transfer_id': row['target_transfer_id'],
            'transfer': _leg_label(origin) + ' → ' + _leg_label(target),
            'status': status, 'reason_code': reason, 'explanation': EXPLANATIONS[reason],
            'timezone': zone_name, 'evaluated_at': local(row['evaluated_at']),
            'arrival_interval': {'earliest': local(inputs['earliest_arrival']), 'latest': local(inputs['latest_arrival'])},
            'deadline_label': 'Assumed latest arrival for boarding' if references_synthetic else 'Assessed latest arrival for boarding',
            'boarding_deadline': local(result['boarding_deadline']),
            'minimum_buffer_microseconds': result['minimum_buffer'],
            'minimum_buffer': format_buffer(result['minimum_buffer']),
            'evidence_source': evidence, 'assumptions': provenance,
            'missing_or_unusable': list(dict.fromkeys(missing)), 'next_check': next_check}
    except (KeyError, TypeError, AttributeError):
        raise BriefError('A complete persisted assessment with input_snapshot is required; a demo summary is insufficient') from None


def format_brief(row):
    brief = build_brief(row)
    arrival = brief['arrival_interval']
    interval = 'not available' if arrival['earliest'] is None and arrival['latest'] is None else (
        f"{arrival['earliest'] or 'unknown'} — {arrival['latest'] or 'unknown'}")
    lines = [f"Journey {brief['journey_id']}: {brief['journey_name']}",
        f"Transfer {brief['transfer_id']}: {brief['transfer']}",
        f"Assessment {brief['assessment_id']}: {brief['status']} ({brief['reason_code']})",
        brief['explanation'], f"Saved arrival window: {interval}",
        f"{brief['deadline_label']}: {brief['boarding_deadline'] or 'not available in the saved result'}",
        f"Minimum buffer: {brief['minimum_buffer'] if brief['minimum_buffer'] is not None else 'not available'}",
        f"Evidence: {brief['evidence_source']} {brief['assumptions']}"]
    if brief['missing_or_unusable']:
        lines.append('Missing/unusable: ' + ' '.join(brief['missing_or_unusable']))
    lines.extend([f"Evaluated: {brief['evaluated_at']}", f"Next check: {brief['next_check']}"])
    return '\n'.join(lines)


def load_assessment(identity, execute_sql):
    if type(identity) is not int or identity <= 0:
        raise BriefError('Assessment ID must be a positive integer')
    # The only DB operation in this module is this single SELECT.
    row = parse_strict_json(execute_sql(f'SELECT row_to_json(a) FROM impact_assessments a WHERE assessment_id={identity};'))
    if row is None:
        raise BriefError('Assessment not found')
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description='Render persisted assessments without evaluating or writing')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--assessment-id', type=int)
    source.add_argument('--input', type=Path, help='Full exported assessment row or array, not a demo summary')
    parser.add_argument('--database')
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    args = parser.parse_args(argv)
    if args.assessment_id is not None and not args.database:
        parser.error('--database is required with --assessment-id')
    try:
        data = load_assessment(args.assessment_id, psql_json_executor(args.database)) if args.assessment_id is not None else parse_strict_json(args.input.read_text())
        rows = data if isinstance(data, list) else [data]
        # Build all output first: invalid later rows do not yield a partial brief.
        if args.format == 'json':
            result = [build_brief(row) for row in rows]
            output = json.dumps(result if isinstance(data, list) else result[0], ensure_ascii=False, indent=2)
        else:
            output = '\n\n'.join(format_brief(row) for row in rows)
        print(output)
    except (BriefError, AssessmentError, ValidationError, OSError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError):
        print('Cannot render brief: provide a complete supported assessment and valid database/file access.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
