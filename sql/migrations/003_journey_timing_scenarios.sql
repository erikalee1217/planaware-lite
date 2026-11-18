-- Extend the foundation without rewriting previously applied migrations.
BEGIN;

ALTER TABLE journeys ADD COLUMN target_arrival_at TIMESTAMPTZ;

ALTER TABLE journey_legs
    DROP CONSTRAINT journey_legs_transport_mode_check,
    ADD CONSTRAINT journey_legs_transport_mode_check CHECK (transport_mode IN (
        'walking', 'bus', 'metro', 'intercity_bus', 'train', 'high_speed_rail'
    )),
    ADD COLUMN estimated_duration_seconds INTEGER CHECK (estimated_duration_seconds > 0),
    ADD COLUMN duration_assumption_source TEXT,
    ADD COLUMN boarding_deadline_at TIMESTAMPTZ,
    ADD COLUMN boarding_deadline_source TEXT,
    ADD CONSTRAINT duration_has_basis CHECK (
        (estimated_duration_seconds IS NULL AND duration_assumption_source IS NULL)
        OR (estimated_duration_seconds IS NOT NULL
            AND NULLIF(btrim(duration_assumption_source), '') IS NOT NULL)
    ),
    ADD CONSTRAINT boarding_deadline_has_basis CHECK (
        (boarding_deadline_at IS NULL AND boarding_deadline_source IS NULL)
        OR (boarding_deadline_at IS NOT NULL
            AND NULLIF(btrim(boarding_deadline_source), '') IS NOT NULL)
    ),
    ADD CONSTRAINT boarding_deadline_before_departure CHECK (
        boarding_deadline_at IS NULL OR planned_departure_at IS NULL
        OR boarding_deadline_at <= planned_departure_at
    ),
    ADD CONSTRAINT walking_has_no_boarding_deadline CHECK (
        transport_mode <> 'walking' OR boarding_deadline_at IS NULL
    );

COMMENT ON COLUMN journey_legs.estimated_duration_seconds IS
    'Reference duration, not a live prediction. NULL means unknown; basis required when populated.';
COMMENT ON COLUMN journey_legs.boarding_deadline_at IS
    'Deadline for completing boarding of this leg, not station arrival; must have an explicit basis.';
COMMENT ON COLUMN transfer_constraints.min_transfer_minutes IS
    'Additional transfer/access/boarding allowance; exclude walking already represented as an explicit leg.';

ALTER TABLE transfer_constraints ADD UNIQUE (transfer_id, journey_id);

ALTER TABLE impact_assessments
    DROP CONSTRAINT impact_assessments_status_check,
    ADD COLUMN legacy_status TEXT CHECK (
        legacy_status IN ('connection_at_risk', 'connection_feasible')
    ),
    ADD COLUMN target_leg_id BIGINT,
    ADD COLUMN target_transfer_id BIGINT,
    ADD FOREIGN KEY (target_leg_id, journey_id)
        REFERENCES journey_legs(leg_id, journey_id),
    ADD FOREIGN KEY (target_transfer_id, journey_id)
        REFERENCES transfer_constraints(transfer_id, journey_id),
    ADD CONSTRAINT assessment_single_target CHECK (
        target_leg_id IS NULL OR target_transfer_id IS NULL
    );

-- Preserve the exact original label for existing history. Evidence, input
-- snapshots, keys, evaluation times and model versions remain untouched.
UPDATE impact_assessments
SET legacy_status = status,
    status = CASE status
        WHEN 'connection_at_risk' THEN 'at_risk'
        WHEN 'connection_feasible' THEN 'on_track'
    END
WHERE status IN ('connection_at_risk', 'connection_feasible');

ALTER TABLE impact_assessments ADD CONSTRAINT impact_assessments_status_check
    CHECK (status IN ('on_track', 'at_risk', 'missed_deadline', 'insufficient_evidence'));

COMMENT ON COLUMN impact_assessments.target_leg_id IS
    'Optional leg target in the same journey. Both targets NULL means journey-level assessment.';
COMMENT ON COLUMN impact_assessments.target_transfer_id IS
    'Optional transfer target in the same journey. Cannot also target a leg.';
COMMENT ON COLUMN impact_assessments.legacy_status IS
    'Original pre-003 status label, retained only for migrated historical assessments.';

COMMIT;
