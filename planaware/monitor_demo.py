"""Local pilot service for the fixed, manually stepped monitoring demonstration."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .assessment import to_utc_datetime
from .journeys import JourneyStore
from .monitor_scheduler import MonitorScheduler, timeline_provider
from .monitor_state import Observation
from .monitor_store import MonitorError, MonitorSession, MonitorStore, _positive_bigint
from .ui_helpers import TAIPEI


def local_pilot_dsn(dsn):
    """Require an explicit local endpoint and keep JourneyStore's pilot-name guard."""
    from psycopg import ProgrammingError
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        params = conninfo_to_dict(dsn)
    except ProgrammingError:
        raise MonitorError("Configure a valid local pilot database connection.") from None
    host = params.get("host", "")
    loopback = host in ("127.0.0.1", "::1")
    if (not (loopback or host.startswith("/")) or "," in host or params.get("service")
        or params.get("hostaddr", "") not in ("", host if loopback else "")):
        raise MonitorError("Monitoring requires an explicit loopback address or local PostgreSQL socket.")
    # Pin hostaddr so an inherited PGHOSTADDR cannot override the approved host.
    return make_conninfo(dsn, hostaddr=host if loopback else "")


@dataclass(frozen=True)
class DemoSession:
    session: MonitorSession
    next_due_at: datetime | None
    stop_reason: str | None = None
    last_outcome: str | None = None
    acquisitions_remaining: int | None = None

    @property
    def can_step(self):
        return self.session.state.is_active and self.next_due_at is not None


class MonitoringDemo:
    def __init__(self, dsn):
        dsn = local_pilot_dsn(dsn)
        self._journeys = JourneyStore(dsn)
        self._store = MonitorStore(dsn)

    @staticmethod
    def _session_id(journey_id):
        if not _positive_bigint(journey_id):
            raise MonitorError("Expected a saved journey.")
        return f"offline-demo:{journey_id}"

    def list_journeys(self):
        return self._journeys.list_journeys()

    def latest_assessment(self, journey_id):
        assessments = self._journeys.list_assessments(journey_id)
        return assessments[0] if assessments else None

    def _rows(self, connection, journey_id, *, session_id=None, latest_only=False):
        base = self._session_id(journey_id)
        return connection.execute("""
            SELECT session_id, lifecycle, next_due_at, stop_reason, last_outcome,
                   max_acquisitions - acquisitions_reserved AS acquisitions_remaining
            FROM public.monitor_sessions
            WHERE journey_id = %s AND origin = 'synthetic'
                AND (session_id = %s OR session_id LIKE %s)
                AND (%s::text IS NULL OR session_id = %s)
            ORDER BY session_id COLLATE "C" DESC
            LIMIT %s
        """, (journey_id, base, base + ":%", session_id, session_id, 1 if latest_only else None)).fetchall()

    def sessions(self, journey_id):
        with self._journeys._connect() as connection:
            return tuple((row["session_id"], row["lifecycle"])
                         for row in self._rows(connection, journey_id))

    def _load(self, connection, journey_id, session_id=None):
        rows = self._rows(connection, journey_id, session_id=session_id, latest_only=True)
        if not rows:
            return None
        row = rows[0]
        return DemoSession(self._store._read_session(connection, row["session_id"]),
                           to_utc_datetime(row["next_due_at"]), row["stop_reason"],
                           row["last_outcome"], row["acquisitions_remaining"])

    def load(self, journey_id, session_id=None):
        with self._journeys._connect() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return self._load(connection, journey_id, session_id)

    def create(self, journey_id, *, after=None):
        base = self._session_id(journey_id)
        with self._journeys._connect() as connection, connection.transaction(), connection.cursor() as cursor:
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self._journeys._lock_journey(cursor, journey_id)
            current = self._load(connection, journey_id)
            if current is not None:
                identity = current.session.state.session_id
                if current.session.state.lifecycle != "stopped" or after != identity:
                    return current
                ordinal = 2 if identity == base else int(identity.rsplit(":", 1)[-1]) + 1
                identity = f"{base}:{ordinal:020d}"
            elif after is None:
                identity = base
            else:
                raise MonitorError("The preceding demo session could not be found.")
            journey = self._journeys._read(connection, journey_id)
            start = datetime.combine(journey.service_date, time(9), TAIPEI)
            connection.execute("""
                INSERT INTO public.monitor_sessions
                    (session_id, journey_id, origin, started_at, evaluated_at,
                     deadline, next_due_at, min_interval, max_acquisitions)
                VALUES (%s, %s, 'synthetic', %s, %s, %s, %s, 60, 6)
            """, (identity, journey_id, start, start, start + timedelta(minutes=6), start))
            return self._load(connection, journey_id, identity)

    def events(self, demo):
        return self._store.list_session_events(demo.session.state.session_id)

    def step(self, demo):
        if not demo.can_step:
            raise MonitorError("This monitoring session cannot run another step.")
        start = demo.session.started_at
        observations = (
            Observation("demo-first", "synthetic", "ok", start, start),
            Observation("demo-same-clock", "synthetic", "ok", start, start + timedelta(minutes=1)),
            None,
            Observation("demo-provider-failure", "synthetic", "failed"),
            Observation("demo-recovered", "synthetic", "ok",
                        start + timedelta(minutes=4), start + timedelta(minutes=4)),
        )
        return MonitorScheduler(self._store, timeline_provider(observations)).run_once(
            demo.next_due_at, session_id=demo.session.state.session_id,
        )

    def stop(self, demo):
        prior = demo.session
        identity = prior.state.session_id
        with self._journeys._connect() as connection, connection.transaction():
            session = self._store._read_session(connection, identity, lock=True)
            if session.state.lifecycle == "stopped":
                return session
            sequence = prior.last_input_sequence + 1
            updated = self._store._apply_locked(
                connection, session, "stop", prior.state.evaluated_at, sequence,
                f"offline-demo:stop:{sequence}", prior.version,
            )
            if updated.version != session.version:
                connection.execute("""
                    UPDATE public.monitor_sessions SET stop_reason = 'user_stopped', next_due_at = NULL
                    WHERE session_id = %s
                """, (identity,))
            return updated
