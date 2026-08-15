CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    scope_id INTEGER,
    operation TEXT NOT NULL,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('user', 'system')),
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
