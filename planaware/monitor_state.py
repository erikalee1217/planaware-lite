"""Pure, session-local monitoring of previously validated observations and saved risk."""
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Literal

from .assessment import canonical_json, evidence_clock_issue, STATUS_MAP, to_utc_datetime

Identity = int | str
Clock = datetime | str
Origin = Literal["live", "synthetic"]
Lifecycle = Literal["active", "paused", "stopped"]
Acquisition = Literal["none", "ok", "failed"]
Freshness = Literal["none", "fresh", "stale"]
Risk = Literal["on_track", "at_risk", "insufficient_evidence"]
Control = Literal["pause", "resume", "stop"]
EventType = Literal["evidence_became_stale", "acquisition_failed", "evidence_recovered", "risk_changed"]


@dataclass(frozen=True)
class SavedAssessment:
    assessment_id: Identity
    status: Risk


@dataclass(frozen=True)
class Observation:
    """For empty or failed acquisitions, use the stable acquisition identity.

    Supply a source clock only for evidence admitted by the existing quality
    gate (including stale evidence). A successful fetch can yield no evidence.
    """
    identity: Identity
    origin: Origin
    result: Literal["ok", "failed"]
    source_updated_at: Clock | None = None
    fetched_at: Clock | None = None
    assessment: SavedAssessment | None = None


@dataclass(frozen=True)
class MonitorState:
    session_id: str
    origin: Origin
    lifecycle: Lifecycle = "active"
    acquisition: Acquisition = "none"
    freshness: Freshness = "none"
    risk: Risk | None = None
    source_updated_at: datetime | None = None
    fetched_at: datetime | None = None
    evaluated_at: datetime | None = None
    seen_observations: frozenset[Identity] = frozenset()
    seen_assessments: frozenset[Identity] = frozenset()

    @property
    def is_active(self) -> bool:
        return self.lifecycle == "active"


@dataclass(frozen=True)
class MonitorEvent:
    event_id: str
    session_id: str
    kind: EventType
    evaluated_at: datetime
    previous: str
    current: str


def transition(
    state: MonitorState,
    input_or_tick: Observation | SavedAssessment | Control | None,
    evaluated_at: Clock,
) -> tuple[MonitorState, tuple[MonitorEvent, ...]]:
    """None advances time; paused sessions age evidence and ignore data inputs."""
    if state.lifecycle == "stopped":
        return state, ()
    evaluated = to_utc_datetime(evaluated_at)
    if evaluated is None:
        raise ValueError("Fixed evaluation time is required")
    if state.evaluated_at is not None and evaluated < state.evaluated_at:
        return state, ()

    item = input_or_tick
    if state.lifecycle == "paused" and isinstance(item, (Observation, SavedAssessment)):
        return state, ()

    assessment = item if isinstance(item, SavedAssessment) else None
    if assessment is not None and assessment.assessment_id in state.seen_assessments:
        return state, ()
    updated = replace(state, evaluated_at=evaluated)
    if item == "stop":
        updated = replace(updated, lifecycle="stopped")
    elif item == "pause":
        updated = replace(updated, lifecycle="paused")
    elif item == "resume":
        updated = replace(updated, lifecycle="active")
    elif isinstance(item, Observation):
        if item.origin not in ("live", "synthetic") or item.result not in ("ok", "failed"):
            raise ValueError("Unknown observation origin or acquisition result")
        if item.origin != state.origin or item.identity in state.seen_observations:
            return state, ()
        source = to_utc_datetime(item.source_updated_at)
        fetched = to_utc_datetime(item.fetched_at)
        if source is not None:
            if item.result != "ok" or fetched is None:
                raise ValueError("A source clock requires a successful fetch and fetch time")
            if state.source_updated_at is not None and source < state.source_updated_at:
                return state, ()
            if evidence_clock_issue(source, fetched, evaluated) == "future_or_inconsistent_clock":
                return state, ()
            updated = replace(updated, source_updated_at=source, fetched_at=fetched)
        elif fetched is not None and fetched > evaluated:
            return state, ()
        updated = replace(updated, acquisition=item.result,
                          seen_observations=state.seen_observations | {item.identity})
        assessment = item.assessment
    elif item is not None and assessment is None:
        raise ValueError("Unknown monitoring input")

    events = []

    def emit(kind: EventType, basis, previous: str, current: str):
        key = sha256(canonical_json([state.session_id, kind, basis]).encode("utf-8")).hexdigest()
        events.append(MonitorEvent(key, state.session_id, kind, evaluated, previous, current))

    if isinstance(item, Observation) and updated.acquisition == "failed" and state.acquisition != "failed":
        emit("acquisition_failed", item.identity, state.acquisition, updated.acquisition)

    if updated.source_updated_at is not None:
        issue = evidence_clock_issue(updated.source_updated_at, updated.fetched_at, evaluated)
        freshness = "stale" if issue == "stale_source_time" else "fresh"
        updated = replace(updated, freshness=freshness)
        if state.freshness == "fresh" and freshness == "stale":
            emit("evidence_became_stale", updated.source_updated_at, state.freshness, freshness)
        if state.freshness == "stale" and freshness == "fresh":
            emit("evidence_recovered", updated.source_updated_at, state.freshness, freshness)

    if assessment is not None and assessment.assessment_id not in state.seen_assessments:
        if assessment.status not in STATUS_MAP.values():
            raise ValueError("Expected a saved assessment status")
        updated = replace(updated, risk=assessment.status,
                          seen_assessments=state.seen_assessments | {assessment.assessment_id})
        if state.risk is not None and state.risk != assessment.status:
            emit("risk_changed", assessment.assessment_id, state.risk, assessment.status)

    return updated, tuple(events)
