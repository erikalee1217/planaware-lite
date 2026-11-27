"""Pure presentation helpers for the offline monitoring page."""
from .assessment import to_utc_datetime
from .ui_helpers import TAIPEI


SIMULATION = "Simulation"
HISTORICAL = "Historical evidence"
SIMULATED_TIME = "Simulated time (Taipei)"
RECORDED_TIME = "Recorded at (real time, Taipei)"
STOP_REASONS = {
    "deadline": "Deadline reached",
    "budget_exhausted": "Acquisition budget exhausted",
    "user_stopped": "Stopped by user",
}
OUTCOMES = {
    "tick": "Freshness check", "ok": "Observation received", "empty": "No observation",
    "failed": "Acquisition failed", "uncertain": "Acquisition outcome uncertain",
    "duplicate": "Duplicate observation", "rejected": "Observation rejected",
}


def step_summary(demo):
    reason = "—"
    if demo.session.state.lifecycle == "stopped":
        reason = STOP_REASONS.get(demo.stop_reason, "No stop reason recorded")
    return {
        "Stop reason": reason,
        "Last offline step": OUTCOMES.get(demo.last_outcome, "No step recorded"),
        "Acquisitions remaining": demo.acquisitions_remaining if demo.acquisitions_remaining is not None else "—",
    }


def taipei_time(value):
    timestamp = to_utc_datetime(value)
    return timestamp.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S") if timestamp is not None else "—"


def monitoring_summary(state, assessment):
    freshness = state.freshness
    if not state.is_active and freshness != "none":
        freshness = f"{freshness} (last recorded)"
    return {
        "Lifecycle": state.lifecycle,
        "Acquisition result": state.acquisition,
        "Evidence freshness": freshness,
        "Last processed (Taipei)": taipei_time(state.evaluated_at),
        "Latest saved assessment risk": assessment["status"] if assessment is not None else "No saved assessment",
        "Saved assessment time (Taipei)": taipei_time(assessment["evaluated_at"]) if assessment is not None else "—",
    }


def event_history(events):
    ordered = sorted(events, key=lambda item: (to_utc_datetime(item.occurred_at), item.event_id))
    return [
        {"Event type": item.kind, SIMULATED_TIME: taipei_time(item.occurred_at),
         RECORDED_TIME: taipei_time(item.processed_at)}
        for item in ordered
    ]
