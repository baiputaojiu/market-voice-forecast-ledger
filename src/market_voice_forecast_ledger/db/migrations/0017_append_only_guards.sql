CREATE TRIGGER schema_migrations_no_replace
BEFORE INSERT ON schema_migrations
WHEN EXISTS (
    SELECT 1 FROM schema_migrations WHERE name = NEW.name
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER audit_events_no_replace
BEFORE INSERT ON audit_events
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM audit_events WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER transcription_chunks_no_replace
BEFORE INSERT ON transcription_chunks
WHEN EXISTS (
    SELECT 1
    FROM transcription_chunks
    WHERE id = NEW.id
        OR (video_id = NEW.video_id AND chunk_no = NEW.chunk_no)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER speaker_threshold_configs_no_replace
BEFORE INSERT ON speaker_threshold_configs
WHEN EXISTS (
    SELECT 1
    FROM speaker_threshold_configs
    WHERE version = NEW.version
        OR (NEW.is_active = 1 AND is_active = 1)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER voice_reference_profiles_no_replace
BEFORE INSERT ON voice_reference_profiles
WHEN EXISTS (
    SELECT 1
    FROM voice_reference_profiles
    WHERE id = NEW.id
        OR (
            NEW.is_active = 1
            AND is_active = 1
            AND subject_id = NEW.subject_id
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER jobs_no_replace
BEFORE INSERT ON jobs
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM jobs WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_units_no_replace
BEFORE INSERT ON job_units
WHEN EXISTS (
    SELECT 1
    FROM job_units
    WHERE job_id = NEW.job_id
        AND (unit_key = NEW.unit_key OR ordinal = NEW.ordinal)
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_MANIFEST'); END;

CREATE TRIGGER job_unit_attempts_no_replace
BEFORE INSERT ON job_unit_attempts
WHEN EXISTS (
    SELECT 1
    FROM job_unit_attempts
    WHERE id = NEW.id
        OR (
            job_id = NEW.job_id
            AND unit_key = NEW.unit_key
            AND attempt_no = NEW.attempt_no
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER job_events_no_replace
BEFORE INSERT ON job_events
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM job_events WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_job_attempts_no_replace
BEFORE INSERT ON analysis_run_job_attempts
WHEN EXISTS (
    SELECT 1
    FROM analysis_run_job_attempts
    WHERE id = NEW.id
        OR job_id = NEW.job_id
        OR (
            run_id = NEW.run_id
            AND attempt_ordinal = NEW.attempt_ordinal
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_events_no_replace
BEFORE INSERT ON analysis_run_events
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM analysis_run_events WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_segments_no_replace
BEFORE INSERT ON analysis_run_segments
WHEN EXISTS (
    SELECT 1
    FROM analysis_run_segments
    WHERE id = NEW.id
        OR (run_id = NEW.run_id AND ordinal = NEW.ordinal)
        OR (run_id = NEW.run_id AND segment_id = NEW.segment_id)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_run_outputs_no_replace
BEFORE INSERT ON analysis_run_outputs
WHEN EXISTS (
    SELECT 1
    FROM analysis_run_outputs
    WHERE id = NEW.id
        OR (run_id = NEW.run_id AND unit_key = NEW.unit_key)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statements_no_replace
BEFORE INSERT ON analysis_statements
WHEN EXISTS (
    SELECT 1
    FROM analysis_statements
    WHERE id = NEW.id
        OR (run_id = NEW.run_id AND ordinal = NEW.ordinal)
        OR (
            run_id = NEW.run_id
            AND batch_ordinal = NEW.batch_ordinal
            AND proposal_ordinal = NEW.proposal_ordinal
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statement_evidence_links_no_replace
BEFORE INSERT ON analysis_statement_evidence_links
WHEN EXISTS (
    SELECT 1
    FROM analysis_statement_evidence_links
    WHERE statement_id = NEW.statement_id
        AND (
            ordinal = NEW.ordinal
            OR run_segment_id = NEW.run_segment_id
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statement_periods_no_replace
BEFORE INSERT ON analysis_statement_periods
WHEN EXISTS (
    SELECT 1
    FROM analysis_statement_periods
    WHERE id = NEW.id OR statement_id = NEW.statement_id
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER period_reviews_no_replace
BEFORE INSERT ON period_reviews
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM period_reviews WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_asset_mappings_no_replace
BEFORE INSERT ON analysis_asset_mappings
WHEN EXISTS (
    SELECT 1
    FROM analysis_asset_mappings
    WHERE id = NEW.id
        OR (
            run_id = NEW.run_id
            AND statement_id = NEW.statement_id
            AND asset = NEW.asset
        )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecasts_logical_no_replace
BEFORE INSERT ON analysis_forecasts
WHEN EXISTS (
    SELECT 1
    FROM analysis_forecasts
    WHERE projection_batch_id = NEW.projection_batch_id
        AND asset = NEW.asset
        AND COALESCE(period_start, '') = COALESCE(NEW.period_start, '')
        AND COALESCE(period_end, '') = COALESCE(NEW.period_end, '')
        AND unknown_period = NEW.unknown_period
        AND condition_kind = NEW.condition_kind
        AND COALESCE(condition_text, '') = COALESCE(NEW.condition_text, '')
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecast_statement_links_ordinal_no_replace
BEFORE INSERT ON analysis_forecast_statement_links
WHEN EXISTS (
    SELECT 1
    FROM analysis_forecast_statement_links
    WHERE forecast_id = NEW.forecast_id
        AND relation_kind = NEW.relation_kind
        AND ordinal = NEW.ordinal
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_no_replace
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_binding_sets
    WHERE job_id = NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_replace
BEFORE INSERT ON video_pipeline_job_bindings
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_bindings
    WHERE job_id = NEW.job_id
        AND eligibility_id = NEW.eligibility_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;
