BEGIN;

ALTER TABLE monitor_sessions
    ADD COLUMN deadline TIMESTAMPTZ,
    ADD COLUMN next_due_at TIMESTAMPTZ,
    ADD COLUMN min_interval INTEGER CHECK (min_interval > 0),
    ADD COLUMN max_acquisitions INTEGER CHECK (max_acquisitions >= 0),
    ADD COLUMN acquisitions_reserved INTEGER NOT NULL DEFAULT 0 CHECK (acquisitions_reserved >= 0),
    ADD COLUMN last_outcome TEXT CHECK (last_outcome IN ('tick', 'ok', 'empty', 'failed', 'uncertain', 'duplicate', 'rejected')),
    ADD COLUMN stop_reason TEXT CHECK (stop_reason IN ('deadline', 'budget_exhausted')),
    ADD CHECK (
        (deadline IS NULL AND next_due_at IS NULL AND min_interval IS NULL AND max_acquisitions IS NULL)
        OR (deadline IS NOT NULL AND min_interval IS NOT NULL AND max_acquisitions IS NOT NULL
            AND (next_due_at IS NOT NULL OR lifecycle = 'stopped'))
    ),
    ADD CHECK (acquisitions_reserved <= coalesce(max_acquisitions, 0)),
    ADD CHECK (stop_reason IS NULL OR lifecycle = 'stopped');

COMMIT;
