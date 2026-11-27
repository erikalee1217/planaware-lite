"""Transactional PostgreSQL storage for the offline monitoring state machine."""
from dataclasses import dataclass, replace
from datetime import datetime

from .assessment import canonical_json, to_utc_datetime
from .journeys import JourneyStore
from .monitor_state import MonitorState, Observation, SavedAssessment, transition


class MonitorError(ValueError):
    """Invalid monitoring command or missing session."""


class MonitorConflict(MonitorError):
    """Version mismatch or conflicting input cursor."""


@dataclass(frozen=True)
class MonitorSession:
    state: MonitorState
    journey_id: int
    version: int
    started_at: datetime
    stopped_at: datetime | None
    last_input_sequence: int
    last_input_identity: str | None
    last_assessment_id: int | None


@dataclass(frozen=True)
class StoredMonitorEvent:
    event_id: str
    session_id: str
    kind: str
    basis: str
    previous: str
    current: str
    occurred_at: datetime
    processed_at: datetime


def _required_time(value):
    result = to_utc_datetime(value)
    if result is None:
        raise MonitorError("A fixed timestamp is required")
    return result


def _positive_bigint(value):
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


class MonitorStore:
    """Use the dedicated pilot connection policy and session-local row locks.

    The caller assigns stable, strictly increasing sequence numbers to distinct
    inputs. Retries carry their original sequence and identity.
    """

    def __init__(self, dsn: str):
        self._journeys = JourneyStore(dsn)

    @staticmethod
    def _read_session(connection, session_id, *, lock=False):
        query = "SELECT * FROM public.monitor_sessions WHERE session_id = %s"
        row = connection.execute(query + (" FOR UPDATE" if lock else ""), (session_id,)).fetchone()
        if row is None:
            raise MonitorError("Monitoring session not found")
        state = MonitorState(
            session_id=row["session_id"], origin=row["origin"], lifecycle=row["lifecycle"],
            acquisition=row["acquisition"], freshness=row["freshness"], risk=row["risk"],
            source_updated_at=to_utc_datetime(row["source_updated_at"]),
            fetched_at=to_utc_datetime(row["fetched_at"]),
            evaluated_at=to_utc_datetime(row["evaluated_at"]),
        )
        return MonitorSession(state, row["journey_id"], row["version"],
                              to_utc_datetime(row["started_at"]), to_utc_datetime(row["stopped_at"]),
                              row["last_input_sequence"], row["last_input_identity"], row["last_assessment_id"])

    def create_session(self, session_id, journey_id, origin, started_at):
        started = _required_time(started_at)
        if (not isinstance(session_id, str) or not session_id.strip() or "\x00" in session_id
            or not _positive_bigint(journey_id) or origin not in ("live", "synthetic")):
            raise MonitorError("Expected a session identity, journey identity and known origin")
        with self._journeys._connect() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            with connection.cursor() as cursor:
                self._journeys._lock_journey(cursor, journey_id)
            connection.execute("""
                INSERT INTO public.monitor_sessions (session_id, journey_id, origin, started_at, evaluated_at)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING
            """, (session_id, journey_id, origin, started, started))
            session = self._read_session(connection, session_id)
            if (session.journey_id, session.state.origin, session.started_at) != (journey_id, origin, started):
                raise MonitorConflict("Session identity already belongs to a different configuration")
            return session

    def get_session(self, session_id):
        with self._journeys._connect() as connection:
            return self._read_session(connection, session_id)

    def list_session_events(self, session_id):
        with self._journeys._connect() as connection:
            self._read_session(connection, session_id)
            rows = connection.execute("""
                SELECT event_id, session_id, kind, basis, previous, current, occurred_at, processed_at
                FROM public.monitor_events WHERE session_id = %s
                ORDER BY session_version, event_index
            """, (session_id,)).fetchall()
            return tuple(StoredMonitorEvent(**row) for row in rows)

    @staticmethod
    def _assessment_for(connection, session, item):
        assessment = item.assessment if isinstance(item, Observation) else item
        if not isinstance(assessment, SavedAssessment):
            return item, session.last_assessment_id
        identity = assessment.assessment_id
        if not _positive_bigint(identity):
            raise MonitorError("Expected a persisted assessment identity")
        row = connection.execute("""
            SELECT status FROM public.impact_assessments WHERE assessment_id = %s AND journey_id = %s
        """, (identity, session.journey_id)).fetchone()
        if row is None or row["status"] != assessment.status:
            raise MonitorError("Assessment does not match saved history for this journey")
        if session.last_assessment_id is not None and identity <= session.last_assessment_id:
            return (replace(item, assessment=None) if isinstance(item, Observation) else None,
                    session.last_assessment_id)
        return item, identity

    @staticmethod
    def _write_events(connection, events, updated, item, assessment_id, version):
        for index, event in enumerate(events):
            if event.kind == "acquisition_failed":
                basis = canonical_json(item.identity)
            elif event.kind == "risk_changed":
                basis = str(assessment_id)
            else:
                basis = updated.source_updated_at.isoformat()
            connection.execute("""
                INSERT INTO public.monitor_events
                    (event_id, session_id, session_version, event_index, kind, basis,
                     previous, current, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
            """, (event.event_id, event.session_id, version, index, event.kind, basis,
                  event.previous, event.current, event.evaluated_at))

    def apply_monitor_input(self, session_id, input_or_tick, evaluated_at, *,
                            input_sequence, input_identity, expected_version):
        if (not _positive_bigint(input_sequence) or type(expected_version) is not int
            or expected_version < 0 or not isinstance(input_identity, str)
            or not input_identity.strip() or "\x00" in input_identity):
            raise MonitorError("Expected a stable input cursor and session version")
        evaluated = _required_time(evaluated_at)

        with self._journeys._connect() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            session = self._read_session(connection, session_id, lock=True)
            return self._apply_locked(connection, session, input_or_tick, evaluated,
                                      input_sequence, input_identity, expected_version)

    def _apply_locked(self, connection, session, input_or_tick, evaluated,
                      input_sequence, input_identity, expected_version):
        """Apply one input using the caller's transaction and locked session row."""
        session_id = session.state.session_id
        state = session.state
        if state.lifecycle == "stopped" or input_sequence < session.last_input_sequence:
            return session
        if input_sequence == session.last_input_sequence:
            if input_identity == session.last_input_identity:
                return session
            raise MonitorConflict("Input sequence already belongs to a different identity")
        if input_identity == session.last_input_identity:
            raise MonitorConflict("Retry must preserve the original input sequence")
        if expected_version != session.version:
            raise MonitorConflict("Monitoring session version changed")

        item, assessment_id = self._assessment_for(connection, session, input_or_tick)
        if isinstance(input_or_tick, SavedAssessment) and item is None:
            return session
        updated, events = transition(state, item, evaluated)
        if updated is state:
            return session

        version = session.version + 1
        connection.execute("""
            UPDATE public.monitor_sessions
            SET lifecycle = %s, acquisition = %s, freshness = %s, risk = %s,
                source_updated_at = %s, fetched_at = %s, evaluated_at = %s, stopped_at = %s,
                last_input_sequence = %s, last_input_identity = %s, last_assessment_id = %s, version = %s
            WHERE session_id = %s
        """, (updated.lifecycle, updated.acquisition, updated.freshness, updated.risk,
              updated.source_updated_at, updated.fetched_at, updated.evaluated_at,
              evaluated if updated.lifecycle == "stopped" else None,
              input_sequence, input_identity, assessment_id, version, session_id))
        self._write_events(connection, events, updated, item, assessment_id, version)
        return self._read_session(connection, session_id)

    def stop_session(self, session_id, evaluated_at, *, input_sequence, input_identity, expected_version):
        return self.apply_monitor_input(session_id, "stop", evaluated_at, input_sequence=input_sequence,
                                        input_identity=input_identity, expected_version=expected_version)
