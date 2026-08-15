CREATE TABLE analysis_run_outputs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    unit_key TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal > 0),
    canonical_output_json TEXT NOT NULL,
    output_sha256 TEXT NOT NULL CHECK (
        length(output_sha256) = 64
        AND output_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_model TEXT NOT NULL CHECK (receipt_model = 'gpt-5.6-sol'),
    receipt_reasoning_effort TEXT NOT NULL CHECK (
        receipt_reasoning_effort = 'max'
    ),
    receipt_tool_call_count INTEGER NOT NULL CHECK (
        receipt_tool_call_count = 0
    ),
    receipt_boundary_mode TEXT NOT NULL CHECK (
        receipt_boundary_mode = 'stored_statements_only'
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id, unit_key) REFERENCES job_units(job_id, unit_key),
    UNIQUE(run_id, unit_key)
);

CREATE INDEX analysis_run_outputs_run_ordinal
ON analysis_run_outputs(run_id, batch_ordinal);

CREATE TRIGGER analysis_run_outputs_require_owned_codex_unit
BEFORE INSERT ON analysis_run_outputs
WHEN NOT EXISTS (
    SELECT 1
    FROM analysis_run_job_attempts AS attempt
    JOIN job_units AS unit
        ON unit.job_id = attempt.job_id
        AND unit.unit_key = NEW.unit_key
    WHERE attempt.run_id = NEW.run_id
        AND attempt.job_id = NEW.job_id
        AND unit.unit_key = 'codex:batch:1'
        AND unit.stage = 'codex_analysis'
        AND unit.ordinal = NEW.batch_ordinal
)
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_CODEX_OUTPUT_UNIT_MISMATCH'); END;

CREATE TRIGGER analysis_run_outputs_no_update
BEFORE UPDATE ON analysis_run_outputs
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_outputs_no_delete
BEFORE DELETE ON analysis_run_outputs
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
