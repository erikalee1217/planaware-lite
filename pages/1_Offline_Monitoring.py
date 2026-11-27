"""Explicit controls for the local synthetic monitoring demonstration."""
import os

import streamlit as st

from planaware.journeys import JourneyError
from planaware.monitor_demo import MonitoringDemo
from planaware.monitor_store import MonitorError
from planaware.monitor_ui_helpers import HISTORICAL, SIMULATION, event_history, monitoring_summary, step_summary


def main():
    st.set_page_config(page_title="PlanAware · Offline Monitoring")
    st.title("Offline Monitoring")
    st.caption(f"{SIMULATION} · Synthetic observations and a fixed Taipei demo clock.")
    dsn = os.environ.get("PLANAWARE_PILOT_DSN", "").strip()
    if not dsn:
        st.warning("Set PLANAWARE_PILOT_DSN to a dedicated local pilot or test database.")
        return
    try:
        from psycopg import Error
    except ImportError:
        st.error("Install the pilot database dependency to use Offline Monitoring.")
        return

    try:
        service = MonitoringDemo(dsn)
        journeys = {row["journey_id"]: row for row in service.list_journeys()}
        if not journeys:
            st.info("Save a Journey before creating a synthetic session.")
            return
        selected = st.selectbox(
            "Saved journey", list(journeys), index=None,
            format_func=lambda key: f"{journeys[key]['journey_name']} · {journeys[key]['service_date']}",
        )
        if selected is None:
            return

        sessions = service.sessions(selected)
        if not sessions:
            if st.button("Create synthetic session"):
                service.create(selected)
                st.rerun()
            return

        latest, latest_lifecycle = sessions[0]
        labels = {identity: f"{identity} · {lifecycle}" for identity, lifecycle in sessions}
        selector = f"offline-monitor-session:{selected}"
        pending = st.session_state.pop(f"{selector}:pending", None)
        if pending in labels:
            st.session_state[selector] = pending
        identity = st.selectbox("Synthetic session", list(labels), format_func=labels.__getitem__, key=selector)
        demo = service.load(selected, identity)
        if demo is None:
            st.info("This session is unavailable. Reload the page to refresh the list.")
            return

        summary = monitoring_summary(demo.session.state, service.latest_assessment(selected))
        st.subheader(SIMULATION)
        for label in ("Lifecycle", "Acquisition result", "Evidence freshness", "Last processed (Taipei)"):
            st.text(f"{label}: {summary[label]}")
        st.subheader(HISTORICAL)
        st.caption("The latest saved assessment is a historical result. It does not show live transfer risk.")
        for label in ("Latest saved assessment risk", "Saved assessment time (Taipei)"):
            st.text(f"{label}: {summary[label]}")

        for label, value in step_summary(demo).items():
            st.text(f"{label}: {value}")

        is_latest = identity == latest
        run, stop = st.columns(2)
        if run.button("Run next offline step", disabled=not (is_latest and demo.can_step)):
            service.step(demo)
            st.rerun()
        if stop.button("Stop monitoring", disabled=not is_latest or demo.session.state.lifecycle == "stopped"):
            service.stop(demo)
            st.rerun()
        if not is_latest:
            st.caption("This session is read-only. Select the latest session to see its available actions.")
        elif latest_lifecycle == "stopped":
            st.caption("This simulation has ended. Start a new simulation to replay this journey.")
            if st.button("Start new simulation"):
                created = service.create(selected, after=identity)
                st.session_state[f"{selector}:pending"] = created.session.state.session_id
                st.rerun()
        elif not demo.can_step:
            st.caption("There is no step available. You can stop this session and start a new simulation.")

        st.subheader("Domain event history")
        st.caption("Simulated time follows the offline timeline. Recorded at is when the event was saved to the database.")
        history = event_history(service.events(demo))
        if history:
            st.table(history)
        else:
            st.caption("No domain events recorded.")
    except (JourneyError, MonitorError, Error):
        st.error("Could not complete the operation. Check the local pilot database and reload the page.")


if __name__ == "__main__":
    main()
