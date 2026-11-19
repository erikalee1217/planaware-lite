-- Database persistence boundary for idempotent impact assessments.
BEGIN;

CREATE FUNCTION public.persist_impact_assessment(p_record JSONB) RETURNS BIGINT
LANGUAGE plpgsql VOLATILE
AS $$
DECLARE
    expected public.impact_assessments%ROWTYPE;
    saved public.impact_assessments%ROWTYPE;
BEGIN
    IF p_record IS NULL OR jsonb_typeof(p_record) <> 'object' THEN
        RAISE EXCEPTION 'An assessment record object is required'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO expected
    FROM jsonb_populate_record(NULL::public.impact_assessments, p_record);

    IF expected.assessment_key IS NULL OR btrim(expected.assessment_key) = '' THEN
        RAISE EXCEPTION 'A nonblank assessment_key is required'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO public.impact_assessments (
        journey_id, assessment_key, status, reason_code,
        model_version, reference_data_version, assumption_version,
        input_snapshot, evidence_observation_id, evaluated_at,
        target_transfer_id, target_leg_id
    )
    VALUES (
        expected.journey_id, expected.assessment_key, expected.status, expected.reason_code,
        expected.model_version, expected.reference_data_version, expected.assumption_version,
        expected.input_snapshot, expected.evidence_observation_id, expected.evaluated_at,
        expected.target_transfer_id, expected.target_leg_id
    )
    ON CONFLICT (assessment_key) DO NOTHING;

    -- Concurrent retries rely on PostgreSQL's default READ COMMITTED isolation.
    SELECT * INTO STRICT saved
    FROM public.impact_assessments
    WHERE assessment_key = expected.assessment_key;

    IF (to_jsonb(saved) - 'assessment_id') IS DISTINCT FROM (to_jsonb(expected) - 'assessment_id') THEN
        RAISE EXCEPTION 'Assessment key already belongs to a different record'
            USING ERRCODE = '23514', CONSTRAINT = 'impact_assessment_conflict';
    END IF;

    RETURN saved.assessment_id;
END;
$$;

COMMIT;
