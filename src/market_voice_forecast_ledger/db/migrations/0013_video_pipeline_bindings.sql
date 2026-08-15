CREATE TABLE video_pipeline_job_binding_sets (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    expected_binding_count INTEGER NOT NULL CHECK (expected_binding_count > 0),
    is_sealed INTEGER NOT NULL DEFAULT 0 CHECK (is_sealed IN (0, 1))
);

CREATE TABLE video_pipeline_job_bindings (
    job_id INTEGER NOT NULL REFERENCES video_pipeline_job_binding_sets(job_id),
    eligibility_id INTEGER NOT NULL REFERENCES subject_video_eligibility(id),
    PRIMARY KEY(job_id, eligibility_id)
);

CREATE INDEX video_pipeline_job_bindings_eligibility
ON video_pipeline_job_bindings(eligibility_id, job_id);

CREATE INDEX analysis_run_segments_segment_run
ON analysis_run_segments(segment_id, run_id);

CREATE INDEX analysis_run_segments_policy_run
ON analysis_run_segments(policy_id, run_id);

CREATE TRIGGER video_pipeline_job_binding_sets_require_video_job
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN COALESCE(
    (SELECT job_kind FROM jobs WHERE id = NEW.job_id),
    ''
) != 'video_pipeline'
BEGIN SELECT RAISE(ABORT, 'VIDEO_PIPELINE_JOB_REQUIRED'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_require_open_insert
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN NEW.is_sealed != 0
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_seal_once
BEFORE UPDATE ON video_pipeline_job_binding_sets
WHEN NOT (
    OLD.job_id IS NEW.job_id
    AND OLD.expected_binding_count IS NEW.expected_binding_count
    AND OLD.is_sealed = 0
    AND NEW.is_sealed = 1
    AND (SELECT job_kind FROM jobs WHERE id = OLD.job_id) = 'video_pipeline'
    AND (
        SELECT COUNT(*)
        FROM video_pipeline_job_bindings
        WHERE job_id = OLD.job_id
    ) = OLD.expected_binding_count
    AND (
        SELECT COUNT(DISTINCT eligibility.video_id)
        FROM video_pipeline_job_bindings AS binding
        JOIN subject_video_eligibility AS eligibility
            ON eligibility.id = binding.eligibility_id
        WHERE binding.job_id = OLD.job_id
    ) = 1
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_no_delete
BEFORE DELETE ON video_pipeline_job_binding_sets
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_open_set
BEFORE INSERT ON video_pipeline_job_bindings
WHEN (SELECT job_kind FROM jobs WHERE id = NEW.job_id) = 'video_pipeline'
    AND NOT EXISTS (
        SELECT 1
        FROM video_pipeline_job_binding_sets AS binding_set
        WHERE binding_set.job_id = NEW.job_id
            AND binding_set.is_sealed = 0
            AND (
                SELECT COUNT(*)
                FROM video_pipeline_job_bindings AS existing
                WHERE existing.job_id = NEW.job_id
            ) < binding_set.expected_binding_count
    )
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_video_job
BEFORE INSERT ON video_pipeline_job_bindings
WHEN COALESCE(
    (SELECT job_kind FROM jobs WHERE id = NEW.job_id),
    ''
) != 'video_pipeline'
BEGIN SELECT RAISE(ABORT, 'VIDEO_PIPELINE_JOB_REQUIRED'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_one_video
BEFORE INSERT ON video_pipeline_job_bindings
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_bindings AS existing
    JOIN subject_video_eligibility AS existing_eligibility
        ON existing_eligibility.id = existing.eligibility_id
    JOIN subject_video_eligibility AS new_eligibility
        ON new_eligibility.id = NEW.eligibility_id
    WHERE existing.job_id = NEW.job_id
        AND existing_eligibility.video_id != new_eligibility.video_id
)
BEGIN SELECT RAISE(ABORT, 'BINDING_VIDEO_MISMATCH'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_update
BEFORE UPDATE ON video_pipeline_job_bindings
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_delete
BEFORE DELETE ON video_pipeline_job_bindings
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER bound_video_eligibility_identity_immutable
BEFORE UPDATE OF id, subject_id, video_id ON subject_video_eligibility
WHEN EXISTS (
        SELECT 1
        FROM video_pipeline_job_bindings AS binding
        WHERE binding.eligibility_id = OLD.id
    )
    AND (
        OLD.id IS NOT NEW.id
        OR OLD.subject_id IS NOT NEW.subject_id
        OR OLD.video_id IS NOT NEW.video_id
    )
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER bound_video_eligibility_no_delete
BEFORE DELETE ON subject_video_eligibility
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_bindings AS binding
    WHERE binding.eligibility_id = OLD.id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;
