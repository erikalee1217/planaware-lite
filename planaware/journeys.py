"""Manage Taipei 212 pilot drafts in a dedicated PostgreSQL database."""

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from importlib.resources import files
import json
import logging
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .assessment import (
    AssessmentError, AssessmentSpec, build_assessment, canonical_json,
    to_utc_datetime, validate_assessment_spec,
)
from .catalog import CatalogError, load_catalog
from .diagnostics import log_failure
from .n1 import FrozenSnapshot, parse_strict_json


logger = logging.getLogger(__name__)


class JourneyError(ValueError):
    """A draft, journey lookup, or pilot database operation failed."""


@dataclass(frozen=True)
class Walk:
    duration_seconds: int
    source: str
    origin: str
    destination: str


@dataclass(frozen=True)
class TrainDeparture:
    train_number: str
    departure_at: datetime
    source: str


@dataclass(frozen=True)
class BoardingAllowance:
    minutes: int
    source: str


@dataclass(frozen=True)
class JourneyDraft:
    name: str
    service_date: date
    route_uid: str
    subroute_uid: str
    direction: int
    boarding: tuple[str, int]
    alighting: tuple[str, int]
    walk: Walk | None = None
    train: TrainDeparture | None = None
    allowance: BoardingAllowance | None = None
    confirm_service_date_assumptions: bool = False


@dataclass(frozen=True)
class Journey:
    journey_id: int
    name: str
    service_date: date
    legs: tuple[dict[str, Any], ...]
    transfer: dict[str, Any] | None
    has_assessments: bool
    edit_token: str


@dataclass(frozen=True)
class JourneySave:
    journey: Journey
    outcome: Literal["updated", "cloned", "unchanged"]
    source_journey_id: int | None = None

    @property
    def journey_id(self) -> int:
        return self.journey.journey_id

    @property
    def edit_token(self) -> str:
        return self.journey.edit_token


def _validate_id(value, label):
    if type(value) is not int or not 0 < value <= 9_223_372_036_854_775_807:
        raise JourneyError(f"{label} ID must be a positive PostgreSQL bigint")


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise JourneyError(f"{field} must be nonblank text")
    return value.strip()


def _integer(value, minimum, field):
    if type(value) is not int or not minimum <= value <= 2_147_483_647:
        raise JourneyError(f"{field} must be an integer of at least {minimum} within PostgreSQL integer range")
    return value


def _draft_values(draft):
    name = _text(draft.name, "Journey name")
    if type(draft.service_date) is not date:
        raise JourneyError("Service date must be a date")
    catalog = load_catalog()
    for selection in (draft.boarding, draft.alighting):
        if not isinstance(selection, tuple) or len(selection) != 2:
            raise JourneyError("Select each stop by UID and sequence")
    try:
        route = catalog.lookup_route(draft.route_uid, draft.subroute_uid, draft.direction)
        boarding, alighting = route.validate_stop_pair(draft.boarding, draft.alighting)
    except CatalogError as error:
        raise JourneyError(str(error)) from None

    walk_seconds = walk_source = departure = None
    walk_reference = {}
    train_reference = {"operator": "THSR", "station": "Nangang", "direction": "southbound"}
    if draft.walk is not None:
        walk_seconds = _integer(draft.walk.duration_seconds, 1, "Walk duration")
        walk_source = _text(draft.walk.source, "Walk source")
        walk_reference = {
            "origin_name": _text(draft.walk.origin, "Walk origin"),
            "destination_name": _text(draft.walk.destination, "Walk destination"),
        }
    if draft.train is not None:
        departure = draft.train.departure_at
        if not isinstance(departure, datetime) or departure.utcoffset() is None:
            raise JourneyError("Train departure must be an offset-aware datetime")
        if departure.astimezone(ZoneInfo("Asia/Taipei")).date() != draft.service_date:
            raise JourneyError("Train departure must match the Taipei service date")
        train_reference.update(
            train_number=_text(draft.train.train_number, "Train number"),
            timetable_source=_text(draft.train.source, "Timetable source"),
            timetable_service_date=draft.service_date.isoformat(),
        )

    allowance = None
    if draft.allowance is not None:
        allowance = (
            _integer(draft.allowance.minutes, 0, "Boarding allowance"),
            _text(draft.allowance.source, "Boarding allowance source"),
        )
    bus_reference = {
        "catalog_version": catalog.version, "catalog_coverage": catalog.coverage,
        "catalog_source": catalog.source, "route_id": route.route_id,
        "subroute_uid": route.subroute_uid, "subroute_id": route.subroute_id,
        "route_name": route.name, "headsign": route.headsign,
        "origin_sequence": boarding.sequence, "destination_sequence": alighting.sequence,
        "origin_name": boarding.name, "destination_name": alighting.name,
    }
    if draft.confirm_service_date_assumptions:
        if walk_seconds is None or departure is None or allowance is None:
            raise JourneyError("Add walk, train, and boarding allowance details before confirming assumptions")
        service_day = draft.service_date.isoformat()
        revision = sha256(canonical_json({
            "service_date": service_day, "bus": bus_reference, "walk": walk_reference,
            "walk_seconds": walk_seconds, "walk_source": walk_source, "train": train_reference,
            "departure": departure, "allowance": allowance,
        }).encode()).hexdigest()
        timing_reference = {
            "reference_data_version": f"pilot-reference-{revision}",
            "assumption_version": f"synthetic-pilot-{revision}",
            "valid_from": service_day, "valid_to": service_day,
            "timing_basis": "pilot_service_date_assumptions",
        }
        walk_reference.update(timing_reference)
        train_reference.update(timing_reference)
    legs = (
        ("bus", route.route_uid, route.direction, boarding.stop_uid, alighting.stop_uid,
         None, None, None, bus_reference),
        ("walking", None, None, None, None, None, walk_seconds, walk_source, walk_reference),
        ("high_speed_rail", None, None, None, None, departure, None, None, train_reference),
    )
    return name, legs, allowance


def _edit_token(row, transfer):
    legs = [{key: value for key, value in leg.items() if key not in ("leg_id", "journey_id")}
            for leg in row["legs"]]
    if transfer:
        order_by_id = {leg["leg_id"]: leg["leg_order"] for leg in row["legs"]}
        transfer = {
            "from_order": order_by_id[transfer["from_leg_id"]],
            "to_order": order_by_id[transfer["to_leg_id"]],
            "min_transfer_minutes": transfer["min_transfer_minutes"],
            "assumption_source": transfer["assumption_source"],
        }
    editable = dict(name=row["journey_name"], service_date=row["service_date"].isoformat(),
                    target_arrival_at=row["target_arrival_at"], legs=legs, transfer=transfer)
    return sha256(json.dumps(editable, sort_keys=True).encode()).hexdigest()


def _matches_draft(current, name, service_date, legs, allowance):
    if (current.name, current.service_date) != (name, service_date):
        return False
    for saved, proposed in zip(current.legs, legs):
        departure = saved["planned_departure_at"]
        values = (
            saved["transport_mode"], saved["route_uid"], saved["direction"],
            saved["origin_stop_uid"], saved["destination_stop_uid"],
            datetime.fromisoformat(departure) if departure else None,
            saved["estimated_duration_seconds"], saved["duration_assumption_source"],
            saved["reference_details"],
        )
        if values != proposed:
            return False
    transfer = current.transfer
    saved_allowance = (transfer["min_transfer_minutes"], transfer["assumption_source"]) if transfer else None
    return saved_allowance == allowance


def validate_pilot_snapshot_scope(snapshot: FrozenSnapshot):
    """Require a frozen N1 snapshot scoped to the pilot boarding corridor."""
    if not isinstance(snapshot, FrozenSnapshot):
        raise JourneyError("Expected a validated frozen N1 snapshot")
    scope = snapshot.scope
    route = load_catalog().route
    boarding, alighting = route.stops
    if ((scope.route_uid, scope.direction) != (route.route_uid, route.direction)
        or boarding.stop_uid not in scope.stop_uids
        or not set(scope.stop_uids) <= {boarding.stop_uid, alighting.stop_uid}):
        raise JourneyError("Snapshot scope does not match the pilot boarding corridor")


def load_synthetic_212_snapshot() -> FrozenSnapshot:
    """Load the bundled historical-clock test fixture. No TDX requests are made."""
    resource = files("planaware").joinpath("catalogs", "synthetic-212-n1-v1.json")
    snapshot = FrozenSnapshot.from_json(resource.read_text(encoding="utf-8"))
    validate_pilot_snapshot_scope(snapshot)
    return snapshot


def observation_assessment_spec(
    journey: Journey, observation: dict[str, Any], *, evaluated_at: datetime | None = None,
) -> AssessmentSpec:
    """Assess boarding-stop evidence without supplying a downstream arrival interval."""
    if tuple(leg["transport_mode"] for leg in journey.legs) != ("bus", "walking", "high_speed_rail"):
        raise JourneyError("Journey is outside the assessed pilot shape")
    bus, walk, train = journey.legs
    transfer = journey.transfer
    if not transfer or (transfer["from_leg_id"], transfer["to_leg_id"]) != (walk["leg_id"], train["leg_id"]):
        raise JourneyError("Save the walk-to-THSR transfer before assessing")
    stop_uid = bus["origin_stop_uid"]
    if (observation.get("source_system"), observation.get("route_uid"), observation.get("direction"),
        observation.get("stop_uid")) != ("tdx_bus_n1", bus["route_uid"], bus["direction"], stop_uid):
        raise JourneyError("Observation does not match the journey boarding stop")
    observation_id = observation.get("observation_id")
    _validate_id(observation_id, "Observation")

    walk_ref, train_ref = walk["reference_details"], train["reference_details"]
    reference_version = walk_ref.get("reference_data_version")
    assumption_version = walk_ref.get("assumption_version")
    if (not reference_version or not assumption_version or
        (reference_version, assumption_version) !=
        (train_ref.get("reference_data_version"), train_ref.get("assumption_version"))):
        raise JourneyError("Save matching timing reference and assumption versions before assessing")
    evaluation = to_utc_datetime(observation.get("fetched_at") if evaluated_at is None else evaluated_at)
    if evaluation is None:
        raise JourneyError("Historical evaluation requires the observation fetch timestamp")
    return AssessmentSpec(
        journey_id=journey.journey_id,
        transfer_id=transfer["transfer_id"],
        evaluated_at=evaluation,
        reference_version=reference_version,
        assumption_version=assumption_version,
        scenario="source_backed",
        buffer_semantics="boarding_only",
        observation_ids=(observation_id,),
        evidence_leg_id=bus["leg_id"],
        evidence_stop_uid=stop_uid,
    )


class JourneyStore:
    """Open only an explicitly configured dedicated pilot database.

    Concurrent legacy CLI assessment and draft edits are unsupported.
    Pilot assessment writers must coordinate on the journey lock before reading context.
    """

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise JourneyError("An explicit pilot database connection is required")
        self._dsn = dsn

    def _connect(self):
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
        except ImportError as error:
            log_failure(logger, logging.DEBUG, "pilot_db.dependency_missing", error)
            raise JourneyError("Install the journey optional dependency to use PostgreSQL") from None
        try:
            parameters = conninfo_to_dict(self._dsn)
            database = parameters.get("dbname", "")
            if not (database == "planaware_pilot" or
                    (database.startswith("planaware_pilot_") and len(database) > len("planaware_pilot_"))):
                raise JourneyError("Configure an explicit planaware_pilot database name")
            if not parameters.get("host"):
                raise JourneyError("Configure an explicit pilot database host or socket directory")
            return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True, connect_timeout=5)
        except psycopg.Error as error:
            log_failure(logger, logging.WARNING, "pilot_db.connect_failed", error)
            raise JourneyError("Cannot connect to the pilot database") from None

    @staticmethod
    def _read(connection, journey_id):
        row = connection.execute("""
            SELECT j.journey_id, j.journey_name, j.service_date,
                   to_jsonb(j.target_arrival_at) AS target_arrival_at,
                   COALESCE((SELECT jsonb_agg(to_jsonb(l) ORDER BY l.leg_order)
                             FROM public.journey_legs l WHERE l.journey_id = j.journey_id), '[]') AS legs,
                   ARRAY(SELECT to_jsonb(t) FROM public.transfer_constraints t
                         WHERE t.journey_id = j.journey_id LIMIT 2) AS transfers,
                   EXISTS (SELECT 1 FROM public.impact_assessments a
                           WHERE a.journey_id = j.journey_id) AS has_assessments
            FROM public.journeys j WHERE j.journey_id = %s
        """, (journey_id,)).fetchone()
        if row is None:
            raise JourneyError("Journey not found")
        transfers = row["transfers"]
        if len(transfers) > 1:
            raise JourneyError("Journey has an unsupported transfer")
        transfer = transfers[0] if transfers else None
        return Journey(row["journey_id"], row["journey_name"], row["service_date"],
                       tuple(row["legs"]), transfer, row["has_assessments"], _edit_token(row, transfer))

    @staticmethod
    def _insert_transfer(connection, journey_id, from_leg_id, to_leg_id, allowance):
        connection.execute("""
            INSERT INTO public.transfer_constraints
                (journey_id, from_leg_id, to_leg_id, min_transfer_minutes, assumption_source)
            VALUES (%s, %s, %s, %s, %s)
        """, (journey_id, from_leg_id, to_leg_id, *allowance))

    def _insert_draft(self, connection, name, service_date, legs, allowance):
        from psycopg.types.json import Jsonb

        journey_id = connection.execute("""
            INSERT INTO public.journeys (journey_name, service_date)
            VALUES (%s, %s) RETURNING journey_id
        """, (name, service_date)).fetchone()["journey_id"]
        leg_ids = []
        for order, (*leg, reference) in enumerate(legs, 1):
            row = connection.execute("""
                INSERT INTO public.journey_legs (
                    journey_id, leg_order, transport_mode, route_uid, direction,
                    origin_stop_uid, destination_stop_uid, planned_departure_at,
                    estimated_duration_seconds, duration_assumption_source, reference_details
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING leg_id
            """, (journey_id, order, *leg, Jsonb(reference))).fetchone()
            leg_ids.append(row["leg_id"])
        if allowance is not None:
            self._insert_transfer(connection, journey_id, leg_ids[1], leg_ids[2], allowance)
        return self._read(connection, journey_id)

    def create_draft(self, draft: JourneyDraft) -> Journey:
        name, legs, allowance = _draft_values(draft)
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction():
                    connection.execute("SET LOCAL TIME ZONE 'UTC'")
                    return self._insert_draft(connection, name, draft.service_date, legs, allowance)
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.create_draft_failed", error)
                raise JourneyError("Could not create the journey draft") from None

    def get_journey(self, journey_id: int) -> Journey:
        _validate_id(journey_id, "Journey")
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction():
                    connection.execute("SET LOCAL TIME ZONE 'UTC'")
                    return self._read(connection, journey_id)
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.get_journey_failed", error)
                raise JourneyError("Could not read the pilot journey") from None

    def execute_snapshot_sql(self, statement: str) -> str:
        """Execute only SQL generated by FrozenSnapshot.to_sql."""
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    row = connection.execute(statement).fetchone()
                    return str(next(iter(row.values())))
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.execute_snapshot_sql_failed", error)
                raise JourneyError("Could not persist the frozen snapshot") from None

    def import_snapshot(self, snapshot: FrozenSnapshot) -> int:
        """Commit a validated pilot snapshot and return its existing or new run ID."""
        validate_pilot_snapshot_scope(snapshot)
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    row = connection.execute("""
                        SELECT public.persist_tdx_n1_snapshot(
                            %s, %s::timestamptz, %s::timestamptz, %s::jsonb
                        ) AS run_id
                    """, (snapshot.run_key, snapshot.requested_at, snapshot.fetched_at,
                          snapshot.records_json)).fetchone()
                    return row["run_id"]
            except Error as error:
                if error.sqlstate == "23514" and error.diag.constraint_name == "ingestion_snapshot_conflict":
                    log_failure(logger, logging.DEBUG, "pilot_db.import_snapshot_conflict", error)
                    raise JourneyError("Run key already belongs to a different frozen snapshot") from None
                log_failure(logger, logging.WARNING, "pilot_db.import_snapshot_failed", error)
                raise JourneyError("Could not import the frozen snapshot; retry the identical artifact "
                                   "if the commit outcome is unknown") from None

    def list_run_observations(
        self, run_id: int, route_uid: str, direction: int, stop_uid: str,
    ) -> list[dict[str, Any]]:
        """Read matching persisted observations in snapshot order."""
        _validate_id(run_id, "Ingestion run")
        with self._connect() as connection:
            from psycopg import Error

            try:
                rows = connection.execute("""
                    SELECT row_to_json(o)::text AS observation
                    FROM public.source_observations o
                    WHERE o.run_id = %s AND o.route_uid = %s AND o.direction = %s AND o.stop_uid = %s
                    ORDER BY o.record_index
                """, (run_id, route_uid, direction, stop_uid)).fetchall()
                return [parse_strict_json(row["observation"]) for row in rows]
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.list_run_observations_failed", error)
                raise JourneyError("Could not read observations for the saved run") from None

    def list_journeys(self, limit: int = 50) -> list[dict[str, Any]]:
        """Read the most recently created journeys."""
        limit = _integer(limit, 1, "Journey list limit")
        with self._connect() as connection:
            from psycopg import Error

            try:
                return connection.execute("""
                    SELECT j.journey_id, j.journey_name, j.service_date,
                           EXISTS (SELECT 1 FROM public.impact_assessments a
                                   WHERE a.journey_id = j.journey_id) AS has_assessments
                    FROM public.journeys j
                    ORDER BY j.created_at DESC, j.journey_id DESC
                    LIMIT %s
                """, (limit,)).fetchall()
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.list_journeys_failed", error)
                raise JourneyError("Could not load saved journeys") from None

    def list_assessments(self, journey_id: int) -> list[dict[str, Any]]:
        """Read saved assessment rows in reverse evaluation order."""
        _validate_id(journey_id, "Journey")
        with self._connect() as connection:
            from psycopg import Error

            try:
                rows = connection.execute("""
                    SELECT row_to_json(a)::text AS assessment
                    FROM public.impact_assessments a
                    WHERE a.journey_id = %s
                    ORDER BY a.evaluated_at DESC, a.assessment_id DESC
                """, (journey_id,)).fetchall()
                return [parse_strict_json(row["assessment"]) for row in rows]
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.list_assessments_failed", error)
                raise JourneyError("Could not read saved assessments") from None

    @staticmethod
    def _lock_journey(cursor, journey_id):
        cursor.execute("""
            SELECT 1 FROM public.journeys WHERE journey_id = %s FOR UPDATE
        """, (journey_id,))
        if cursor.fetchone() is None:
            raise JourneyError("Journey not found")

    def assess_journey(self, spec: AssessmentSpec) -> dict[str, int | str]:
        """Assess and persist the same locked version of a pilot journey."""
        validate_assessment_spec(spec)
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    cursor.execute("SET LOCAL TIME ZONE 'UTC'")
                    self._lock_journey(cursor, spec.journey_id)
                    cursor.execute("""
                        SELECT json_build_object(
                            'journey', (SELECT row_to_json(j) FROM public.journeys j
                                        WHERE j.journey_id = %s),
                            'transfer', (SELECT row_to_json(t) FROM public.transfer_constraints t
                                         WHERE t.journey_id = %s AND t.transfer_id = %s),
                            'legs', COALESCE((SELECT json_agg(l ORDER BY l.leg_order)
                                              FROM public.journey_legs l WHERE l.journey_id = %s), '[]'),
                            'observations', COALESCE((SELECT json_agg(o ORDER BY o.observation_id)
                                                      FROM public.source_observations o
                                                      WHERE o.observation_id = ANY(%s::bigint[])), '[]')
                        )::text AS context
                    """, (spec.journey_id, spec.journey_id, spec.transfer_id,
                          spec.journey_id, list(spec.observation_ids)))
                    # Decode with the same decimal-preserving reader as the CLI.
                    context = parse_strict_json(cursor.fetchone()["context"])
                    record = build_assessment(spec, context)
                    cursor.execute("SELECT public.persist_impact_assessment(%s::jsonb) AS assessment_id",
                                   (canonical_json(record),))
                    return {
                        "assessment_id": cursor.fetchone()["assessment_id"],
                        "status": record["status"],
                        "reason_code": record["reason_code"],
                        "assessment_key": record["assessment_key"],
                    }
            except Error as error:
                if error.sqlstate == "23514" and error.diag.constraint_name == "impact_assessment_conflict":
                    log_failure(logger, logging.DEBUG, "pilot_db.assess_journey_conflict", error)
                    raise AssessmentError("Assessment key already belongs to a different record") from None
                log_failure(logger, logging.WARNING, "pilot_db.assess_journey_failed", error)
                raise AssessmentError("Could not assess the pilot journey; retry with identical inputs "
                                      "if the commit outcome is unknown") from None

    def save_journey(
        self, journey_id: int, updated_draft: JourneyDraft, expected_edit_token: str
    ) -> JourneySave:
        """Save a pilot draft or create an independent draft from an assessed journey."""
        _validate_id(journey_id, "Journey")
        name, legs, allowance = _draft_values(updated_draft)
        service_date = updated_draft.service_date
        with self._connect() as connection:
            from psycopg import Error

            try:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    connection.execute("SET LOCAL TIME ZONE 'UTC'")
                    with connection.cursor() as cursor:
                        self._lock_journey(cursor, journey_id)
                    current = self._read(connection, journey_id)
                    if current.edit_token != expected_edit_token:
                        raise JourneyError("Stale journey edit; reload before saving")
                    if [(leg["leg_order"], leg["transport_mode"]) for leg in current.legs] != [
                        (1, "bus"), (2, "walking"), (3, "high_speed_rail")
                    ] or current.legs[0]["reference_details"].get("catalog_coverage") != "partial_pilot_corridor":
                        raise JourneyError("Journey is outside the editable pilot shape")
                    transfer = current.transfer
                    if transfer and (transfer["from_leg_id"], transfer["to_leg_id"]) != (
                        current.legs[1]["leg_id"], current.legs[2]["leg_id"]
                    ):
                        raise JourneyError("Journey has an unsupported transfer")
                    if _matches_draft(current, name, service_date, legs, allowance):
                        return JourneySave(current, "unchanged")
                    if current.has_assessments:
                        cloned = self._insert_draft(connection, name, service_date, legs, allowance)
                        return JourneySave(cloned, "cloned", source_journey_id=journey_id)
                    self._update_draft(connection, current, name, service_date, legs, allowance)
                    return JourneySave(self._read(connection, journey_id), "updated")
            except Error as error:
                log_failure(logger, logging.WARNING, "pilot_db.save_journey_failed", error)
                raise JourneyError("Could not save the journey draft") from None

    def _update_draft(self, connection, current, name, service_date, legs, allowance):
        from psycopg.types.json import Jsonb

        journey_id = current.journey_id
        connection.execute("""
            UPDATE public.journeys SET journey_name = %s, service_date = %s WHERE journey_id = %s
        """, (name, service_date, journey_id))
        for saved, (*leg, reference) in zip(current.legs, legs):
            connection.execute("""
                UPDATE public.journey_legs SET
                    transport_mode = %s, route_uid = %s, direction = %s,
                    origin_stop_uid = %s, destination_stop_uid = %s, planned_departure_at = %s,
                    estimated_duration_seconds = %s, duration_assumption_source = %s, reference_details = %s
                WHERE journey_id = %s AND leg_id = %s
            """, (*leg, Jsonb(reference), journey_id, saved["leg_id"]))
        transfer = current.transfer
        if transfer is None:
            if allowance is not None:
                self._insert_transfer(connection, journey_id, current.legs[1]["leg_id"],
                                      current.legs[2]["leg_id"], allowance)
        elif allowance is None:
            connection.execute("""
                DELETE FROM public.transfer_constraints WHERE journey_id = %s AND transfer_id = %s
            """, (journey_id, transfer["transfer_id"]))
        elif allowance != (transfer["min_transfer_minutes"], transfer["assumption_source"]):
            connection.execute("""
                UPDATE public.transfer_constraints SET min_transfer_minutes = %s, assumption_source = %s
                WHERE journey_id = %s AND transfer_id = %s
            """, (*allowance, journey_id, transfer["transfer_id"]))
