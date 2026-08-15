CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    source_job_id INTEGER REFERENCES jobs(id),
    job_kind TEXT NOT NULL CHECK (job_kind IN ('video_pipeline', 'analysis_scope')),
    manifest_hash TEXT NOT NULL,
    total_units INTEGER NOT NULL CHECK (total_units > 0),
    status TEXT NOT NULL CHECK (status IN (
        'queued',
        'running',
        'pause_requested',
        'paused',
        'cancel_requested',
        'stopped',
        'failed',
        'retrying',
        'succeeded'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_units (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    unit_key TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN (
        'video_metadata',
        'audio_acquisition',
        'transcription',
        'speaker_assignment',
        'analysis_input_extraction',
        'codex_analysis',
        'asset_mapping',
        'heatmap_update'
    )),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    declared_input_hash TEXT,
    dependency_keys_json TEXT NOT NULL,
    execution_contract_hash TEXT NOT NULL,
    external_input_hash TEXT,
    bound_input_hash TEXT,
    output_hash TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(job_id, unit_key),
    UNIQUE(job_id, ordinal),
    CHECK (bound_input_hash IS NOT NULL OR external_input_hash IS NULL),
    CHECK (status != 'running' OR (started_at IS NOT NULL AND output_hash IS NULL)),
    CHECK (status != 'success' OR (
        bound_input_hash IS NOT NULL
        AND output_hash IS NOT NULL
        AND error_code IS NULL
        AND finished_at IS NOT NULL
    )),
    CHECK (status != 'failed' OR (
        output_hash IS NULL
        AND error_code IS NOT NULL
        AND finished_at IS NOT NULL
    ))
);

CREATE TABLE job_unit_attempts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    result_status TEXT NOT NULL CHECK (result_status IN ('success', 'failed', 'interrupted')),
    output_hash TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    FOREIGN KEY(job_id, unit_key) REFERENCES job_units(job_id, unit_key),
    UNIQUE(job_id, unit_key, attempt_no),
    CHECK (
        (result_status = 'success' AND output_hash IS NOT NULL AND error_code IS NULL)
        OR (result_status = 'failed' AND output_hash IS NULL AND error_code IS NOT NULL)
        OR (result_status = 'interrupted' AND output_hash IS NULL AND error_code IS NULL)
    )
);

CREATE TABLE job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    unit_key TEXT,
    event_kind TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id, unit_key) REFERENCES job_units(job_id, unit_key)
);

CREATE INDEX job_units_status_ordinal
ON job_units(job_id, status, ordinal);

CREATE INDEX job_events_job_id
ON job_events(job_id, id);

CREATE TRIGGER jobs_manifest_immutable BEFORE UPDATE ON jobs
WHEN OLD.job_kind IS NOT NEW.job_kind
    OR OLD.manifest_hash IS NOT NEW.manifest_hash
    OR OLD.total_units IS NOT NEW.total_units
    OR OLD.source_job_id IS NOT NEW.source_job_id
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_units_manifest_immutable BEFORE UPDATE ON job_units
WHEN OLD.job_id IS NOT NEW.job_id
    OR OLD.unit_key IS NOT NEW.unit_key
    OR OLD.stage IS NOT NEW.stage
    OR OLD.ordinal IS NOT NEW.ordinal
    OR OLD.declared_input_hash IS NOT NEW.declared_input_hash
    OR OLD.dependency_keys_json IS NOT NEW.dependency_keys_json
    OR OLD.execution_contract_hash IS NOT NEW.execution_contract_hash
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_units_input_binding_immutable BEFORE UPDATE ON job_units
WHEN OLD.bound_input_hash IS NOT NULL
    AND (
        OLD.bound_input_hash IS NOT NEW.bound_input_hash
        OR OLD.external_input_hash IS NOT NEW.external_input_hash
    )
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_UNIT_INPUT'); END;

CREATE TRIGGER job_units_manifest_no_extra_insert BEFORE INSERT ON job_units
WHEN (
    SELECT COUNT(*) FROM job_units WHERE job_id = NEW.job_id
) >= (
    SELECT total_units FROM jobs WHERE id = NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_units_manifest_no_delete BEFORE DELETE ON job_units
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_unit_attempts_no_update BEFORE UPDATE ON job_unit_attempts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER job_unit_attempts_no_delete BEFORE DELETE ON job_unit_attempts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER job_events_no_update BEFORE UPDATE ON job_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER job_events_no_delete BEFORE DELETE ON job_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
