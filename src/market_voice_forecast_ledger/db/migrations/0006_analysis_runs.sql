CREATE TABLE analysis_scopes (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    cutoff_day_jst TEXT NOT NULL,
    cutoff_exclusive_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'ready', 'running', 'current', 'stale', 'failed'
    )),
    stale_reason TEXT,
    UNIQUE(subject_id, cutoff_day_jst)
);

CREATE TABLE analysis_runs (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES analysis_scopes(id),
    model TEXT NOT NULL CHECK (model = 'gpt-5.6-sol'),
    reasoning_effort TEXT NOT NULL CHECK (reasoning_effort = 'max'),
    prompt_version TEXT NOT NULL CHECK (
        prompt_version = 'm2-core-prompt-contract-v1'
    ),
    schema_version TEXT NOT NULL CHECK (
        schema_version = 'm2-analysis-output-v1'
    ),
    information_boundary_version TEXT NOT NULL CHECK (
        information_boundary_version = 'stored-statements-only-v1'
    ),
    input_hash TEXT NOT NULL,
    input_contract_hash TEXT NOT NULL,
    started_at TEXT NOT NULL
);

CREATE TABLE analysis_run_job_attempts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    attempt_ordinal INTEGER NOT NULL CHECK (attempt_ordinal > 0),
    source_job_id INTEGER REFERENCES jobs(id),
    attached_at TEXT NOT NULL,
    UNIQUE(run_id, attempt_ordinal),
    CHECK (
        (attempt_ordinal = 1 AND source_job_id IS NULL)
        OR (attempt_ordinal > 1 AND source_job_id IS NOT NULL)
    )
);

CREATE TABLE analysis_run_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    status TEXT NOT NULL CHECK (status IN (
        'started', 'transport_validated', 'failed', 'accepted'
    )),
    safe_error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE analysis_run_segments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    segment_id INTEGER NOT NULL REFERENCES transcript_segments(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    published_at TEXT NOT NULL,
    policy_id INTEGER NOT NULL REFERENCES subject_channel_policies(id),
    policy_hash TEXT NOT NULL,
    assignment_kind TEXT NOT NULL CHECK (
        assignment_kind IN ('subject', 'interviewer', 'hold')
    ),
    assigned_subject_id INTEGER REFERENCES analysis_subjects(id),
    assignment_updated_at TEXT NOT NULL,
    assignment_evidence_hash TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, segment_id),
    CHECK (
        (assignment_kind = 'subject' AND assigned_subject_id IS NOT NULL)
        OR (assignment_kind IN ('interviewer', 'hold')
            AND assigned_subject_id IS NULL)
    )
);

CREATE TABLE analysis_input_snapshots (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES analysis_runs(id),
    input_text TEXT,
    metadata_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    snapshot_created_at TEXT NOT NULL,
    expires_at TEXT,
    text_deleted_at TEXT,
    CHECK (
        (input_text IS NOT NULL AND text_deleted_at IS NULL)
        OR (input_text IS NULL AND text_deleted_at IS NOT NULL)
    )
);

CREATE INDEX analysis_runs_scope_id ON analysis_runs(scope_id, id);
CREATE INDEX analysis_run_events_run_id ON analysis_run_events(run_id, id);
CREATE INDEX analysis_run_segments_run_id
ON analysis_run_segments(run_id, ordinal);

CREATE TRIGGER analysis_runs_no_update BEFORE UPDATE ON analysis_runs
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_runs_no_delete BEFORE DELETE ON analysis_runs
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_job_attempts_require_analysis_job
BEFORE INSERT ON analysis_run_job_attempts
WHEN (SELECT job_kind FROM jobs WHERE id = NEW.job_id) IS NOT 'analysis_scope'
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_JOB_REQUIRED'); END;

CREATE TRIGGER analysis_run_job_attempts_match_job_source
BEFORE INSERT ON analysis_run_job_attempts
WHEN (SELECT source_job_id FROM jobs WHERE id = NEW.job_id)
    IS NOT NEW.source_job_id
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_JOB_SOURCE_MISMATCH'); END;

CREATE TRIGGER analysis_run_job_attempts_no_update
BEFORE UPDATE ON analysis_run_job_attempts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_job_attempts_no_delete
BEFORE DELETE ON analysis_run_job_attempts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_events_no_update BEFORE UPDATE ON analysis_run_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_events_no_delete BEFORE DELETE ON analysis_run_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_segments_match_video
BEFORE INSERT ON analysis_run_segments
WHEN (SELECT video_id FROM transcript_segments WHERE id = NEW.segment_id)
    IS NOT NEW.video_id
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_SEGMENT_VIDEO_MISMATCH'); END;

CREATE TRIGGER analysis_run_segments_no_update
BEFORE UPDATE ON analysis_run_segments
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_segments_no_delete
BEFORE DELETE ON analysis_run_segments
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_input_snapshots_limited_update
BEFORE UPDATE ON analysis_input_snapshots
WHEN NOT (
    OLD.input_text IS NOT NULL
    AND NEW.input_text IS NULL
    AND OLD.text_deleted_at IS NULL
    AND NEW.text_deleted_at IS NOT NULL
    AND OLD.id IS NEW.id
    AND OLD.run_id IS NEW.run_id
    AND OLD.metadata_json IS NEW.metadata_json
    AND OLD.input_sha256 IS NEW.input_sha256
    AND OLD.snapshot_created_at IS NEW.snapshot_created_at
    AND OLD.expires_at IS NEW.expires_at
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ANALYSIS_SNAPSHOT'); END;

CREATE TRIGGER analysis_input_snapshots_no_delete
BEFORE DELETE ON analysis_input_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
