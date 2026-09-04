-- Schema-only migration: no production row is removed here.
CREATE TABLE voice_vad_repairs (
    id INTEGER PRIMARY KEY,
    schema_version TEXT NOT NULL CHECK (schema_version = 'presence-vad-repair.v1'),
    from_vad_contract_version TEXT NOT NULL CHECK (from_vad_contract_version = 'vad-v1'),
    to_vad_contract_version TEXT NOT NULL CHECK (to_vad_contract_version = 'vad-v2'),
    preview_hash TEXT NOT NULL CHECK (
        typeof(preview_hash) = 'text'
        AND length(CAST(preview_hash AS BLOB)) = 64
        AND preview_hash NOT GLOB '*[^0-9a-f]*'
    ),
    target_fingerprint TEXT NOT NULL CHECK (
        typeof(target_fingerprint) = 'text'
        AND length(CAST(target_fingerprint AS BLOB)) = 64
        AND target_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    preserved_fingerprint TEXT NOT NULL CHECK (
        typeof(preserved_fingerprint) = 'text'
        AND length(CAST(preserved_fingerprint AS BLOB)) = 64
        AND preserved_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    candidate_order_hash TEXT NOT NULL CHECK (
        typeof(candidate_order_hash) = 'text'
        AND length(CAST(candidate_order_hash AS BLOB)) = 64
        AND candidate_order_hash NOT GLOB '*[^0-9a-f]*'
    ),
    database_backup_sha256 TEXT NOT NULL CHECK (
        typeof(database_backup_sha256) = 'text'
        AND length(CAST(database_backup_sha256 AS BLOB)) = 64
        AND database_backup_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    runtime_backup_fingerprint TEXT NOT NULL CHECK (
        typeof(runtime_backup_fingerprint) = 'text'
        AND length(CAST(runtime_backup_fingerprint AS BLOB)) = 64
        AND runtime_backup_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    deleted_counts_json TEXT NOT NULL CHECK (
        json_valid(deleted_counts_json) AND json_type(deleted_counts_json) = 'object'
    ),
    candidate_ids_json TEXT NOT NULL CHECK (
        json_valid(candidate_ids_json) AND json_type(candidate_ids_json) = 'array'
        AND json_array_length(candidate_ids_json) = 20
    ),
    old_job_ids_json TEXT NOT NULL CHECK (
        json_valid(old_job_ids_json) AND json_type(old_job_ids_json) = 'array'
        AND json_array_length(old_job_ids_json) = 20
    ),
    new_job_ids_json TEXT NOT NULL CHECK (
        json_valid(new_job_ids_json) AND json_type(new_job_ids_json) = 'array'
        AND json_array_length(new_job_ids_json) = 20
    ),
    completed_at TEXT NOT NULL CHECK (
        length(completed_at) = 27
        AND substr(completed_at, 20, 1) = '.'
        AND substr(completed_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(completed_at, 27, 1) = 'Z'
        AND strftime('%Y-%m-%dT%H:%M:%S', completed_at) IS substr(completed_at, 1, 19)
    ),
    UNIQUE(from_vad_contract_version, to_vad_contract_version)
);

CREATE TRIGGER voice_vad_repairs_no_update BEFORE UPDATE ON voice_vad_repairs
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VAD_REPAIR'); END;
CREATE TRIGGER voice_vad_repairs_no_delete BEFORE DELETE ON voice_vad_repairs
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VAD_REPAIR'); END;
CREATE TRIGGER voice_vad_repairs_no_replace BEFORE INSERT ON voice_vad_repairs
WHEN EXISTS (SELECT 1 FROM voice_vad_repairs)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VAD_REPAIR'); END;

DROP TRIGGER voice_verification_reviews_no_delete;
CREATE TRIGGER voice_verification_reviews_no_delete BEFORE DELETE ON voice_verification_reviews
WHEN presence_vad_repair_delete_authorized('voice_verification_reviews', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REVIEW'); END;

DROP TRIGGER voice_verification_segments_no_delete;
CREATE TRIGGER voice_verification_segments_no_delete BEFORE DELETE ON voice_verification_segments
WHEN presence_vad_repair_delete_authorized('voice_verification_segments', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

DROP TRIGGER voice_verification_runs_no_delete;
CREATE TRIGGER voice_verification_runs_no_delete BEFORE DELETE ON voice_verification_runs
WHEN presence_vad_repair_delete_authorized('voice_verification_runs', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

DROP TRIGGER voice_verification_manifests_no_delete;
CREATE TRIGGER voice_verification_manifests_no_delete BEFORE DELETE ON voice_verification_manifests
WHEN presence_vad_repair_delete_authorized('voice_verification_manifests', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_MANIFEST'); END;

DROP TRIGGER job_events_no_delete;
CREATE TRIGGER job_events_no_delete BEFORE DELETE ON job_events
WHEN presence_vad_repair_delete_authorized('job_events', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

DROP TRIGGER job_unit_attempts_no_delete;
CREATE TRIGGER job_unit_attempts_no_delete BEFORE DELETE ON job_unit_attempts
WHEN presence_vad_repair_delete_authorized('job_unit_attempts', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

DROP TRIGGER video_pipeline_job_bindings_no_delete;
CREATE TRIGGER video_pipeline_job_bindings_no_delete BEFORE DELETE ON video_pipeline_job_bindings
WHEN presence_vad_repair_delete_authorized('video_pipeline_job_bindings', CAST(OLD.job_id AS TEXT) || ':' || CAST(OLD.candidate_id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

DROP TRIGGER video_pipeline_job_binding_sets_no_delete;
CREATE TRIGGER video_pipeline_job_binding_sets_no_delete BEFORE DELETE ON video_pipeline_job_binding_sets
WHEN presence_vad_repair_delete_authorized('video_pipeline_job_binding_sets', CAST(OLD.job_id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

DROP TRIGGER job_units_manifest_no_delete;
CREATE TRIGGER job_units_manifest_no_delete BEFORE DELETE ON job_units
WHEN presence_vad_repair_delete_authorized('job_units', CAST(OLD.job_id AS TEXT) || ':' || OLD.unit_key) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

DROP TRIGGER local_artifacts_no_delete;
CREATE TRIGGER local_artifacts_no_delete BEFORE DELETE ON local_artifacts
WHEN presence_vad_repair_delete_authorized('local_artifacts', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER jobs_no_delete BEFORE DELETE ON jobs
WHEN presence_vad_repair_delete_authorized('jobs', CAST(OLD.id AS TEXT)) IS NOT 1
BEGIN SELECT RAISE(ABORT, 'PRESENCE_VAD_REPAIR_REQUIRED'); END;
