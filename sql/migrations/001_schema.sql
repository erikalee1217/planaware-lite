-- PostgreSQL 14+. Apply once, in order, to an empty application database.
BEGIN;

CREATE TABLE journeys (
    journey_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journey_name TEXT NOT NULL CHECK (btrim(journey_name) <> ''),
    service_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE journey_legs (
    leg_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journey_id BIGINT NOT NULL REFERENCES journeys(journey_id),
    leg_order INTEGER NOT NULL CHECK (leg_order > 0),
    transport_mode TEXT NOT NULL
        CHECK (transport_mode IN ('bus', 'metro', 'intercity_bus')),
    route_uid TEXT,
    direction SMALLINT CHECK (direction BETWEEN 0 AND 255),
    origin_stop_uid TEXT,
    destination_stop_uid TEXT,
    planned_departure_at TIMESTAMPTZ,
    planned_arrival_at TIMESTAMPTZ,
    -- Namespace, verified reference identifiers, provenance and assumptions.
    reference_details JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(reference_details) = 'object'),
    UNIQUE (journey_id, leg_order),
    UNIQUE (leg_id, journey_id),
    CONSTRAINT valid_leg_time CHECK (
        planned_departure_at IS NULL OR planned_arrival_at IS NULL
        OR planned_arrival_at > planned_departure_at
    )
);

CREATE TABLE transfer_constraints (
    transfer_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journey_id BIGINT NOT NULL,
    from_leg_id BIGINT NOT NULL,
    to_leg_id BIGINT NOT NULL,
    min_transfer_minutes INTEGER NOT NULL CHECK (min_transfer_minutes >= 0),
    assumption_source TEXT NOT NULL CHECK (btrim(assumption_source) <> ''),
    CHECK (from_leg_id <> to_leg_id),
    FOREIGN KEY (from_leg_id, journey_id)
        REFERENCES journey_legs(leg_id, journey_id),
    FOREIGN KEY (to_leg_id, journey_id)
        REFERENCES journey_legs(leg_id, journey_id),
    UNIQUE (from_leg_id, to_leg_id)
);

CREATE TABLE ingestion_runs (
    run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE CHECK (btrim(run_key) <> ''),
    source_system TEXT NOT NULL DEFAULT 'tdx_bus_n1'
        CHECK (source_system = 'tdx_bus_n1'),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'started'
        CHECK (status IN ('started', 'completed', 'failed')),
    snapshot_fetched_at TIMESTAMPTZ,
    -- Frozen normalized input, including each original record. Also represents
    -- an empty response and lets retries detect changed order/content.
    snapshot_payload JSONB CHECK (jsonb_typeof(snapshot_payload) = 'array'),
    CONSTRAINT run_completion_state CHECK (
        (status = 'started' AND completed_at IS NULL)
        OR (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
    ),
    CHECK (completed_at IS NULL OR completed_at >= requested_at),
    CONSTRAINT completed_run_has_snapshot CHECK (
        status <> 'completed'
        OR (snapshot_fetched_at IS NOT NULL AND snapshot_payload IS NOT NULL)
    )
);

CREATE TABLE source_observations (
    observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES ingestion_runs(run_id),
    record_index INTEGER NOT NULL CHECK (record_index >= 0),
    source_system TEXT NOT NULL DEFAULT 'tdx_bus_n1'
        CHECK (source_system = 'tdx_bus_n1'),
    route_uid TEXT NOT NULL CHECK (btrim(route_uid) <> ''),
    direction SMALLINT NOT NULL CHECK (direction BETWEEN 0 AND 255),
    stop_uid TEXT NOT NULL CHECK (btrim(stop_uid) <> ''),
    estimate_seconds INTEGER CHECK (estimate_seconds >= 0),
    -- Preserve numeric source codes without guessing a complete enum.
    stop_status SMALLINT,
    source_updated_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    raw_payload JSONB NOT NULL CHECK (jsonb_typeof(raw_payload) = 'object'),
    UNIQUE (run_id, record_index)
);

CREATE INDEX idx_eta_source_time ON source_observations (
    source_system, route_uid, direction, stop_uid,
    source_updated_at DESC NULLS LAST, fetched_at DESC, observation_id DESC
);

CREATE TABLE impact_assessments (
    assessment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    journey_id BIGINT NOT NULL REFERENCES journeys(journey_id),
    assessment_key TEXT NOT NULL UNIQUE CHECK (btrim(assessment_key) <> ''),
    status TEXT NOT NULL CHECK (status IN (
        'connection_at_risk', 'connection_feasible', 'insufficient_evidence'
    )),
    reason_code TEXT NOT NULL CHECK (btrim(reason_code) <> ''),
    model_version TEXT NOT NULL CHECK (btrim(model_version) <> ''),
    reference_data_version TEXT NOT NULL CHECK (btrim(reference_data_version) <> ''),
    assumption_version TEXT NOT NULL CHECK (btrim(assumption_version) <> ''),
    -- Actual journey/reference/assumption inputs used, not just their labels.
    -- This keeps prior evaluations interpretable if the plan is later edited.
    input_snapshot JSONB NOT NULL CHECK (jsonb_typeof(input_snapshot) = 'object'),
    evidence_observation_id BIGINT REFERENCES source_observations(observation_id),
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX idx_assessment_journey_time ON impact_assessments (
    journey_id, evaluated_at DESC, assessment_id DESC
);

COMMIT;
