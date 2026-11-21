from dataclasses import replace
from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from planaware.assessment import AssessmentError, build_assessment, canonical_json
from planaware.catalog import load_catalog
from planaware.journeys import (
    Journey, JourneyError, JourneySave, _draft_values,
    load_synthetic_212_snapshot, observation_assessment_spec,
)
from planaware.n1 import _read_json
from planaware.ui_helpers import (
    ARRIVAL_SOURCE, ASSUMPTION_SOURCE, WALK_END, WALK_START,
    assessment_history, assessment_prerequisites, assessment_spec_from_form, draft_from_form,
    form_from_journey, missing_timing_inputs, offline_observation_prerequisites,
    observation_summary, save_message, store_error_message,
)
from test_brief import saved_row
from test_journeys import DRAFT, TIMED

ROOT = Path(__file__).resolve().parents[1]
ROUTE = load_catalog().route
CONFIRMED = replace(TIMED, confirm_service_date_assumptions=True)


def saved_journey(draft=DRAFT, *, journey_id=1, token="original-token", assessed=False):
    name, legs, allowance = _draft_values(draft)
    rows = []
    for order, leg in enumerate(legs, 1):
        mode, route, direction, origin, destination, departure, duration, source, reference = leg
        rows.append(dict(leg_id=journey_id * 10 + order, leg_order=order, transport_mode=mode,
                         route_uid=route, direction=direction, origin_stop_uid=origin,
                         destination_stop_uid=destination, planned_departure_at=departure.isoformat() if departure else None,
                         estimated_duration_seconds=duration, duration_assumption_source=source,
                         reference_details=reference))
    transfer = dict(transfer_id=journey_id, from_leg_id=rows[1]["leg_id"], to_leg_id=rows[2]["leg_id"],
                    min_transfer_minutes=allowance[0], assumption_source=allowance[1]) if allowance else None
    return Journey(journey_id, name, draft.service_date, tuple(rows), transfer, assessed, token)


def demo_observation():
    snapshot = load_synthetic_212_snapshot()
    row, = _read_json(snapshot.records_json)
    return dict(row, run_id=12, observation_id=42, source_system="tdx_bus_n1",
                fetched_at=snapshot.fetched_at)


def historical_journey():
    day = datetime.fromisoformat(demo_observation()["fetched_at"]).date()
    departure = datetime.combine(day, CONFIRMED.train.departure_at.timetz())
    return saved_journey(replace(CONFIRMED, service_date=day,
                                 train=replace(CONFIRMED.train, departure_at=departure)))


class JourneyFormTests(unittest.TestCase):
    def test_round_trip_timing_and_catalog_identifiers(self):
        values = form_from_journey(saved_journey(TIMED), ROUTE)
        draft = draft_from_form(values, ROUTE)
        self.assertEqual(draft, CONFIRMED)
        self.assertEqual(draft.boarding, ("TPE10600", 10))
        self.assertEqual(draft.alighting, ("TPE10606", 13))
        self.assertEqual(missing_timing_inputs(draft), ())

    def test_optional_inputs_stay_unknown_or_explicit_zero(self):
        values = form_from_journey(saved_journey(), ROUTE)
        draft = draft_from_form(values, ROUTE)
        self.assertIsNone(draft.walk)
        self.assertIsNone(draft.train)
        self.assertIsNone(draft.allowance)
        self.assertEqual(len(missing_timing_inputs(draft)), 3)
        values.update(allowance_minutes="0", allowance_source="Explicit test assumption")
        self.assertEqual(draft_from_form(values, ROUTE).allowance.minutes, 0)

    def test_timing_defaults_apply_only_when_timing_is_entered(self):
        values = form_from_journey(saved_journey(), ROUTE)
        self.assertEqual((values["walk_origin"], values["walk_destination"]), (WALK_START, WALK_END))
        self.assertIsNone(draft_from_form(values, ROUTE).walk)
        values.update(walk_minutes="4.5", train_number="SYNTHETIC-999", train_time="12:30", allowance_minutes="5")
        draft = draft_from_form(values, ROUTE)
        self.assertEqual(draft.walk.duration_seconds, 270)
        self.assertEqual((draft.walk.origin, draft.walk.destination), (WALK_START, WALK_END))
        self.assertEqual((draft.walk.source, draft.train.source, draft.allowance.source),
                         (ASSUMPTION_SOURCE,) * 3)
        self.assertTrue(draft.confirm_service_date_assumptions)

    def test_service_date_drives_train_date_and_rejects_partial_train_input(self):
        values = form_from_journey(saved_journey(TIMED), ROUTE)
        values.update(service_date=date(2026, 11, 24))
        draft = draft_from_form(values, ROUTE)
        self.assertEqual(draft.train.departure_at.date(), date(2026, 11, 24))
        values.update(train_time="")
        with self.assertRaisesRegex(JourneyError, "THSR departure time"):
            draft_from_form(values, ROUTE)

    def test_subminute_legacy_walking_duration_round_trips(self):
        draft = replace(TIMED, walk=replace(TIMED.walk, duration_seconds=301))
        values = form_from_journey(saved_journey(draft), ROUTE)
        self.assertEqual(draft_from_form(values, ROUTE).walk.duration_seconds, 301)

    def test_save_messages_and_controlled_errors(self):
        self.assertIn("no changes", save_message("unchanged", 1))
        self.assertIn("Saved changes", save_message("updated", 1))
        self.assertIn("Assessed journey #1 was preserved", save_message("cloned", 2, 1))
        self.assertIn("Load it again", store_error_message(JourneyError("Stale journey edit; reload before saving")))
        self.assertNotIn("sensitive DSN", store_error_message(JourneyError("sensitive DSN")))


    def test_assessment_spec_uses_saved_versions_and_explicit_taipei_inputs(self):
        journey = saved_journey(CONFIRMED)
        values = form_from_journey(journey, ROUTE)
        self.assertEqual(draft_from_form(values, ROUTE), CONFIRMED)
        spec = assessment_spec_from_form(journey, "11:45", "11:50", "11:30", "  Manual test bounds  ")
        self.assertEqual((spec.journey_id, spec.transfer_id), (journey.journey_id, journey.transfer["transfer_id"]))
        self.assertEqual((spec.scenario, spec.interval_basis, spec.buffer_semantics),
                         ("synthetic", "synthetic", "boarding_only"))
        self.assertEqual(spec.observation_ids, ())
        self.assertEqual(spec.earliest_arrival.isoformat(), "2026-11-23T11:45:00+08:00")
        self.assertEqual(spec.latest_arrival.isoformat(), "2026-11-23T11:50:00+08:00")
        self.assertEqual(spec.evaluated_at.isoformat(), "2026-11-23T11:30:00+08:00")
        self.assertEqual(spec.interval_source, "Manual test bounds")
        self.assertEqual(assessment_spec_from_form(journey, "11:45", "11:50", "11:30", "").interval_source,
                         ARRIVAL_SOURCE)
        reference = journey.legs[1]["reference_details"]
        self.assertEqual(spec.reference_version, reference["reference_data_version"])
        self.assertEqual(spec.assumption_version, reference["assumption_version"])

    def test_assessment_gate_requires_saved_timing_and_matching_versions(self):
        self.assertEqual(len(assessment_prerequisites(saved_journey())), 4)
        self.assertEqual(len(assessment_prerequisites(saved_journey(TIMED))), 1)
        self.assertEqual(assessment_prerequisites(saved_journey(CONFIRMED)), ())
        with self.assertRaisesRegex(JourneyError, "before assessing"):
            assessment_spec_from_form(saved_journey(TIMED), "", "", "", "")
        with self.assertRaisesRegex(JourneyError, "HH:MM"):
            assessment_spec_from_form(saved_journey(CONFIRMED), "2026-11-23", "", "11:30", "test")
        with self.assertRaisesRegex(JourneyError, "before confirming assumptions"):
            saved_journey(replace(DRAFT, confirm_service_date_assumptions=True))

    def test_history_displays_saved_briefs_and_skips_unsupported_format(self):
        row = saved_row()
        history = list(assessment_history([row, {"assessment_id": 99}]))
        self.assertEqual(history[0][1]["status"], row["status"])
        self.assertEqual(history[0][1]["reason_code"], row["reason_code"])
        self.assertEqual(history[0][2], row["input_snapshot"]["request"]["interval_source"])
        self.assertEqual(history[1], (99, None, None))


    def test_offline_card_shows_saved_clocks_and_rejection(self):
        observation = demo_observation()
        card = observation_summary(observation, ROUTE)
        self.assertEqual(card["Run ID"], "12")
        self.assertEqual(card["Observation ID"], "42")
        self.assertEqual(card["StopStatus"], "1 (not departed)")
        self.assertEqual(card["EstimateTime"], "480 s")
        self.assertEqual(card["SrcUpdateTime"], "2026-11-20 11:30:00+08:00")
        self.assertEqual(card["fetched_at"], "2026-11-20 11:30:05+08:00")
        self.assertEqual(card["Freshness lag"], "5 s")
        key = "Boarding evidence quality at fetch time"
        self.assertIn("Rejected", card[key])
        self.assertIn("Source status", card[key])
        normal = dict(observation, stop_status=0, raw_payload=dict(observation["raw_payload"], StopStatus=0))
        self.assertIn("Eligible as boarding-stop evidence", observation_summary(normal, ROUTE)[key])

    def test_offline_assessment_requires_matching_service_date_and_timing(self):
        fetched = demo_observation()["fetched_at"]
        messages = offline_observation_prerequisites(saved_journey(CONFIRMED), fetched)
        self.assertIn("2026-11-20", messages[0])
        self.assertIn("2026-11-23", messages[0])
        matching = historical_journey()
        self.assertEqual(offline_observation_prerequisites(matching, fetched), ())
        incomplete = offline_observation_prerequisites(replace(matching, transfer=None), fetched)
        self.assertIn("boarding allowance", incomplete[0])
        self.assertIn("2026-11-20", incomplete[0])


@unittest.skipUnless(find_spec("streamlit"), "Streamlit optional dependency unavailable")
class JourneyStreamlitTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest

        self.store = Mock(spec=["create_draft", "get_journey", "save_journey", "assess_journey",
                                "list_assessments", "list_journeys", "import_snapshot", "list_run_observations"])
        self.recent = {}
        self.store.list_journeys.side_effect = lambda: list(self.recent.values())
        self.store.list_assessments.return_value = []
        store_patch = patch("planaware.journeys.JourneyStore", return_value=self.store)
        self.constructor = store_patch.start()
        self.addCleanup(store_patch.stop)
        env = patch.dict("os.environ", {"PLANAWARE_PILOT_DSN": "test-only-placeholder",
                                       "TDX_CLIENT_ID": "", "TDX_CLIENT_SECRET": ""})
        env.start()
        self.addCleanup(env.stop)
        self.app = AppTest.from_file(str(ROOT / "app.py"))

    def click(self, label):
        next(button for button in self.app.button if button.label == label).click().run()
        self.assertFalse(self.app.exception)

    def remember(self, journey):
        self.recent[journey.journey_id] = dict(
            journey_id=journey.journey_id, journey_name=journey.name,
            service_date=journey.service_date, has_assessments=journey.has_assessments,
        )
        return journey

    def load(self, journey):
        self.remember(journey)
        self.store.get_journey.return_value = journey
        self.app.run()
        self.app.selectbox(key="journey_selector").set_value(journey.journey_id).run()
        self.assertFalse(self.app.exception)

    def test_missing_configuration_fails_closed(self):
        with patch.dict("os.environ", {"PLANAWARE_PILOT_DSN": ""}):
            self.app.run()
        self.constructor.assert_not_called()
        self.assertIn("PLANAWARE_PILOT_DSN", self.app.warning[0].value)

    def test_render_and_load_do_not_write_create_requires_submit(self):
        self.app.run()
        self.app.run()
        self.store.get_journey.assert_not_called()
        self.store.save_journey.assert_not_called()
        self.store.create_draft.assert_not_called()
        self.store.create_draft.side_effect = lambda draft: self.remember(saved_journey(draft))
        next(field for field in self.app.text_input if field.label == "Journey name").set_value("New pilot")
        self.click("Save journey with assumptions")
        self.store.create_draft.assert_called_once()
        self.app.run()
        self.store.create_draft.assert_called_once()
        self.assertIn("Incomplete draft", " ".join(caption.value for caption in self.app.caption))
        self.assertTrue(any("Add walking time" in item.value for item in self.app.markdown))
        self.load(saved_journey(journey_id=2))
        self.store.get_journey.assert_called_once_with(2)
        self.store.save_journey.assert_not_called()
        self.store.create_draft.assert_called_once()

    def test_save_outcomes_refresh_identity_and_stale_token_keeps_loaded_state(self):
        self.app.run()
        original = saved_journey(TIMED, assessed=True)
        self.load(original)
        self.store.save_journey.side_effect = JourneyError("Stale journey edit; reload before saving")
        next(field for field in self.app.text_input if field.label == "Journey name").set_value("Retain this edit")
        self.click("Save journey with assumptions")
        self.assertIn("Load it again", self.app.error[0].value)
        self.assertEqual(self.store.save_journey.call_args.args[2], original.edit_token)
        self.assertEqual(next(field.value for field in self.app.text_input if field.label == "Journey name"), "Retain this edit")
        self.store.save_journey.side_effect = None
        for outcome, result, source in (
            ("cloned", saved_journey(TIMED, journey_id=2, token="clone-token"), 1),
            ("updated", saved_journey(TIMED, journey_id=2, token="updated-token"), None),
            ("unchanged", saved_journey(TIMED, journey_id=2, token="updated-token"), None),
        ):
            self.store.save_journey.return_value = JourneySave(self.remember(result), outcome, source)
            self.click("Save journey with assumptions")
            self.assertEqual(self.app.success[0].value, save_message(outcome, result.journey_id, source))
        self.assertEqual(self.store.save_journey.call_args.args[0::2], (2, "updated-token"))
        self.assertTrue(next(button for button in self.app.button if button.label == "Assess saved journey").disabled)
        self.store.assess_journey.assert_not_called()

    def test_assessment_requires_submit_refreshes_journey_and_reads_saved_brief(self):
        self.app.run()
        loaded = saved_journey(CONFIRMED)
        self.load(loaded)
        self.app.run()
        self.store.assess_journey.assert_not_called()
        for label, value in (
            ("Earliest arrival", "11:45"),
            ("Latest arrival", "11:50"),
            ("Evaluation time", "11:30"),
            ("Interval source", "Test assumptions"),
        ):
            next(field for field in self.app.text_input if field.label == label).set_value(value)

        self.store.assess_journey.side_effect = AssessmentError("private database diagnostics")
        self.click("Assess saved journey")
        self.assertNotIn("private database diagnostics", self.app.error[0].value)
        self.store.assess_journey.reset_mock(side_effect=True)
        row = saved_row()
        self.store.assess_journey.return_value = {"assessment_id": row["assessment_id"], "status": "ignored"}
        self.store.list_assessments.return_value = [row]
        self.store.get_journey.return_value = replace(loaded, has_assessments=True)
        self.click("Assess saved journey")
        self.store.assess_journey.assert_called_once()
        self.assertEqual(self.store.get_journey.call_count, 2)
        self.assertIn("Saved assessment", self.app.success[0].value)
        self.assertTrue(any("saved assessments" in info.value for info in self.app.info))
        self.assertTrue(any(row["status"].replace("_", " ") in expander.label for expander in self.app.expander))
        self.assertTrue(any(
            item.value == f"Interval source: {row['input_snapshot']['request']['interval_source']}"
            for item in self.app.text
        ))
        self.app.run()
        self.store.assess_journey.assert_called_once()
        self.store.save_journey.assert_not_called()

    def test_selector_restores_journey_from_url(self):
        journey = self.remember(saved_journey(journey_id=2))
        self.store.get_journey.return_value = journey
        self.app.query_params["journey"] = "2"
        self.app.run()
        self.assertFalse(self.app.exception)
        selector = self.app.selectbox(key="journey_selector")
        self.assertEqual(selector.value, 2)
        self.assertEqual(selector.options, ["Create new journey", f"{journey.name} · {journey.service_date}"])
        self.store.get_journey.assert_called_once_with(2)
        self.store.save_journey.assert_not_called()

    def test_offline_demo_requires_explicit_import_and_assess_then_shows_persisted_brief(self):
        self.app.run()
        journey = historical_journey()
        self.load(journey)
        self.app.run()
        self.store.import_snapshot.assert_not_called()
        self.store.assess_journey.assert_not_called()
        self.store.import_snapshot.return_value = 12
        observation = demo_observation()
        self.store.list_run_observations.return_value = [observation]
        self.click("Import demo N1 snapshot")
        self.store.import_snapshot.assert_called_once()
        self.store.list_run_observations.assert_called_once_with(12, "TPE10912", 0, "TPE10600")
        self.assertTrue(any("Synthetic historical observation" in item.value for item in self.app.info))
        self.assertTrue(any(item.value == "Observation ID: 42" for item in self.app.text))
        self.assertTrue(any(item.value == "Run ID: 12" for item in self.app.text))
        self.app.run()
        self.store.import_snapshot.assert_called_once()
        self.store.assess_journey.assert_not_called()

        spec = observation_assessment_spec(journey, observation)
        context = dict(journey=dict(journey_id=journey.journey_id, journey_name=journey.name,
                                    service_date=journey.service_date.isoformat()),
                       transfer=dict(journey.transfer, journey_id=journey.journey_id),
                       legs=journey.legs, observations=[observation])
        row = _read_json(canonical_json(dict(build_assessment(spec, context), assessment_id=73)))
        self.store.assess_journey.return_value = {"assessment_id": 73}
        self.store.list_assessments.return_value = [row]
        self.store.get_journey.return_value = replace(journey, has_assessments=True)
        self.click("Assess with this observation")
        self.store.assess_journey.assert_called_once_with(spec)
        self.assertTrue(any("#73 · insufficient evidence" in item.label for item in self.app.expander))
        self.assertTrue(any("Synthetic N1 observation fixture" in item.value for item in self.app.text))
        self.assertTrue(any("Minimum buffer: Unknown" == item.value for item in self.app.text))
        self.assertTrue(any("saved assessments" in item.value for item in self.app.info))
        self.app.run()
        self.store.import_snapshot.assert_called_once()
        self.store.assess_journey.assert_called_once()

    def test_offline_assessment_is_disabled_for_a_different_date(self):
        self.app.run()
        self.load(saved_journey(CONFIRMED))
        self.store.import_snapshot.return_value = 12
        self.store.list_run_observations.return_value = [demo_observation()]
        self.click("Import demo N1 snapshot")
        self.assertTrue(next(button for button in self.app.button if button.label == "Assess with this observation").disabled)
        self.assertTrue(any("fixture is dated 2026-11-20" in item.value for item in self.app.warning))
        self.store.assess_journey.assert_not_called()
        self.store.save_journey.assert_not_called()

    def test_live_control_requires_credentials_and_explicit_confirmation_and_click(self):
        with patch("planaware.live_observations.acquire_live_boarding") as fetch, patch(
            "planaware.live_observations.list_live_artifacts", return_value=[],
        ):
            self.app.run()
            self.assertTrue(next(item for item in self.app.button if item.label == "Fetch live TDX N1 once").disabled)
            fetch.assert_not_called()
            with patch.dict("os.environ", {"TDX_CLIENT_ID": "test-client", "TDX_CLIENT_SECRET": "test-secret"}):
                self.app.run()
                self.click("Fetch live TDX N1 once")
                fetch.assert_not_called()
                self.assertTrue(any("Confirm TDX access" in item.value for item in self.app.warning))
                next(item for item in self.app.checkbox if "available quota" in item.label).set_value(True)
                self.app.run()
                fetch.assert_not_called()
                observation = demo_observation()
                self.store.list_run_observations.return_value = [observation]
                fetch.return_value = (12, observation["fetched_at"])
                next(item for item in self.app.checkbox if "available quota" in item.label).set_value(True)
                self.click("Fetch live TDX N1 once")
                fetch.assert_called_once()
                self.assertTrue(fetch.call_args.kwargs["confirmed"])
                self.assertTrue(any("Newly acquired TDX observation" in item.value for item in self.app.info))
                self.assertTrue(any(item.value == "Observation ID: 42" for item in self.app.text))
                self.app.run()
                fetch.assert_called_once()
                self.store.assess_journey.assert_not_called()

    def test_saved_snapshot_replay_without_credentials_displays_empty_run(self):
        with patch("planaware.live_observations.acquire_live_boarding") as fetch, patch(
            "planaware.live_observations.list_live_artifacts", return_value=["0" * 32 + ".json"],
        ), patch("planaware.live_observations.replay_live_boarding",
                 return_value=(12, demo_observation()["fetched_at"])) as replay:
            self.store.list_run_observations.return_value = []
            self.app.run()
            replay.assert_not_called()
            self.click("Replay saved snapshot")
            replay.assert_called_once()
            fetch.assert_not_called()
            self.assertTrue(any("Real historical replay" in item.value for item in self.app.info))
            self.assertTrue(any("no boarding-stop observations" in item.value for item in self.app.caption))
            self.store.assess_journey.assert_not_called()
