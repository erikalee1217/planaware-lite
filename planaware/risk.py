"""Deterministic single-transfer assessment; no I/O or time propagation."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

EvidenceQuality = Literal['usable', 'missing', 'unusable', 'stale']
RiskStatus = Literal['connection_feasible', 'connection_at_risk', 'insufficient_evidence']
ReasonCode = Literal[
    'evidence_missing', 'evidence_unusable', 'evidence_stale',
    'missing_arrival_interval', 'missing_departure', 'missing_boarding_buffer',
    'deadline_missed', 'deadline_overlap', 'within_buffer',
]


class RiskInputError(ValueError):
    """Malformed input, distinct from valid input with missing evidence."""


@dataclass(frozen=True)
class TransferInput:
    journey_id: int
    transfer_id: int
    earliest_arrival: datetime | None
    latest_arrival: datetime | None
    next_departure: datetime | None
    boarding_buffer: timedelta | None
    evidence_quality: EvidenceQuality
    evaluated_at: datetime


@dataclass(frozen=True)
class TransferAssessment:
    journey_id: int
    transfer_id: int
    status: RiskStatus
    reason_code: ReasonCode
    evaluated_at: datetime
    boarding_deadline: datetime | None
    minimum_buffer: timedelta | None


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RiskInputError(f'{field} must be a timezone-aware datetime')
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RiskInputError(f'{field} cannot be represented in UTC') from None


def evaluate_transfer(inputs: TransferInput) -> TransferAssessment:
    """Bounds already include walking/access durations. No durations are added."""
    if not isinstance(inputs, TransferInput):
        raise RiskInputError('Expected TransferInput')
    for field in ('journey_id', 'transfer_id'):
        value = getattr(inputs, field)
        if type(value) is not int or value <= 0:
            raise RiskInputError(f'{field} must be a positive integer')
    if inputs.evidence_quality not in ('usable', 'missing', 'unusable', 'stale'):
        raise RiskInputError('Unknown evidence quality')
    evaluated_at = _utc(inputs.evaluated_at, 'evaluated_at')
    times = {}
    for field in ('earliest_arrival', 'latest_arrival', 'next_departure'):
        value = getattr(inputs, field)
        times[field] = None if value is None else _utc(value, field)
    earliest, latest, departure = (times[field] for field in (
        'earliest_arrival', 'latest_arrival', 'next_departure'))
    buffer = inputs.boarding_buffer
    if buffer is not None and (not isinstance(buffer, timedelta) or buffer < timedelta(0)):
        raise RiskInputError('boarding_buffer must be a nonnegative timedelta or None')
    if earliest is not None and latest is not None and earliest > latest:
        raise RiskInputError('earliest_arrival cannot follow latest_arrival')

    def result(status: RiskStatus, reason: ReasonCode, deadline=None, minimum=None):
        return TransferAssessment(inputs.journey_id, inputs.transfer_id, status,
                                  reason, evaluated_at, deadline, minimum)

    # Structural validation precedes policy: malformed evidence is never silently
    # accepted just because another field would yield insufficient evidence.
    if inputs.evidence_quality != 'usable':
        reasons: dict[str, ReasonCode] = {
            'missing': 'evidence_missing', 'unusable': 'evidence_unusable', 'stale': 'evidence_stale'}
        return result('insufficient_evidence', reasons[inputs.evidence_quality])
    if earliest is None or latest is None:
        return result('insufficient_evidence', 'missing_arrival_interval')
    if departure is None:
        return result('insufficient_evidence', 'missing_departure')
    if buffer is None:
        return result('insufficient_evidence', 'missing_boarding_buffer')
    try:
        deadline = departure - buffer
    except OverflowError:
        raise RiskInputError('boarding deadline is outside the supported datetime range') from None
    minimum = deadline - latest
    if earliest >= deadline:
        return result('connection_at_risk', 'deadline_missed', deadline, minimum)
    if latest >= deadline:
        return result('connection_at_risk', 'deadline_overlap', deadline, minimum)
    return result('connection_feasible', 'within_buffer', deadline, minimum)
