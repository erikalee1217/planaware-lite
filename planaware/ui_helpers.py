"""Translate Journey form values without importing Streamlit or opening a database."""

from datetime import date, datetime, time
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .assessment import AssessmentSpec, _evidence, _time
from .brief import ISSUES, BriefError, build_brief
from .catalog import RouteVariant
from .journeys import BoardingAllowance, Journey, JourneyDraft, JourneyError, TrainDeparture, Walk
from .n1 import STATUS_LABELS

TAIPEI = ZoneInfo("Asia/Taipei")
ASSUMPTION_SOURCE = "User-entered assumption"
WALK_START = "南港行政中心（南港車站）"
WALK_END = "THSR Nangang southbound gate"
ARRIVAL_SOURCE = "User-entered arrival estimate"


def _optional_integer(value, label):
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        raise JourneyError(f"{label} must be a whole number.") from None


def _taipei_datetime(day, value, label):
    try:
        if ":" not in value:
            raise ValueError
        local_time = time.fromisoformat(value.strip())
        if local_time.tzinfo is not None:
            raise ValueError
    except ValueError:
        raise JourneyError(f"Enter {label} as HH:MM in Taipei time.") from None
    return datetime.combine(day, local_time, TAIPEI)


def draft_from_form(values, route: RouteVariant) -> JourneyDraft:
    boarding, alighting = route.stops
    day = values["service_date"]
    walk = None
    if values["walk_minutes"].strip():
        try:
            seconds = (Decimal(values["walk_minutes"]) * 60).to_integral_value(rounding=ROUND_HALF_UP)
            if not 0 < seconds <= 2_147_483_647:
                raise ValueError
        except (DecimalException, ValueError):
            raise JourneyError("Enter a positive walking time in minutes.") from None
        walk = Walk(int(seconds), values["walk_source"].strip() or ASSUMPTION_SOURCE,
                    values["walk_origin"].strip() or WALK_START,
                    values["walk_destination"].strip() or WALK_END)

    train = None
    train_number, train_time = values["train_number"].strip(), values["train_time"].strip()
    if train_number or train_time:
        if not train_number:
            raise JourneyError("Enter the THSR train number, or clear the departure time.")
        train = TrainDeparture(train_number, _taipei_datetime(day, train_time, "the THSR departure time"),
                               values["train_source"].strip() or ASSUMPTION_SOURCE)

    minutes = _optional_integer(values["allowance_minutes"], "Boarding buffer")
    allowance = None if minutes is None else BoardingAllowance(
        minutes, values["allowance_source"].strip() or ASSUMPTION_SOURCE,
    )
    return JourneyDraft(
        values["name"], day, route.route_uid, route.subroute_uid,
        route.direction, (boarding.stop_uid, boarding.sequence),
        (alighting.stop_uid, alighting.sequence), walk, train, allowance,
        confirm_service_date_assumptions=all(value is not None for value in (walk, train, allowance)),
    )


def form_from_journey(journey: Journey, route: RouteVariant) -> dict:
    if tuple(leg["transport_mode"] for leg in journey.legs) != ("bus", "walking", "high_speed_rail"):
        raise JourneyError("This journey is outside the supported pilot corridor.")
    bus, walk, train = journey.legs
    boarding, alighting = route.stops
    reference = bus["reference_details"]
    if (bus["route_uid"], reference.get("subroute_uid"), bus["direction"],
        bus["origin_stop_uid"], reference.get("origin_sequence"),
        bus["destination_stop_uid"], reference.get("destination_sequence")) != (
        route.route_uid, route.subroute_uid, route.direction,
        boarding.stop_uid, boarding.sequence, alighting.stop_uid, alighting.sequence
    ):
        raise JourneyError("This journey is outside the supported pilot corridor.")
    walk_ref, train_ref = walk["reference_details"], train["reference_details"]
    departure = train["planned_departure_at"]
    local = datetime.fromisoformat(departure).astimezone(TAIPEI) if departure else None
    transfer = journey.transfer
    duration = walk["estimated_duration_seconds"]
    return {
        "name": journey.name, "service_date": journey.service_date,
        "walk_minutes": f"{duration / 60:.14g}" if duration is not None else "",
        "walk_source": walk["duration_assumption_source"] or ASSUMPTION_SOURCE,
        "walk_origin": walk_ref.get("origin_name", WALK_START),
        "walk_destination": walk_ref.get("destination_name", WALK_END),
        "train_number": train_ref.get("train_number", ""),
        "train_time": local.time().isoformat() if local else "",
        "train_source": train_ref.get("timetable_source", ASSUMPTION_SOURCE),
        "allowance_minutes": str(transfer["min_transfer_minutes"]) if transfer else "",
        "allowance_source": transfer["assumption_source"] if transfer else ASSUMPTION_SOURCE,
    }


def missing_timing_inputs(draft: JourneyDraft) -> tuple[str, ...]:
    return tuple(label for value, label in (
        (draft.walk, "Walking time"),
        (draft.train, "THSR train number and departure time"),
        (draft.allowance, "Boarding buffer"),
    ) if value is None)


def save_message(outcome, journey_id, source_journey_id=None):
    if outcome == "cloned":
        return f"Created draft #{journey_id}. Assessed journey #{source_journey_id} was preserved."
    if outcome == "unchanged":
        return f"Journey #{journey_id} has no changes."
    return f"Saved changes to journey #{journey_id}."


def store_error_message(error):
    message = str(error)
    if message == "Stale journey edit; reload before saving":
        return "This journey changed after you loaded it. Load it again before saving. Your changes were not saved."
    if message in (
        "Journey not found",
        "This journey is outside the supported pilot corridor.",
        "Journey is outside the editable pilot shape",
        "Journey has an unsupported transfer",
        "Journey name must be nonblank text",
        "Walk source must be nonblank text",
        "Walk origin must be nonblank text",
        "Walk destination must be nonblank text",
        "Train number must be nonblank text",
        "Timetable source must be nonblank text",
        "Boarding allowance source must be nonblank text",
        "Add walk, train, and boarding allowance details before confirming assumptions",
        "Train departure must match the Taipei service date",
        "Walk duration must be an integer of at least 1 within PostgreSQL integer range",
        "Boarding allowance must be an integer of at least 0 within PostgreSQL integer range",
    ):
        return message
    return "The journey operation failed. Check the dedicated pilot database configuration and try again."


def assessment_prerequisites(journey: Journey) -> tuple[str, ...]:
    _, walk, train = journey.legs
    transfer = journey.transfer
    missing = []
    if not transfer or (transfer["from_leg_id"], transfer["to_leg_id"]) != (walk["leg_id"], train["leg_id"]):
        missing.append("Save a walk → THSR transfer with a boarding allowance.")
    if not walk["estimated_duration_seconds"] or not walk["duration_assumption_source"]:
        missing.append("Save a walking duration and its source.")
    if not train["planned_departure_at"]:
        missing.append("Save a dated THSR departure.")
    walk_ref, train_ref = walk["reference_details"], train["reference_details"]
    if any(not ref.get(key) for ref in (walk_ref, train_ref) for key in
           ("reference_data_version", "assumption_version", "valid_from", "valid_to")) or any(
               walk_ref[key] != train_ref[key] for key in ("reference_data_version", "assumption_version")
           ):
        missing.append("Save the journey with all timing assumptions for the service date.")
    return tuple(missing)


def assessment_spec_from_form(journey, earliest, latest, evaluated, source) -> AssessmentSpec:
    if assessment_prerequisites(journey):
        raise JourneyError("Complete the saved journey prerequisites before assessing.")
    day = journey.service_date
    reference = journey.legs[1]["reference_details"]
    return AssessmentSpec(
        journey_id=journey.journey_id,
        transfer_id=journey.transfer["transfer_id"],
        evaluated_at=_taipei_datetime(day, evaluated, "the evaluation timestamp"),
        reference_version=reference["reference_data_version"],
        assumption_version=reference["assumption_version"],
        scenario="synthetic",
        earliest_arrival=_taipei_datetime(day, earliest, "earliest arrival"),
        latest_arrival=_taipei_datetime(day, latest, "latest arrival"),
        interval_basis="synthetic",
        interval_source=source.strip() or ARRIVAL_SOURCE,
        buffer_semantics="boarding_only",
    )


def assessment_history(rows):
    """Yield displayable briefs and keep unsupported legacy rows visible."""
    for row in rows:
        try:
            brief = build_brief(row)
        except BriefError:
            brief = None
        snapshot = row.get("input_snapshot") or {}
        request = snapshot.get("request") if isinstance(snapshot, dict) else None
        origin = request.get("interval_source") if isinstance(request, dict) else None
        yield row["assessment_id"], brief, origin if isinstance(origin, str) and origin.strip() else None


def observation_summary(observation, route: RouteVariant, *, evaluated_at=None) -> dict[str, str]:
    """Display V0.1 boarding evidence quality without constructing arrival bounds."""
    fetched = _time(observation["fetched_at"])
    source = _time(observation["source_updated_at"])
    evaluation = fetched if evaluated_at is None else _time(evaluated_at)
    updated = _time(observation["raw_payload"].get("UpdateTime"))
    boarding, alighting = route.stops
    # The evidence gate only needs a bus leg and a selected observation. This card
    # can be shown even when the user has not loaded a journey.
    spec = SimpleNamespace(
        scenario="source_backed", observation_ids=(observation["observation_id"],),
        evidence_leg_id=1, evidence_stop_uid=boarding.stop_uid,
        evaluated_at=evaluation, freshness_seconds=120,  # V0.1 default policy
    )
    leg = dict(leg_id=1, transport_mode="bus", route_uid=route.route_uid,
               direction=route.direction, origin_stop_uid=boarding.stop_uid,
               destination_stop_uid=alighting.stop_uid)
    quality, _, issues = _evidence(spec, {"observations": [observation], "legs": [leg]})
    status = observation["stop_status"]
    status_label = STATUS_LABELS.get(status, "unknown").replace("_", " ")
    estimate = observation["estimate_seconds"]
    summary = {
        "Run ID": str(observation["run_id"]),
        "Observation ID": str(observation["observation_id"]),
        "StopStatus": f"{status} ({status_label})" if status is not None else "Not reported",
        "EstimateTime": f"{estimate} s" if estimate is not None else "Not reported",
        "SrcUpdateTime": source.astimezone(TAIPEI).isoformat(sep=" ") if source else "Not reported",
        "UpdateTime": updated.astimezone(TAIPEI).isoformat(sep=" ") if updated else "Not reported",
        "fetched_at": fetched.astimezone(TAIPEI).isoformat(sep=" "),
        "Freshness lag": f"{(fetched - source).total_seconds():g} s" if source else "Unknown",
        "V0.1 ETA eligibility at fetch time" if evaluated_at is None else "V0.1 ETA eligibility at evaluation": (
            "Eligible as boarding-stop evidence; no downstream interval established." if quality == "usable"
            else f"Rejected ({quality}). " + " ".join(ISSUES.get(issue, issue) for issue in issues)
        ),
    }
    if evaluated_at is not None:
        summary["Evaluated at"] = evaluation.astimezone(TAIPEI).isoformat(sep=" ")
        summary["Age at evaluation"] = f"{(evaluation - source).total_seconds():g} s" if source else "Unknown"
    return summary


def live_evaluation_time(fetched_at, local_timestamp=""):
    """Use the saved acquisition time unless an explicit Taipei time was entered."""
    if not local_timestamp.strip():
        return _time(fetched_at)
    try:
        day, value = local_timestamp.strip().split(maxsplit=1)
        return _taipei_datetime(date.fromisoformat(day), value, "the evaluation time")
    except (ValueError, JourneyError):
        raise JourneyError("Enter an evaluation time as YYYY-MM-DD HH:MM in Taipei time.") from None


def live_observation_prerequisites(journey, fetched_at, evaluated_at):
    day = _time(fetched_at).astimezone(TAIPEI).date()
    if journey is None:
        return (f"Load a saved journey dated {day} to assess this observation.",)
    if journey.service_date != day:
        return (f"This observation was fetched on {day}. Select a journey with that service date.",)
    missing = assessment_prerequisites(journey)
    if missing:
        return missing
    evaluation_day = evaluated_at.astimezone(TAIPEI).date()
    for leg in journey.legs[1:]:
        reference = leg["reference_details"]
        try:
            start, end = (date.fromisoformat(reference[field]) for field in ("valid_from", "valid_to"))
            applicable = all(start <= value <= end for value in (day, evaluation_day))
        except (TypeError, ValueError):
            applicable = False
        if not applicable:
            return ("The saved timing references must cover the service and evaluation dates.",)
    return ()


def offline_observation_prerequisites(journey: Journey | None, fetched_at: str) -> tuple[str, ...]:
    fixture_date = _time(fetched_at).astimezone(TAIPEI).date()
    if journey is None:
        return (f"Load a saved journey dated {fixture_date} to use this historical snapshot.",)
    if journey.service_date != fixture_date:
        return (f"The fixture is dated {fixture_date}, but the saved service date is "
                f"{journey.service_date}. Select a journey dated {fixture_date}.",)
    return tuple(f"For the {fixture_date} fixture: {item}" for item in assessment_prerequisites(journey))
