-- A database persistence boundary, NOT a TDX adapter or HTTP client.
-- Callers supply already-normalized records and the unchanged raw JSON objects.
BEGIN;

CREATE FUNCTION persist_tdx_n1_snapshot(
    p_run_key TEXT,
    p_requested_at TIMESTAMPTZ,
    p_fetched_at TIMESTAMPTZ,
    p_records JSONB
) RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    saved ingestion_runs%ROWTYPE;
BEGIN
    IF p_run_key IS NULL OR btrim(p_run_key) = ''
       OR p_requested_at IS NULL OR p_fetched_at IS NULL
       OR p_records IS NULL OR jsonb_typeof(p_records) <> 'array' THEN
        RAISE EXCEPTION 'A run key, timestamps and normalized record array are required'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_records) AS item(value)
               WHERE jsonb_typeof(value) <> 'object') THEN
        RAISE EXCEPTION 'Each normalized record must be an object'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO ingestion_runs (run_key, requested_at)
    VALUES (p_run_key, p_requested_at)
    ON CONFLICT (run_key) DO NOTHING;

    -- Serializes writers sharing a logical run key. Different runs remain
    -- independent, even when every source value happens to be identical.
    SELECT * INTO STRICT saved FROM ingestion_runs
    WHERE run_key = p_run_key FOR UPDATE;

    IF saved.status = 'completed' THEN
        IF saved.requested_at IS DISTINCT FROM p_requested_at
           OR saved.snapshot_fetched_at IS DISTINCT FROM p_fetched_at
           OR saved.snapshot_payload IS DISTINCT FROM p_records THEN
            RAISE EXCEPTION 'Run key already belongs to a different frozen snapshot'
                USING ERRCODE = '23514', CONSTRAINT = 'ingestion_snapshot_conflict';
        END IF;
        RETURN saved.run_id;
    END IF;

    IF saved.status <> 'started'
       OR saved.requested_at IS DISTINCT FROM p_requested_at
       OR saved.snapshot_payload IS NOT NULL
       OR EXISTS (SELECT 1 FROM source_observations WHERE run_id = saved.run_id) THEN
        RAISE EXCEPTION 'Run is not an empty, matching started run'
            USING ERRCODE = '23514', CONSTRAINT = 'ingestion_run_not_writable';
    END IF;

    INSERT INTO source_observations (
        run_id, record_index, route_uid, direction, stop_uid,
        estimate_seconds, stop_status, source_updated_at, fetched_at, raw_payload
    )
    SELECT saved.run_id, (item.ordinality - 1)::integer,
           item.value->>'route_uid', (item.value->>'direction')::smallint,
           item.value->>'stop_uid', (item.value->>'estimate_seconds')::integer,
           (item.value->>'stop_status')::smallint,
           (item.value->>'source_updated_at')::timestamptz,
           p_fetched_at, item.value->'raw_payload'
    FROM jsonb_array_elements(p_records) WITH ORDINALITY AS item(value, ordinality);

    UPDATE ingestion_runs SET status = 'completed',
        completed_at = clock_timestamp(), snapshot_fetched_at = p_fetched_at,
        snapshot_payload = p_records
    WHERE run_id = saved.run_id;

    RETURN saved.run_id;
END;
$$;

COMMIT;
