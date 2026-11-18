"""TDX Bus N1 validation, freezing, SQL delegation and small argparse CLI.

Only the verified bare-array response and explicitly configured route scope are
accepted. Optional live commands delegate to .live; normalization stays offline.
No vehicle inference or impact evaluation.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from typing import Callable
from uuid import uuid4


FORMAT_VERSION = 'tdx-n1-fixture-v1'
STATUS_LABELS = {
    0: 'normal', 1: 'not_departed', 2: 'not_stopping',
    3: 'service_ended', 4: 'not_operating',
}
TIMESTAMPS = {
    'SrcUpdateTime': 'source_updated_at',
    'UpdateTime': 'tdx_updated_at',
}


class ValidationError(ValueError):
    """The complete snapshot is unusable structurally; nothing may be persisted."""


@dataclass(frozen=True)
class N1Scope:
    route_uid: str
    direction: int
    stop_uids: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.route_uid, str) or not self.route_uid.strip():
            raise ValidationError('Configured RouteUID must be nonblank')
        _integer(self.direction, 'configured direction', 0, 255)
        if not isinstance(self.stop_uids, tuple) or not self.stop_uids or any(
            not isinstance(stop, str) or not stop.strip() for stop in self.stop_uids
        ) or len(set(self.stop_uids)) != len(self.stop_uids):
            raise ValidationError('Configured StopUIDs must be a nonempty unique tuple')
        _read_json(_json_text(self.as_dict()))

    def as_dict(self):
        return {'route_uid': self.route_uid, 'direction': self.direction,
                'stop_uids': list(self.stop_uids)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'route_uid', 'direction', 'stop_uids'} or not isinstance(value['stop_uids'], list):
            raise ValidationError('Scope requires route_uid, direction and stop_uids array')
        return cls(value['route_uid'], value['direction'], tuple(value['stop_uids']))

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(_read_json(text))


def _json_text(value):
    """Stable JSON with Decimal numbers kept numeric, never rounded via float."""
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + _json_text(value[k])
                              for k in sorted(value)) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_json_text(item) for item in value) + ']'
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationError('JSON numbers must be finite')
        return str(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _read_json(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError('Duplicate JSON object keys are not accepted')
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValidationError('Nonstandard JSON numeric constants are not accepted')

    try:
        value = json.loads(text, parse_float=Decimal, parse_constant=invalid_constant,
                           object_pairs_hook=object_pairs)
        # PostgreSQL JSONB cannot represent NUL or unpaired Unicode surrogates.
        def check_strings(item):
            if isinstance(item, str):
                if '\x00' in item:
                    raise ValidationError('JSON strings cannot contain NUL')
                item.encode('utf-8', errors='strict')
            elif isinstance(item, dict):
                for key, val in item.items():
                    check_strings(key)
                    check_strings(val)
            elif isinstance(item, list):
                for val in item:
                    check_strings(val)
        check_strings(value)
        return value
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        # Do not echo source contents in diagnostic messages.
        raise ValidationError('Malformed JSON or unsupported Unicode/nesting') from error


def _timestamp(value, field):
    # No local-time guessing, whitespace repair or discarded fractional digits.
    if not isinstance(value, str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value
    ):
        raise ValidationError(f'{field} requires an offset-aware ISO timestamp')
    if not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValidationError(f'{field} contains an invalid UTC offset')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValidationError(f'{field} contains an invalid timestamp') from error
    return parsed


def _metadata(run_key, requested_at, fetched_at):
    if not isinstance(run_key, str) or not run_key.strip() or '\x00' in run_key:
        raise ValidationError('A nonblank run key is required')
    _read_json(_json_text(run_key))
    requested = _timestamp(requested_at, 'requested_at')
    fetched = _timestamp(fetched_at, 'fetched_at')
    if fetched < requested:
        raise ValidationError('fetched_at cannot precede requested_at')


def _integer(value, field, lower, upper):
    # bool is an int subclass in Python but not a valid source numeric code.
    if type(value) is not int or not lower <= value <= upper:
        raise ValidationError(f'{field} requires an integer in the accepted range')
    return value


def _normalize(records, scope):
    if not isinstance(records, list):
        raise ValidationError('N1 response must be a bare JSON array, not Items/LiveEvents or an object')
    normalized = []
    for index, raw in enumerate(records):
        prefix = f'record[{index}]'
        if not isinstance(raw, dict):
            raise ValidationError(f'{prefix} must be an object')
        if raw.get('RouteUID') != scope.route_uid:
            raise ValidationError(f'{prefix}.RouteUID is outside the configured route')
        if _integer(raw.get('Direction'), prefix + '.Direction', 0, 255) != scope.direction:
            raise ValidationError(f'{prefix}.Direction is outside the configured direction')
        stop = raw.get('StopUID')
        if not isinstance(stop, str) or stop not in scope.stop_uids:
            raise ValidationError(f'{prefix}.StopUID is outside the configured stops')
        for name in ('RouteID', 'StopID'):
            if raw.get(name) is not None and not isinstance(raw[name], str):
                raise ValidationError(f'{prefix}.{name} must be a string or null')
        for name in ('StopName', 'RouteName'):
            if name in raw and raw[name] is not None:
                if not isinstance(raw[name], dict):
                    raise ValidationError(f'{prefix}.{name} must be an object or null')
                for text in raw[name].values():
                    if text is not None and not isinstance(text, str):
                        raise ValidationError(f'{prefix}.{name} text must be a string or null')
        for name in ('PlateNumb', 'SubRouteUID'):
            if raw.get(name) is not None and not isinstance(raw[name], str):
                raise ValidationError(f'{prefix}.{name} must be a string or null')

        flags = []
        estimate = raw.get('EstimateTime')
        if estimate is None:
            flags.append('missing_estimate' if 'EstimateTime' not in raw else 'null_estimate')
        else:
            estimate = _integer(estimate, prefix + '.EstimateTime', -2147483648, 2147483647)
            if estimate < 0:
                flags.append('negative_estimate_preserved_in_raw')
                estimate = None
        status = raw.get('StopStatus')
        if status is None:
            flags.append('missing_stop_status' if 'StopStatus' not in raw else 'null_stop_status')
        else:
            status = _integer(status, prefix + '.StopStatus', 0, 32767)
            if status not in STATUS_LABELS:
                flags.append('unrecognized_stop_status')
        if status != 0:
            flags.append('not_a_normal_status_prediction')

        row = {
            'route_uid': raw['RouteUID'], 'direction': raw['Direction'], 'stop_uid': stop,
            'estimate_seconds': estimate, 'stop_status': status,
            'eta_status': STATUS_LABELS.get(status, 'unknown'),
            'normalization_version': FORMAT_VERSION,
            # Saved with every normalized row so PG's existing conflict check
            # also detects changed scope, without another persistence mechanism.
            'scope': scope.as_dict(),
        }
        for source_field, normalized_field in TIMESTAMPS.items():
            value = raw.get(source_field)
            if value is not None:
                _timestamp(value, prefix + '.' + source_field)
            # Keep the original offset/text, never substitute a different clock.
            row[normalized_field] = value
        if row['source_updated_at'] is None:
            flags.append('missing_source_updated_at' if 'SrcUpdateTime' not in raw else 'null_source_updated_at')
        row['quality_flags'] = flags
        row['raw_payload'] = raw
        normalized.append(row)
    return normalized


@dataclass(frozen=True)
class FrozenSnapshot:
    run_key: str
    requested_at: str
    fetched_at: str
    records_json: str
    scope: N1Scope

    def __post_init__(self):
        _metadata(self.run_key, self.requested_at, self.fetched_at)
        records = _read_json(self.records_json)
        if not isinstance(records, list) or any(
            not isinstance(row, dict) or 'raw_payload' not in row for row in records
        ):
            raise ValidationError('Frozen records must contain original raw_payload objects')
        if not isinstance(self.scope, N1Scope):
            raise ValidationError('Frozen snapshot requires a validated scope')
        if _json_text(records) != _json_text(_normalize([row['raw_payload'] for row in records], self.scope)):
            raise ValidationError('Frozen normalized fields do not match their raw evidence/version')

    def to_json(self):
        return _json_text({
            'format_version': FORMAT_VERSION, 'run_key': self.run_key,
            'requested_at': self.requested_at, 'fetched_at': self.fetched_at,
            'records': _read_json(self.records_json),
            'scope': self.scope.as_dict(),
        })

    @classmethod
    def from_json(cls, text):
        artifact = _read_json(text)
        keys = {'format_version', 'run_key', 'requested_at', 'fetched_at', 'records', 'scope'}
        if not isinstance(artifact, dict) or set(artifact) != keys or artifact['format_version'] != FORMAT_VERSION:
            raise ValidationError('Unsupported frozen snapshot format or fields')
        return cls(artifact['run_key'], artifact['requested_at'], artifact['fetched_at'],
                   _json_text(artifact['records']), N1Scope.from_dict(artifact['scope']))

    def to_sql(self):
        # Explicit E-string escaping is independent of standard_conforming_strings.
        # No shell interpolation, identifiers or ad-hoc INSERT/UPSERT statements.
        def literal(value):
            return "E'" + value.replace('\\', '\\\\').replace("'", "''") + "'"
        return 'SELECT public.persist_tdx_n1_snapshot(' + ','.join((
            literal(self.run_key), literal(self.requested_at) + '::timestamptz',
            literal(self.fetched_at) + '::timestamptz', literal(self.records_json) + '::jsonb',
        )) + ');'


def freeze_n1_fixture(response_text, *, scope, requested_at, fetched_at, run_key=None):
    """Create ONE acquisition. Replay the saved artifact, not this constructor."""
    if run_key is None:
        run_key = 'tdx-n1-fixture:' + uuid4().hex
    if not isinstance(scope, N1Scope):
        raise ValidationError('A validated N1Scope is required')
    return FrozenSnapshot(run_key, requested_at, fetched_at,
                          _json_text(_normalize(_read_json(response_text), scope)), scope)


def persist_snapshot(snapshot: FrozenSnapshot, execute_sql: Callable[[str], str]) -> int:
    """Delegate once to an existing SQL executor; all replay decisions stay in PG."""
    return int(execute_sql(snapshot.to_sql()).strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description='N1 fixture, manually bounded acquisition and replay')
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('input', type=Path)
    freeze.add_argument('--output', type=Path, required=True)
    freeze.add_argument('--requested-at', required=True)
    freeze.add_argument('--fetched-at', required=True)
    freeze.add_argument('--run-key')
    freeze.add_argument('--config', type=Path, required=True)
    sql = commands.add_parser('sql')
    sql.add_argument('input', type=Path)
    live = commands.add_parser('fetch-once')
    live.add_argument('--config', type=Path, required=True)
    live.add_argument('--city', required=True)
    live.add_argument('--max-records', type=int, required=True)
    live.add_argument('--output', type=Path, required=True)
    live.add_argument('--database', required=True)
    live.add_argument('--timeout', type=float, default=15)
    live.add_argument('--confirm-free-quota', action='store_true')
    replay_command = commands.add_parser('replay')
    replay_command.add_argument('input', type=Path)
    replay_command.add_argument('--database', required=True)
    args = parser.parse_args(argv)
    if args.command in ('fetch-once', 'replay'):
        from .live import PipelineError, fetch_once, psql_executor, replay
        try:
            executor = psql_executor(args.database)
            if args.command == 'replay':
                result = replay(args.input, executor)
            else:
                scope = N1Scope.from_json(args.config.read_text(encoding='utf-8'))
                result = fetch_once(scope=scope, city=args.city, max_records=args.max_records,
                    output=args.output, execute_sql=executor, timeout=args.timeout,
                    confirm_free_quota=args.confirm_free_quota)
            print(json.dumps(result))
        except PipelineError as error:
            print(str(error), file=sys.stderr)
            return 2
        except (ValueError, OSError, TypeError):
            # Config/files/HTTP/psql errors might contain secrets. Fixed diagnostic.
            print('N1 workflow failed; no automatic retry. If an artifact exists, use replay. Check configuration, credentials, quota, HTTP and database access privately.', file=sys.stderr)
            return 2
        return 0
    try:
        text = args.input.read_text(encoding='utf-8')
        if args.command == 'freeze':
            scope = N1Scope.from_json(args.config.read_text(encoding='utf-8'))
            snapshot = freeze_n1_fixture(text, scope=scope, requested_at=args.requested_at,
                                         fetched_at=args.fetched_at, run_key=args.run_key)
            artifact = snapshot.to_json()
            # Never silently replace existing frozen evidence, even with a new key.
            with args.output.open('x', encoding='utf-8') as output:
                output.write(artifact + '\n')
        else:
            print(FrozenSnapshot.from_json(text).to_sql())
    except (ValidationError, OSError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
