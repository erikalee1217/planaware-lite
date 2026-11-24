"""Streamlit entry point for PlanAware pilot journeys, N1 observations, and assessments"""

from dataclasses import replace
from datetime import datetime
import logging
import os
from pathlib import Path

import streamlit as st

from planaware.assessment import AssessmentError
from planaware.catalog import load_catalog
from planaware.diagnostics import log_failure
from planaware.journeys import (
    JourneyError, JourneyStore, load_synthetic_212_snapshot, observation_assessment_spec,
)
from planaware.live import PipelineError
from planaware.live_observations import (
    LIVE_RECORD_LIMIT, acquire_live_boarding, list_live_artifacts, live_credentials_available,
    replay_live_boarding,
)
from planaware.n1 import ValidationError
from planaware.risk import RiskInputError
from planaware.ui_helpers import (
    ARRIVAL_SOURCE, ASSUMPTION_SOURCE, TAIPEI, WALK_END, WALK_START,
    assessment_history, assessment_prerequisites, assessment_spec_from_form,
    draft_from_form, form_from_journey, live_evaluation_time, live_observation_prerequisites,
    missing_timing_inputs, offline_observation_prerequisites,
    observation_summary, save_message, store_error_message,
)


logger = logging.getLogger(__name__)
_APP_FAILURE_TYPES = (AssessmentError, JourneyError, PipelineError, RiskInputError, ValidationError)
LIVE_ARTIFACT_DIRECTORY = Path(__file__).resolve().parent / "work" / "live-snapshots"


def _log_action_failure(event, error):
    if not logger.isEnabledFor(logging.DEBUG):
        return

    # The store already logs driver errors before translating them for the UI.
    context = error.__cause__ or error.__context__
    if context is not None:
        try:
            from psycopg import Error
        except ImportError:
            pass
        else:
            seen = set()
            while context is not None and id(context) not in seen:
                if isinstance(context, Error):
                    return
                seen.add(id(context))
                context = context.__cause__ or context.__context__
    log_failure(logger, logging.DEBUG, event, error, expected_types=_APP_FAILURE_TYPES)


def show_summary(journey, route, values):
    draft = draft_from_form(values, route)
    boarding, alighting = route.stops
    missing = missing_timing_inputs(draft)
    st.subheader("Saved journey")
    st.text(f"{journey.name} · {journey.service_date.isoformat()}")
    st.caption("Saved · Incomplete draft" if missing else "Saved · Timing inputs entered")
    st.text(f"1. Bus {route.name} toward {route.headsign}: {boarding.name} → {alighting.name}")
    walk = f"{values['walk_minutes']} min" if draft.walk else "duration unknown"
    st.text(f"2. Walk: {walk}")
    train = (f"train {draft.train.train_number}, {draft.train.departure_at:%Y-%m-%d %H:%M:%S} Taipei time"
             if draft.train else "departure unknown")
    st.text(f"3. THSR Nangang southbound: {train}")
    allowance = f"{draft.allowance.minutes} minutes" if draft.allowance else "allowance unknown"
    st.text(f"Boarding buffer after the walk: {allowance}")
    if journey.has_assessments:
        st.info("This journey has saved assessments. Saving changes creates a new draft.")
    for item in missing:
        st.write(f"• Add {item.lower()}.")


def refresh_after_assessment(store, journey, state, assessment_id):
    state.assessment_notice = assessment_id
    # The assessment committed. Preserve that fact if the refresh fails.
    try:
        refreshed = store.get_journey(journey.journey_id)
    except JourneyError as error:
        log_failure(logger, logging.DEBUG, "pilot_ui.assessment_saved_refresh_failed", error,
                    expected_types=_APP_FAILURE_TYPES)
        state.journey = replace(journey, has_assessments=True)
        state.assessment_refresh_warning = True
    else:
        state.journey = refreshed
        state.pop("assessment_refresh_warning", None)
        if refreshed.edit_token != journey.edit_token:
            state.form_revision += 1
    st.rerun()


def show_offline_observation(store, journey, route, state):
    st.subheader("Bus 212 Observation (Offline Demo)")
    st.info("Synthetic historical observation — not live TDX data")
    if st.button("Import demo N1 snapshot"):
        try:
            snapshot = load_synthetic_212_snapshot()
            run_id = store.import_snapshot(snapshot)
            boarding = route.stops[0]
            observations = store.list_run_observations(run_id, route.route_uid, route.direction, boarding.stop_uid)
            if len(observations) != 1 or observations[0]["raw_payload"].get("synthetic_test_only") is not True:
                raise JourneyError("Expected one synthetic boarding observation")
        except (JourneyError, ValidationError, OSError) as error:
            _log_action_failure("pilot_ui.offline_import_failed", error)
            st.error("Could not load the synthetic boarding observation. Check the pilot database and retry the import.")
        else:
            state.offline_observation = observations[0]

    observation = state.get("offline_observation")
    if observation is None:
        st.caption("Import the bundled snapshot to inspect its saved boarding evidence.")
        return
    st.markdown(f"**Boarding stop · {route.stops[0].name}**")
    show_observation_card(observation, route)
    missing = offline_observation_prerequisites(journey, observation["fetched_at"])
    for item in missing:
        st.warning(item)
    if st.button("Assess with this observation", disabled=bool(missing)) and not missing:
        try:
            spec = observation_assessment_spec(journey, observation)
            result = store.assess_journey(spec)
        except (AssessmentError, JourneyError, RiskInputError) as error:
            _log_action_failure("pilot_ui.offline_assessment_failed", error)
            st.error("Could not save the observation assessment. Reload the journey and check its saved timing inputs.")
        else:
            refresh_after_assessment(store, journey, state, result["assessment_id"])


def show_observation_card(observation, route, *, evaluated_at=None):
    for label, value in observation_summary(observation, route, evaluated_at=evaluated_at).items():
        st.text(f"{label}: {value}")


def show_live_observation(store, journey, route, state):
    with st.expander("Live TDX N1 (optional)"):
        st.caption(f"Fetches current data for the Bus 212 boarding stop only and may use API quota. "
                   f"One authenticated N1 request, up to {LIVE_RECORD_LIMIT} records, with no automatic retries.")
        available = live_credentials_available()
        if not available:
            st.info("Live fetch is unavailable until TDX credentials are configured. Saved snapshots can still be replayed.")
        with st.form("fetch_live_n1"):
            confirmed = st.checkbox("I confirm TDX access and available quota.")
            fetch = st.form_submit_button("Fetch live TDX N1 once", disabled=not available)
        if fetch and available:
            if not confirmed:
                st.warning("Confirm TDX access and available quota before fetching.")
            else:
                try:
                    run_id, fetched_at = acquire_live_boarding(store, LIVE_ARTIFACT_DIRECTORY, confirmed=confirmed)
                except PipelineError as error:
                    _log_action_failure("pilot_ui.live_fetch_failed", error)
                    st.error(str(error))  # Pilot service emits fixed messages only.
                else:
                    state.live_run = (run_id, fetched_at, "Newly acquired TDX observation")

        try:
            artifacts = list_live_artifacts(LIVE_ARTIFACT_DIRECTORY)
        except PipelineError as error:
            _log_action_failure("pilot_ui.snapshot_list_failed", error)
            st.error("Could not list saved snapshots.")
            artifacts = []
        if artifacts:
            with st.form("replay_live_n1"):
                artifact = st.selectbox("Saved live snapshot", artifacts)
                replay = st.form_submit_button("Replay saved snapshot")
            if replay:
                try:
                    run_id, fetched_at = replay_live_boarding(store, LIVE_ARTIFACT_DIRECTORY, artifact)
                except (PipelineError, JourneyError) as error:
                    _log_action_failure("pilot_ui.snapshot_replay_failed", error)
                    st.error("Could not replay the saved snapshot. Check the pilot database and try replay again.")
                else:
                    state.live_run = (run_id, fetched_at, "Real historical replay")

        if "live_run" not in state:
            return
        run_id, fetched_at, provenance = state.live_run
        st.info(provenance)
        acquired = live_evaluation_time(fetched_at).astimezone(TAIPEI)
        st.text(f"Saved run #{run_id} · Acquired {acquired.isoformat(sep=' ')}")
        try:
            observations = store.list_run_observations(run_id, route.route_uid, route.direction, route.stops[0].stop_uid)
        except JourneyError:
            st.error("Could not read boarding-stop observations for this saved run.")
            return
        if not observations:
            st.caption("This run had no boarding-stop observations.")
            return
        by_id = {row["observation_id"]: row for row in observations}
        observation_id = st.selectbox("Boarding-stop observation", list(by_id), key=f"live-observation-{run_id}")
        observation = by_id[observation_id]
        chosen_time = st.text_input("Evidence evaluation time (Taipei)",
                                    key=f"live-evaluation-{run_id}",
                                    help="YYYY-MM-DD HH:MM. Leave blank to evaluate at the original fetch time.")
        try:
            evaluated_at = live_evaluation_time(observation["fetched_at"], chosen_time)
        except JourneyError as error:
            st.warning(str(error))
            show_observation_card(observation, route)
            return
        show_observation_card(observation, route, evaluated_at=evaluated_at)
        st.caption("A usable boarding ETA does not establish a downstream arrival interval.")
        missing = live_observation_prerequisites(journey, observation["fetched_at"], evaluated_at)
        for item in missing:
            st.warning(item)
        if st.button("Assess with selected TDX observation", disabled=bool(missing)) and not missing:
            try:
                spec = observation_assessment_spec(journey, observation, evaluated_at=evaluated_at)
                result = store.assess_journey(spec)
            except (AssessmentError, JourneyError, RiskInputError) as error:
                _log_action_failure("pilot_ui.live_assessment_failed", error)
                st.error("Could not save the observation assessment. Check the journey and evidence times.")
            else:
                refresh_after_assessment(store, journey, state, result["assessment_id"])


def show_assessment(store, journey, state):
    st.subheader("Assumption-Based Assessment")
    st.caption("Uses your assumptions only. Enter arrival times at the THSR boarding point "
               "that already include the bus ride and walk. This does not use live TDX predictions.")
    missing = assessment_prerequisites(journey)
    for item in missing:
        st.write(f"• {item}")
    with st.form(f"assessment-{journey.journey_id}-{state.form_revision}"):
        st.caption(f"Use HH:MM in Taipei time on {journey.service_date.isoformat()}. "
                   "Arrival times are at the THSR boarding point.")
        earliest_column, latest_column = st.columns(2)
        earliest = earliest_column.text_input("Earliest arrival")
        latest = latest_column.text_input("Latest arrival")
        evaluated = st.text_input("Evaluation time")
        with st.expander("Advanced: arrival estimate source (optional)"):
            source = st.text_input("Interval source", value=ARRIVAL_SOURCE)
        submit = st.form_submit_button("Assess saved journey", disabled=bool(missing))
    if submit and not missing:
        try:
            spec = assessment_spec_from_form(journey, earliest, latest, evaluated, source)
        except JourneyError as error:
            st.error(str(error))
        else:
            try:
                result = store.assess_journey(spec)
            except RiskInputError:
                st.error("Check that earliest arrival is at or before latest arrival and that all times are valid.")
            except (AssessmentError, JourneyError) as error:
                _log_action_failure("pilot_ui.assumption_assessment_failed", error)
                st.error("The assessment could not be saved. Reload the journey and check its inputs.")
            else:
                refresh_after_assessment(store, journey, state, result["assessment_id"])

    if state.get("assessment_notice") is not None:
        st.success(f"Saved assessment #{state.assessment_notice}.")
    if state.get("assessment_refresh_warning"):
        st.warning("The assessment was saved. Load the journey again to refresh the plan.")
    st.subheader("Assessment history")
    try:
        rows = store.list_assessments(journey.journey_id)
    except JourneyError:
        st.error("Could not load saved assessment history.")
        return
    if not rows:
        st.caption("No saved assessments.")
    for assessment_id, brief, source in assessment_history(rows):
        if brief is None:
            st.caption(f"Assessment #{assessment_id} uses a brief format this UI cannot display.")
            continue
        with st.expander(f"#{assessment_id} · {brief['status'].replace('_', ' ')} · {brief['evaluated_at']}",
                         expanded=assessment_id == (state.get("assessment_notice") or rows[0]["assessment_id"])):
            st.text(brief["explanation"])
            interval = brief["arrival_interval"]
            st.text(f"Arrival window: {interval['earliest'] or 'Unknown'} → {interval['latest'] or 'Unknown'}")
            st.text(f"{brief['deadline_label']}: {brief['boarding_deadline'] or 'Unknown'}")
            st.text(f"Minimum buffer: {brief['minimum_buffer'] or 'Unknown'}")
            st.text(brief["evidence_source"])
            st.text(f"Interval source: {source or 'Not recorded'}")
            for issue in brief["missing_or_unusable"]:
                st.text(issue)


def main():
    st.set_page_config(page_title="PlanAware · Journeys")
    st.title("PlanAware journeys")
    route = load_catalog().route
    boarding, alighting = route.stops
    st.caption(f"Taipei Bus {route.name} toward {route.headsign} · {boarding.name} → "
               f"{alighting.name} · walk to THSR Nangang southbound")

    dsn = os.environ.get("PLANAWARE_PILOT_DSN", "").strip()
    if not dsn:
        st.warning("Set PLANAWARE_PILOT_DSN for a dedicated pilot database before using Journey Management.")
        return
    store = JourneyStore(dsn)
    state = st.session_state
    state.setdefault("form_revision", 0)
    try:
        recent = {row["journey_id"]: row for row in store.list_journeys()}
    except JourneyError:
        st.error("Could not load saved journeys. Check the dedicated pilot database configuration.")
        return
    pending = state.pop("pending_journey_selection", None)
    if pending in recent:
        state.journey_selector = pending
    if "journey_selector" not in state:
        requested = st.query_params.get("journey")
        state.journey_selector = next((identity for identity in recent if str(identity) == requested), None)
    elif state.journey_selector not in recent:
        state.journey_selector = None

    def label(identity):
        if identity is None:
            return "Create new journey"
        row = recent[identity]
        return f"{row['journey_name']} · {row['service_date'].isoformat()}"

    selected = st.selectbox("Saved journey", [None, *recent], format_func=label, key="journey_selector")
    if selected is None:
        st.query_params.pop("journey", None)
    elif st.query_params.get("journey") != str(selected):
        st.query_params["journey"] = str(selected)

    journey = state.get("journey")
    reload_requested = selected is not None and st.button("Reload journey")
    if selected != (journey.journey_id if journey else None) or reload_requested:
        for key in ("journey", "notice", "assessment_notice", "assessment_refresh_warning"):
            state.pop(key, None)
        state.form_revision += 1
        journey = None
        if selected is not None:
            try:
                loaded = store.get_journey(selected)
                form_from_journey(loaded, route)
            except JourneyError as error:
                st.error(store_error_message(error))
                return
            journey = state.journey = loaded

    if state.get("notice"):
        st.success(state.notice)

    defaults = form_from_journey(journey, route) if journey else {}
    if journey is not None:
        show_summary(journey, route, defaults)
    st.subheader("Edit journey" if journey else "Create a draft")
    revision = state.form_revision
    with st.form(f"draft-{revision}"):
        def field(name, label, *, default="", help=None, area=st):
            return area.text_input(label, value=defaults.get(name, default), key=f"{revision}-{name}", help=help)

        values = {
            "name": field("name", "Journey name"),
            "service_date": st.date_input("Service date (Taipei)",
                                          value=defaults.get("service_date", datetime.now(TAIPEI).date()),
                                          key=f"{revision}-service_date"),
        }
        values["walk_minutes"] = field("walk_minutes", "Walking time (minutes)")
        train_number, train_time = st.columns(2)
        values.update(train_number=field("train_number", "THSR train number", area=train_number),
                      train_time=field("train_time", "THSR departure time (Taipei)", area=train_time, help="HH:MM"))
        values["allowance_minutes"] = field("allowance_minutes", "Boarding buffer (minutes)",
                                            help="Extra time after the walk.")
        with st.expander("Advanced: endpoints & source notes (optional)"):
            values.update(
                walk_origin=field("walk_origin", "Walk start", default=WALK_START),
                walk_destination=field("walk_destination", "Walk end", default=WALK_END),
                walk_source=field("walk_source", "Walking duration source", default=ASSUMPTION_SOURCE),
                train_source=field("train_source", "Timetable source", default=ASSUMPTION_SOURCE),
                allowance_source=field("allowance_source", "Boarding allowance source", default=ASSUMPTION_SOURCE),
            )
        st.caption("Timing fields are optional. Entered timings are saved as user assumptions for this service date.")
        submitted = st.form_submit_button("Save journey with assumptions", type="primary")
    if submitted:
        try:
            draft = draft_from_form(values, route)
        except JourneyError as error:
            st.error(str(error))
            return
        try:
            if journey:
                result = store.save_journey(journey.journey_id, draft, journey.edit_token)
                stored = result.journey
                notice = save_message(result.outcome, result.journey_id, result.source_journey_id)
            else:
                stored = store.create_draft(draft)
                notice = f"Created draft #{stored.journey_id}."
        except JourneyError as error:
            state.pop("notice", None)
            st.error(store_error_message(error))
        else:
            state.journey = stored
            state.pending_journey_selection = stored.journey_id
            state.pop("assessment_notice", None)
            state.pop("assessment_refresh_warning", None)
            state.notice = notice
            state.form_revision += 1
            st.rerun()

    show_offline_observation(store, journey, route, state)
    show_live_observation(store, journey, route, state)
    if journey is not None:
        show_assessment(store, journey, state)


if __name__ == "__main__":
    main()
