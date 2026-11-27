"""One bounded PostgreSQL scheduling pass with a side-effect-free offline provider."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Sequence

from .assessment import inspect_boarding_observation
from .monitor_state import Observation
from .monitor_store import MonitorConflict, MonitorError, MonitorSession, MonitorStore, _required_time
from .n1 import FrozenSnapshot, parse_strict_json


class AcquisitionUncertain(Exception):
    """An offline attempt has an unknown outcome. Spend its slot without retrying."""


@dataclass(frozen=True)
class Attempt:
    session_id: str
    number: int
    input_sequence: int
    input_identity: str
    evaluated_at: datetime


@dataclass(frozen=True)
class OfflineSample:
    """A delivery carries the attempt number originally assigned to that observation."""
    original_attempt: int
    observation: Observation


Provider = Callable[[Attempt], OfflineSample | None]


def timeline_provider(observations: Sequence[Observation | None]) -> Provider:
    """Build a restart-safe tape, indexed by reservation rather than process state."""
    first_attempt = {}
    samples = []
    for number, observation in enumerate(observations, 1):
        if observation is None:
            samples.append(None)
        else:
            original = first_attempt.setdefault(observation.identity, number)
            samples.append(OfflineSample(original, observation))

    def provide(attempt):
        return samples[attempt.number - 1] if attempt.number <= len(samples) else None

    return provide


def frozen_boarding_observation(snapshot: FrozenSnapshot, boarding_stop_uid: str) -> Observation:
    """Adapt a bundled or synthetic snapshot via the existing boarding quality gate."""
    rows = parse_strict_json(snapshot.records_json)
    matching = [(index, row) for index, row in enumerate(rows) if row["stop_uid"] == boarding_stop_uid]
    if len(matching) > 1:
        raise MonitorError("Offline adapter requires at most one boarding row per snapshot")
    if not matching:
        return Observation(snapshot.run_key, "synthetic", "ok", fetched_at=snapshot.fetched_at)

    index, row = matching[0]
    quality, _ = inspect_boarding_observation(
        dict(row, observation_id=index + 1, source_system="tdx_bus_n1", fetched_at=snapshot.fetched_at),
        route_uid=snapshot.scope.route_uid, direction=snapshot.scope.direction,
        boarding_stop_uid=boarding_stop_uid, evaluated_at=snapshot.fetched_at,
    )
    source = row["source_updated_at"] if quality in ("usable", "stale") else None
    return Observation(f"{snapshot.run_key}:{index}", "synthetic", "ok", source, snapshot.fetched_at)


@dataclass(frozen=True)
class ScheduledRun:
    session: MonitorSession
    next_due_at: datetime | None
    acquisitions_reserved: int
    outcome: str | None
    stop_reason: str | None


class MonitorScheduler:
    """Call explicitly. The optional provider must be local, deterministic and read-only."""

    def __init__(self, store: MonitorStore, provider: Provider | None = None):
        self._store = store
        self._provider = provider

    def schedule_session(self, session_id: str, *, deadline, next_due_at,
                         min_interval: int, max_acquisitions: int) -> None:
        """Initialize a session schedule once. Interval is in whole seconds."""
        deadline = _required_time(deadline)
        due = _required_time(next_due_at)
        if (type(min_interval) is not int or not 0 < min_interval <= 2_147_483_647
            or type(max_acquisitions) is not int or not 0 <= max_acquisitions <= 2_147_483_647
            or due > deadline):
            raise MonitorError("Invalid monitoring schedule bounds")
        store = self._store
        with store._journeys._connect() as connection, connection.transaction():
            session = store._read_session(connection, session_id, lock=True)
            if session.state.lifecycle == "stopped" or due < session.started_at:
                raise MonitorError("Cannot schedule this session in the requested window")
            result = connection.execute("""
                UPDATE public.monitor_sessions
                SET deadline = %s, next_due_at = %s, min_interval = %s, max_acquisitions = %s
                WHERE session_id = %s AND deadline IS NULL
            """, (deadline, due, min_interval, max_acquisitions, session_id))
            if result.rowcount != 1:
                raise MonitorConflict("Monitoring session already has a schedule")

    def run_once(self, now, *, session_id: str | None = None) -> ScheduledRun | None:
        """Handle at most one eligible session and commit before returning."""
        now = _required_time(now)
        store = self._store
        with store._journeys._connect() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            row = connection.execute("""
                SELECT session_id, deadline, min_interval, max_acquisitions, acquisitions_reserved, last_outcome
                FROM public.monitor_sessions
                WHERE lifecycle IN ('active', 'paused') AND deadline IS NOT NULL
                    AND (%s::text IS NULL OR session_id = %s)
                    AND (deadline <= %s OR (next_due_at <= %s AND evaluated_at <= %s))
                ORDER BY least(deadline, next_due_at), session_id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            """, (session_id, session_id, now, now, now)).fetchone()
            if row is None:
                return None
            session = store._read_session(connection, row["session_id"])
            result = self._run_locked(connection, session, row, now)
        return result

    def _run_locked(self, connection, session, row, now):
        reserved = row["acquisitions_reserved"]
        outcome = row["last_outcome"]
        reason = None
        due = None

        if now >= row["deadline"]:
            reason = "deadline"
            # Close at the deadline without running a late tick. Externally applied
            # inputs can already have advanced the session beyond that clock.
            session = self._apply(connection, session, "stop",
                                  max(row["deadline"], session.state.evaluated_at), reason)
        elif reserved == row["max_acquisitions"]:
            reason = "budget_exhausted"
            session = self._apply(connection, session, "stop", now, reason)
        else:
            session = self._apply(connection, session, None, now, "tick")
            outcome = "tick"
            if session.state.is_active and session.state.origin == "synthetic" and self._provider is not None:
                reserved += 1
                # A rollback may repeat this read-only provider call with the same
                # reservation. An external side effect cannot use this boundary.
                connection.execute("""
                    UPDATE public.monitor_sessions
                    SET acquisitions_reserved = %s WHERE session_id = %s
                """, (reserved, session.state.session_id))
                session, outcome = self._acquire(connection, session, reserved, now)
                if reserved == row["max_acquisitions"]:
                    reason = "budget_exhausted"
                    session = self._apply(connection, session, "stop", now, reason)
            if reason is None:
                due = now + timedelta(seconds=row["min_interval"])

        connection.execute("""
            UPDATE public.monitor_sessions
            SET next_due_at = %s, last_outcome = %s, stop_reason = %s
            WHERE session_id = %s
        """, (due, outcome, reason, session.state.session_id))
        return ScheduledRun(session, due, reserved, outcome, reason)

    def _acquire(self, connection, session, reserved, now):
        sequence = session.last_input_sequence + 1
        identity = f"scheduler:acquisition:{reserved}"
        attempt = Attempt(session.state.session_id, reserved, sequence, identity, now)
        try:
            sample = self._provider(attempt)
        except AcquisitionUncertain:
            observation = Observation(identity, "synthetic", "failed")
            outcome = "uncertain"
        else:
            if sample is None:
                observation = Observation(identity, "synthetic", "ok")
                outcome = "empty"
            elif sample.original_attempt < reserved:
                return self._apply(connection, session, None, now, "acquisition", identity), "duplicate"
            elif sample.original_attempt != reserved:
                return self._apply(connection, session, None, now, "acquisition", identity), "rejected"
            else:
                observation = sample.observation
                if observation.assessment is not None:
                    raise MonitorError("Scheduler observations cannot carry assessments")
                outcome = "failed" if observation.result == "failed" else "ok"

        updated = self._apply(connection, session, observation, now, "acquisition", identity)
        if updated.version == session.version:
            return self._apply(connection, session, None, now, "acquisition", identity), "rejected"
        return updated, outcome

    def _apply(self, connection, session, item, now, action, identity=None):
        sequence = session.last_input_sequence + 1
        return self._store._apply_locked(connection, session, item, now, sequence,
                                         identity or f"scheduler:{action}:{sequence}", session.version)
