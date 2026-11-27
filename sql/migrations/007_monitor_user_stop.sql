BEGIN;

ALTER TABLE monitor_sessions
    DROP CONSTRAINT monitor_sessions_stop_reason_check,
    ADD CONSTRAINT monitor_sessions_stop_reason_check
        CHECK (stop_reason IN ('deadline', 'budget_exhausted', 'user_stopped'));

COMMIT;
