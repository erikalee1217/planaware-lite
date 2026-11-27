BEGIN;

CREATE TABLE monitor_sessions (
    session_id TEXT PRIMARY KEY CHECK (btrim(session_id) <> ''),
    journey_id BIGINT NOT NULL REFERENCES journeys(journey_id),
    origin TEXT NOT NULL CHECK (origin IN ('live', 'synthetic')),
    lifecycle TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'paused', 'stopped')),
    acquisition TEXT NOT NULL DEFAULT 'none' CHECK (acquisition IN ('none', 'ok', 'failed')),
    freshness TEXT NOT NULL DEFAULT 'none' CHECK (freshness IN ('none', 'fresh', 'stale')),
    risk TEXT CHECK (risk IN ('on_track', 'at_risk', 'insufficient_evidence')),
    last_assessment_id BIGINT REFERENCES impact_assessments(assessment_id),
    source_updated_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ,
    evaluated_at TIMESTAMPTZ NOT NULL,
    last_input_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_input_sequence >= 0),
    last_input_identity TEXT CHECK (btrim(last_input_identity) <> ''),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    started_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    CHECK ((last_input_sequence = 0) = (last_input_identity IS NULL)),
    CHECK ((risk IS NULL) = (last_assessment_id IS NULL)),
    CHECK ((source_updated_at IS NULL) = (fetched_at IS NULL)),
    CHECK ((freshness = 'none') = (source_updated_at IS NULL)),
    CHECK (source_updated_at <= fetched_at AND fetched_at <= evaluated_at),
    CHECK (started_at <= evaluated_at),
    CHECK ((lifecycle = 'stopped') = (stopped_at IS NOT NULL)),
    CHECK (stopped_at = evaluated_at)
);

CREATE TABLE monitor_events (
    event_id TEXT PRIMARY KEY CHECK (event_id ~ '^[0-9a-f]{64}$'),
    session_id TEXT NOT NULL REFERENCES monitor_sessions(session_id),
    session_version BIGINT NOT NULL CHECK (session_version > 0),
    event_index SMALLINT NOT NULL CHECK (event_index >= 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'evidence_became_stale', 'acquisition_failed', 'evidence_recovered', 'risk_changed'
    )),
    basis TEXT NOT NULL CHECK (btrim(basis) <> ''),
    previous TEXT NOT NULL,
    current TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (session_id, session_version, event_index)
);

COMMIT;
