-- The migration runner validates that every product table is empty before
-- executing this destructive fresh-database reconstruction. The only
-- permitted historical seed is retention_settings(id=1, retention_days=365).

DROP TRIGGER "video_pipeline_job_bindings_no_replace";
DROP TRIGGER "video_pipeline_job_binding_sets_no_replace";
DROP TRIGGER "analysis_forecast_statement_links_ordinal_no_replace";
DROP TRIGGER "analysis_forecasts_logical_no_replace";
DROP TRIGGER "analysis_asset_mappings_no_replace";
DROP TRIGGER "period_reviews_no_replace";
DROP TRIGGER "analysis_statement_periods_no_replace";
DROP TRIGGER "analysis_statement_evidence_links_no_replace";
DROP TRIGGER "analysis_statements_no_replace";
DROP TRIGGER "analysis_run_outputs_no_replace";
DROP TRIGGER "analysis_run_segments_no_replace";
DROP TRIGGER "analysis_run_events_no_replace";
DROP TRIGGER "analysis_run_job_attempts_no_replace";
DROP TRIGGER "job_events_no_replace";
DROP TRIGGER "job_unit_attempts_no_replace";
DROP TRIGGER "job_units_no_replace";
DROP TRIGGER "jobs_no_replace";
DROP TRIGGER "voice_reference_profiles_no_replace";
DROP TRIGGER "speaker_threshold_configs_no_replace";
DROP TRIGGER "transcription_chunks_no_replace";
DROP TRIGGER "audit_events_no_replace";
DROP TRIGGER "analysis_runs_no_replace";
DROP TRIGGER "analysis_runs_scope_generation_match";
DROP TRIGGER "analysis_scopes_no_replace";
DROP TRIGGER "analysis_scopes_generation_monotonic";
DROP TRIGGER "analysis_input_snapshots_no_replace";
DROP TRIGGER "analysis_input_snapshots_limited_update";
DROP TRIGGER "analysis_runs_reject_deleted_source_text";
DROP TRIGGER "transcript_segments_no_replace";
DROP TRIGGER "transcript_segments_no_delete";
DROP TRIGGER "transcript_segments_limited_update";
DROP TRIGGER "local_artifacts_no_replace";
DROP TRIGGER "local_artifacts_no_delete";
DROP TRIGGER "local_artifacts_limited_update";
DROP TRIGGER "retention_settings_no_delete";
DROP TRIGGER "retention_settings_limited_update";
DROP TRIGGER "heatmap_cell_forecasts_no_update";
DROP TRIGGER "heatmap_cells_no_update";
DROP TRIGGER "heatmap_cells_validate_insert";
DROP TRIGGER "bound_video_eligibility_no_delete";
DROP TRIGGER "bound_video_eligibility_identity_immutable";
DROP TRIGGER "video_pipeline_job_bindings_no_delete";
DROP TRIGGER "video_pipeline_job_bindings_no_update";
DROP TRIGGER "video_pipeline_job_bindings_require_one_video";
DROP TRIGGER "video_pipeline_job_bindings_require_video_job";
DROP TRIGGER "video_pipeline_job_bindings_require_open_set";
DROP TRIGGER "video_pipeline_job_binding_sets_no_delete";
DROP TRIGGER "video_pipeline_job_binding_sets_seal_once";
DROP TRIGGER "video_pipeline_job_binding_sets_require_open_insert";
DROP TRIGGER "video_pipeline_job_binding_sets_require_video_job";
DROP TRIGGER "current_forecasts_validate_update";
DROP TRIGGER "current_forecasts_validate_insert";
DROP TRIGGER "current_asset_mappings_validate_update";
DROP TRIGGER "current_asset_mappings_validate_insert";
DROP TRIGGER "current_statements_validate_update";
DROP TRIGGER "current_statements_validate_insert";
DROP TRIGGER "current_result_sets_validate_update";
DROP TRIGGER "current_result_sets_validate_insert";
DROP TRIGGER "analysis_forecast_statement_links_no_delete";
DROP TRIGGER "analysis_forecast_statement_links_no_update";
DROP TRIGGER "analysis_forecasts_no_delete";
DROP TRIGGER "analysis_forecasts_no_update";
DROP TRIGGER "forecast_projection_batches_no_delete";
DROP TRIGGER "forecast_projection_batches_no_update";
DROP TRIGGER "analysis_forecast_statement_links_no_replace";
DROP TRIGGER "analysis_forecasts_no_replace";
DROP TRIGGER "forecast_projection_batches_no_replace";
DROP TRIGGER "analysis_forecast_links_require_same_run";
DROP TRIGGER "analysis_forecasts_require_safe_directions";
DROP TRIGGER "analysis_forecasts_require_batch_run";
DROP TRIGGER "forecast_projection_batches_require_current_review_heads";
DROP TRIGGER "period_reviews_require_positive_latest_id";
DROP TRIGGER "mapping_reviews_no_delete";
DROP TRIGGER "mapping_reviews_no_update";
DROP TRIGGER "mapping_reviews_require_latest_id";
DROP TRIGGER "mapping_reviews_no_replace";
DROP TRIGGER "mapping_reviews_require_consistent_state";
DROP TRIGGER "analysis_asset_mappings_no_delete";
DROP TRIGGER "analysis_asset_mappings_no_update";
DROP TRIGGER "analysis_asset_mappings_require_statement_source";
DROP TRIGGER "analysis_asset_mappings_require_safe_rule_evidence";
DROP TRIGGER "analysis_asset_mappings_require_running_unit";
DROP TRIGGER "period_reviews_no_delete";
DROP TRIGGER "period_reviews_no_update";
DROP TRIGGER "analysis_statement_periods_no_delete";
DROP TRIGGER "analysis_statement_periods_no_update";
DROP TRIGGER "period_reviews_approve_requires_unknown";
DROP TRIGGER "analysis_statement_evidence_links_no_delete";
DROP TRIGGER "analysis_statement_evidence_links_no_update";
DROP TRIGGER "analysis_statements_no_delete";
DROP TRIGGER "analysis_statements_no_update";
DROP TRIGGER "analysis_statement_first_evidence_requires_source_video";
DROP TRIGGER "analysis_statement_evidence_requires_exact_times";
DROP TRIGGER "analysis_statement_evidence_requires_same_run";
DROP TRIGGER "analysis_run_outputs_no_delete";
DROP TRIGGER "analysis_run_outputs_no_update";
DROP TRIGGER "analysis_run_outputs_require_owned_codex_unit";
DROP TRIGGER "analysis_input_snapshots_no_delete";
DROP TRIGGER "analysis_run_segments_no_delete";
DROP TRIGGER "analysis_run_segments_no_update";
DROP TRIGGER "analysis_run_segments_match_video";
DROP TRIGGER "analysis_run_events_no_delete";
DROP TRIGGER "analysis_run_events_no_update";
DROP TRIGGER "analysis_run_job_attempts_no_delete";
DROP TRIGGER "analysis_run_job_attempts_no_update";
DROP TRIGGER "analysis_run_job_attempts_match_job_source";
DROP TRIGGER "analysis_run_job_attempts_require_analysis_job";
DROP TRIGGER "analysis_runs_no_delete";
DROP TRIGGER "analysis_runs_no_update";
DROP TRIGGER "job_events_no_delete";
DROP TRIGGER "job_events_no_update";
DROP TRIGGER "job_unit_attempts_no_delete";
DROP TRIGGER "job_unit_attempts_no_update";
DROP TRIGGER "job_units_manifest_no_delete";
DROP TRIGGER "job_units_manifest_no_extra_insert";
DROP TRIGGER "job_units_input_binding_immutable";
DROP TRIGGER "job_units_manifest_immutable";
DROP TRIGGER "jobs_manifest_immutable";
DROP TRIGGER "audit_events_no_delete";
DROP TRIGGER "audit_events_no_update";
DROP INDEX "analysis_run_segments_video_run";
DROP INDEX "heatmap_cell_forecasts_scope_source";
DROP INDEX "heatmap_cells_scope_granularity";
DROP INDEX "current_forecasts_cache_owner";
DROP INDEX "analysis_run_segments_policy_run";
DROP INDEX "analysis_run_segments_segment_run";
DROP INDEX "video_pipeline_job_bindings_eligibility";
DROP INDEX "analysis_forecasts_batch_id";
DROP INDEX "analysis_forecasts_batch_group";
DROP INDEX "mapping_reviews_mapping_id_id";
DROP INDEX "analysis_asset_mappings_run_statement";
DROP INDEX "period_reviews_period_id_id";
DROP INDEX "analysis_statement_evidence_run_segment";
DROP INDEX "analysis_statements_run_ordinal";
DROP INDEX "analysis_run_outputs_run_ordinal";
DROP INDEX "analysis_run_segments_run_id";
DROP INDEX "analysis_run_events_run_id";
DROP INDEX "analysis_runs_scope_id";
DROP INDEX "job_events_job_id";
DROP INDEX "job_units_status_ordinal";
DROP INDEX "one_active_voice_reference_profile_per_subject";
DROP INDEX "one_active_speaker_threshold_config";
DROP TABLE "local_artifacts";
DROP TABLE "retention_deletion_previews";
DROP TABLE "retention_settings";
DROP TABLE "heatmap_cell_forecasts";
DROP TABLE "heatmap_cells";
DROP TABLE "video_pipeline_job_bindings";
DROP TABLE "video_pipeline_job_binding_sets";
DROP TABLE "current_forecasts";
DROP TABLE "current_asset_mappings";
DROP TABLE "current_statements";
DROP TABLE "current_result_sets";
DROP TABLE "analysis_forecast_statement_links";
DROP TABLE "analysis_forecasts";
DROP TABLE "forecast_projection_batches";
DROP TABLE "mapping_reviews";
DROP TABLE "analysis_asset_mappings";
DROP TABLE "period_reviews";
DROP TABLE "analysis_statement_periods";
DROP TABLE "analysis_statement_evidence_links";
DROP TABLE "analysis_statements";
DROP TABLE "analysis_run_outputs";
DROP TABLE "analysis_input_snapshots";
DROP TABLE "analysis_run_segments";
DROP TABLE "analysis_run_events";
DROP TABLE "analysis_run_job_attempts";
DROP TABLE "analysis_runs";
DROP TABLE "analysis_scopes";
DROP TABLE "job_events";
DROP TABLE "job_unit_attempts";
DROP TABLE "job_units";
DROP TABLE "jobs";
DROP TABLE "speaker_assignments";
DROP TABLE "voice_reference_profiles";
DROP TABLE "speaker_threshold_configs";
DROP TABLE "transcript_segments";
DROP TABLE "transcription_chunks";
DROP TABLE "subject_video_eligibility";
DROP TABLE "videos";
DROP TABLE "subject_channel_policies";
DROP TABLE "subject_aliases";
DROP TABLE "analysis_subjects";
DROP TABLE "audit_events";
DROP TABLE "app_metadata";

CREATE TABLE app_metadata(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

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

CREATE TABLE analysis_subjects (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE subject_aliases (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    alias TEXT NOT NULL,
    UNIQUE(subject_id, alias)
);

CREATE TABLE videos (
    id INTEGER PRIMARY KEY,
    youtube_video_id TEXT NOT NULL UNIQUE,
    current_metadata_snapshot_id INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(current_metadata_snapshot_id) REFERENCES video_metadata_snapshots(id)
);

CREATE TABLE transcription_chunks (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    chunk_no INTEGER NOT NULL CHECK (chunk_no >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed')),
    CHECK (start_ms < end_ms),
    UNIQUE(video_id, chunk_no),
    UNIQUE(id, video_id)
);

CREATE TABLE transcript_segments (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    chunk_id INTEGER NOT NULL,
    segment_no INTEGER NOT NULL CHECK (segment_no >= 0),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL,
    text_body TEXT,
    text_sha256 TEXT NOT NULL,
    anonymous_speaker_id TEXT NOT NULL,
    transcript_created_at TEXT NOT NULL,
    expires_at TEXT,
    text_deleted_at TEXT,
    CHECK (start_ms < end_ms),
    CHECK (
        (text_body IS NOT NULL AND text_deleted_at IS NULL)
        OR (text_body IS NULL AND text_deleted_at IS NOT NULL)
    ),
    UNIQUE(video_id, segment_no),
    FOREIGN KEY(chunk_id, video_id) REFERENCES transcription_chunks(id, video_id)
);

CREATE TABLE speaker_threshold_configs (
    version TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    subject_operator TEXT NOT NULL CHECK (subject_operator = 'gte'),
    subject_boundary REAL NOT NULL,
    interviewer_operator TEXT NOT NULL CHECK (interviewer_operator = 'lte'),
    interviewer_boundary REAL NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    CHECK (subject_boundary > interviewer_boundary)
);

CREATE TABLE voice_reference_profiles (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    threshold_config_version TEXT NOT NULL REFERENCES speaker_threshold_configs(version),
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1))
);

CREATE TABLE speaker_assignments (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL UNIQUE REFERENCES transcript_segments(id),
    assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('subject', 'interviewer', 'hold')),
    assigned_subject_id INTEGER REFERENCES analysis_subjects(id),
    assignment_origin TEXT NOT NULL CHECK (assignment_origin IN ('auto_voice', 'manual')),
    raw_match_score REAL,
    model_name TEXT,
    model_version TEXT,
    threshold_config_version TEXT REFERENCES speaker_threshold_configs(version),
    evidence_hash TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    CHECK (
        (assignment_kind = 'subject' AND assigned_subject_id IS NOT NULL)
        OR (assignment_kind IN ('interviewer', 'hold') AND assigned_subject_id IS NULL)
    ),
    CHECK (
        assignment_origin != 'auto_voice'
        OR (
            raw_match_score IS NOT NULL
            AND model_name IS NOT NULL
            AND model_version IS NOT NULL
            AND threshold_config_version IS NOT NULL
        )
    )
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    source_job_id INTEGER REFERENCES jobs(id),
    job_kind TEXT NOT NULL CHECK (job_kind IN ('video_pipeline', 'analysis_scope', 'youtube_sync')),
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
        'heatmap_update',
        'youtube_seed_discovery',
        'youtube_search_discovery',
        'youtube_manual_discovery'
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

CREATE TABLE analysis_scopes (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    cutoff_day_jst TEXT NOT NULL,
    cutoff_exclusive_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'ready', 'running', 'current', 'stale', 'failed'
    )),
    stale_reason TEXT, generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(generation) = 'integer' AND generation >= 0
),
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
, scope_generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(scope_generation) = 'integer' AND scope_generation >= 0
));

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
    metadata_snapshot_id INTEGER NOT NULL REFERENCES video_metadata_snapshots(id),
    metadata_snapshot_hash TEXT NOT NULL,
    presence_decision_id INTEGER NOT NULL REFERENCES presence_decisions(id),
    presence_decision_hash TEXT NOT NULL,
    speaker_assignment_id INTEGER NOT NULL REFERENCES speaker_assignments(id),
    assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('subject', 'interviewer', 'hold')),
    assigned_subject_id INTEGER REFERENCES analysis_subjects(id),
    assignment_updated_at TEXT NOT NULL,
    assignment_evidence_hash TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, segment_id),
    CHECK (
        (assignment_kind = 'subject' AND assigned_subject_id IS NOT NULL)
        OR (assignment_kind IN ('interviewer', 'hold') AND assigned_subject_id IS NULL)
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

CREATE TABLE analysis_statements (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal > 0),
    proposal_ordinal INTEGER NOT NULL CHECK (proposal_ordinal > 0),
    source_video_id INTEGER NOT NULL REFERENCES videos(id),
    statement_type TEXT NOT NULL CHECK (statement_type IN (
        'future_forecast',
        'current_analysis',
        'past_result_analysis',
        'general_statement'
    )),
    forecast_basis TEXT CHECK (forecast_basis IN (
        'direct', 'inferred_from_subject_statements'
    )),
    condition_kind TEXT NOT NULL CHECK (condition_kind IN (
        'unconditional', 'conditional'
    )),
    condition_text TEXT,
    direction_kind TEXT CHECK (direction_kind IN (
        'strong_up',
        'up',
        'flat',
        'down',
        'strong_down',
        'turning_point',
        'unknown'
    )),
    turning_point_kind TEXT CHECK (turning_point_kind IN (
        'bottom', 'top', 'other'
    )),
    original_target_expression TEXT NOT NULL,
    original_period_expression TEXT,
    heatmap_candidate INTEGER NOT NULL CHECK (heatmap_candidate IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, batch_ordinal, proposal_ordinal),
    CHECK (
        (statement_type = 'future_forecast' AND forecast_basis IS NOT NULL)
        OR (statement_type != 'future_forecast' AND forecast_basis IS NULL)
    ),
    CHECK (statement_type != 'future_forecast' OR direction_kind IS NOT NULL),
    CHECK (
        condition_kind != 'conditional'
        OR (condition_text IS NOT NULL AND length(condition_text) > 0)
    ),
    CHECK (
        direction_kind != 'turning_point' OR turning_point_kind IS NOT NULL
    ),
    CHECK (
        (statement_type = 'future_forecast' AND heatmap_candidate = 1)
        OR (statement_type != 'future_forecast' AND heatmap_candidate = 0)
    )
);

CREATE TABLE analysis_statement_evidence_links (
    statement_id INTEGER NOT NULL REFERENCES analysis_statements(id),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    run_segment_id INTEGER NOT NULL REFERENCES analysis_run_segments(id),
    excerpt TEXT NOT NULL CHECK (
        length(excerpt) > 0 AND length(excerpt) <= 300
    ),
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL,
    CHECK (start_ms < end_ms),
    UNIQUE(statement_id, ordinal),
    UNIQUE(statement_id, run_segment_id)
);

CREATE TABLE analysis_statement_periods (
    id INTEGER PRIMARY KEY,
    statement_id INTEGER NOT NULL UNIQUE REFERENCES analysis_statements(id),
    source_expression TEXT,
    start_date TEXT,
    end_date TEXT,
    time_basis TEXT CHECK (time_basis IN (
        'explicit_statement', 'published_at'
    )),
    basis_published_at TEXT,
    is_unknown INTEGER NOT NULL CHECK (is_unknown IN (0, 1)),
    CHECK (
        (
            is_unknown = 1
            AND start_date IS NULL
            AND end_date IS NULL
            AND time_basis IS NULL
            AND basis_published_at IS NULL
        )
        OR (
            is_unknown = 0
            AND start_date IS NOT NULL
            AND end_date IS NOT NULL
            AND start_date <= end_date
            AND time_basis IS NOT NULL
            AND (
                (time_basis = 'explicit_statement'
                    AND basis_published_at IS NULL)
                OR (time_basis = 'published_at'
                    AND basis_published_at IS NOT NULL)
            )
        )
    )
);

CREATE TABLE period_reviews (
    id INTEGER PRIMARY KEY,
    period_id INTEGER NOT NULL REFERENCES analysis_statement_periods(id),
    decision TEXT NOT NULL CHECK (decision IN ('approve_unknown', 'reject')),
    actor TEXT NOT NULL CHECK (length(actor) > 0),
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE analysis_asset_mappings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    statement_id INTEGER NOT NULL REFERENCES analysis_statements(id),
    original_expression TEXT NOT NULL,
    asset TEXT NOT NULL CHECK (asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('direct', 'inferred')),
    conversion_reason TEXT NOT NULL CHECK (
        length(conversion_reason) BETWEEN 1 AND 64
        AND conversion_reason NOT GLOB '*[^a-z0-9_]*'
    ),
    codex_confidence TEXT NOT NULL CHECK (codex_confidence IN (
        'high', 'medium', 'low', 'unresolved'
    )),
    rule_confidence TEXT NOT NULL CHECK (rule_confidence IN (
        'high', 'medium', 'low', 'unresolved'
    )),
    final_confidence TEXT NOT NULL CHECK (final_confidence IN (
        'high', 'medium', 'low', 'unresolved'
    )),
    confidence_disagrees INTEGER NOT NULL CHECK (
        confidence_disagrees IN (0, 1)
        AND confidence_disagrees = CASE
            WHEN codex_confidence != rule_confidence THEN 1
            ELSE 0
        END
    ),
    rule_evidence_json TEXT NOT NULL CHECK (
        json_valid(rule_evidence_json)
        AND json_type(rule_evidence_json) = 'array'
    ),
    source_video_id INTEGER NOT NULL REFERENCES videos(id),
    UNIQUE(run_id, statement_id, asset),
    CHECK (
        final_confidence = CASE
            WHEN codex_confidence = 'unresolved'
                OR rule_confidence = 'unresolved' THEN 'unresolved'
            WHEN codex_confidence = 'low'
                OR rule_confidence = 'low' THEN 'low'
            WHEN codex_confidence = 'medium'
                OR rule_confidence = 'medium' THEN 'medium'
            ELSE 'high'
        END
    )
);

CREATE TABLE mapping_reviews (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    mapping_id INTEGER NOT NULL REFERENCES analysis_asset_mappings(id),
    decision TEXT NOT NULL CHECK (decision IN (
        'approve', 'correct', 'reject'
    )),
    actor TEXT NOT NULL CHECK (actor IN ('user', 'system')),
    reason TEXT NOT NULL CHECK (
        length(trim(
            reason,
            char(
                9, 10, 11, 12, 13,
                28, 29, 30, 31, 32,
                133, 160, 5760,
                8192, 8193, 8194, 8195, 8196, 8197,
                8198, 8199, 8200, 8201, 8202,
                8232, 8233, 8239, 8287, 12288
            )
        )) > 0
    ),
    before_asset TEXT NOT NULL CHECK (before_asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    after_asset TEXT NOT NULL CHECK (after_asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    created_at TEXT NOT NULL,
    CHECK (
        (decision = 'approve')
        OR (decision = 'correct' AND before_asset != after_asset)
        OR (decision = 'reject' AND before_asset = after_asset)
    )
);

CREATE TABLE forecast_projection_batches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    trigger_kind TEXT NOT NULL CHECK (trigger_kind IN (
        'initial', 'mapping_review', 'period_review'
    )),
    latest_mapping_review_id INTEGER REFERENCES mapping_reviews(id),
    latest_period_review_id INTEGER REFERENCES period_reviews(id),
    created_at TEXT NOT NULL CHECK (
        length(created_at) = 27
        AND strftime('%Y-%m-%dT%H:%M:%S', created_at) = substr(created_at, 1, 19)
        AND substr(created_at, 20, 1) = '.'
        AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(created_at, 27, 1) = 'Z'
    )
);

CREATE TABLE analysis_forecasts (
    id INTEGER PRIMARY KEY,
    projection_batch_id INTEGER NOT NULL
        REFERENCES forecast_projection_batches(id),
    run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    asset TEXT NOT NULL CHECK (asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('direct', 'inferred')),
    period_start TEXT,
    period_end TEXT,
    unknown_period INTEGER NOT NULL CHECK (unknown_period IN (0, 1)),
    condition_kind TEXT NOT NULL CHECK (condition_kind IN (
        'unconditional', 'conditional'
    )),
    condition_text TEXT,
    view_relation TEXT NOT NULL CHECK (view_relation IN (
        'current', 'changed', 'disagreement'
    )),
    primary_direction TEXT NOT NULL CHECK (primary_direction IN (
        'strong_up', 'up', 'flat', 'down', 'strong_down',
        'turning_point', 'unknown'
    )),
    directions_json TEXT NOT NULL CHECK (
        json_valid(directions_json)
        AND json_type(directions_json) = 'array'
        AND json_array_length(directions_json) > 0
        AND json(directions_json) = directions_json
    ),
    confidence TEXT NOT NULL CHECK (confidence IN (
        'high', 'medium', 'low', 'unresolved'
    )),
    evidence_count INTEGER NOT NULL CHECK (evidence_count > 0),
    selected_published_at TEXT NOT NULL CHECK (
        length(selected_published_at) = 27
        AND strftime(
            '%Y-%m-%dT%H:%M:%S', selected_published_at
        ) = substr(selected_published_at, 1, 19)
        AND substr(selected_published_at, 20, 1) = '.'
        AND substr(selected_published_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(selected_published_at, 27, 1) = 'Z'
    ),
    selected_forecast_basis TEXT NOT NULL CHECK (selected_forecast_basis IN (
        'direct', 'inferred_from_subject_statements'
    )),
    period_specificity INTEGER NOT NULL CHECK (
        period_specificity BETWEEN 0 AND 3
    ),
    stable_selection_key TEXT NOT NULL CHECK (length(stable_selection_key) > 0),
    heatmap_eligible INTEGER NOT NULL CHECK (heatmap_eligible IN (0, 1)),
    exclusion_reason TEXT,
    CHECK (
        (
            unknown_period = 1
            AND period_start IS NULL
            AND period_end IS NULL
            AND period_specificity = 0
        )
        OR (
            unknown_period = 0
            AND period_start IS NOT NULL
            AND period_end IS NOT NULL
            AND date(period_start) = period_start
            AND date(period_end) = period_end
            AND period_start <= period_end
            AND period_specificity = CASE
                WHEN julianday(period_end) - julianday(period_start) + 1 <= 7
                    THEN 3
                WHEN julianday(period_end) - julianday(period_start) + 1 <= 31
                    THEN 2
                ELSE 1
            END
        )
    ),
    CHECK (
        (condition_kind = 'unconditional' AND condition_text IS NULL)
        OR (
            condition_kind = 'conditional'
            AND condition_text IS NOT NULL
            AND length(condition_text) > 0
        )
    ),
    CHECK (
        (heatmap_eligible = 1 AND exclusion_reason IS NULL)
        OR (
            heatmap_eligible = 0
            AND exclusion_reason IS NOT NULL
            AND length(exclusion_reason) > 0
        )
    )
);

CREATE TABLE analysis_forecast_statement_links (
    forecast_id INTEGER NOT NULL REFERENCES analysis_forecasts(id),
    statement_id INTEGER NOT NULL REFERENCES analysis_statements(id),
    relation_kind TEXT NOT NULL CHECK (relation_kind IN (
        'supporting', 'counterevidence'
    )),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY(forecast_id, statement_id),
    UNIQUE(forecast_id, relation_kind, ordinal)
);

CREATE TABLE current_result_sets (
    scope_id INTEGER PRIMARY KEY REFERENCES analysis_scopes(id),
    source_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    projection_batch_id INTEGER NOT NULL
        REFERENCES forecast_projection_batches(id),
    UNIQUE(scope_id, source_run_id),
    UNIQUE(scope_id, source_run_id, projection_batch_id)
);

CREATE TABLE current_statements (
    scope_id INTEGER NOT NULL,
    analysis_statement_id INTEGER NOT NULL REFERENCES analysis_statements(id),
    source_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    PRIMARY KEY(scope_id, analysis_statement_id),
    FOREIGN KEY(scope_id, source_run_id)
        REFERENCES current_result_sets(scope_id, source_run_id)
);

CREATE TABLE current_asset_mappings (
    scope_id INTEGER NOT NULL,
    analysis_mapping_id INTEGER NOT NULL
        REFERENCES analysis_asset_mappings(id),
    source_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    effective_asset TEXT NOT NULL CHECK (effective_asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    effective_eligibility INTEGER NOT NULL CHECK (
        effective_eligibility IN (0, 1)
    ),
    PRIMARY KEY(scope_id, analysis_mapping_id),
    FOREIGN KEY(scope_id, source_run_id)
        REFERENCES current_result_sets(scope_id, source_run_id)
);

CREATE TABLE current_forecasts (
    scope_id INTEGER NOT NULL,
    analysis_forecast_id INTEGER NOT NULL REFERENCES analysis_forecasts(id),
    source_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    projection_batch_id INTEGER NOT NULL
        REFERENCES forecast_projection_batches(id),
    PRIMARY KEY(scope_id, analysis_forecast_id),
    FOREIGN KEY(scope_id, source_run_id, projection_batch_id)
        REFERENCES current_result_sets(
            scope_id, source_run_id, projection_batch_id
        )
);

CREATE TABLE video_pipeline_job_binding_sets (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    expected_binding_count INTEGER NOT NULL CHECK (expected_binding_count > 0),
    is_sealed INTEGER NOT NULL DEFAULT 0 CHECK (is_sealed IN (0, 1))
);

CREATE TABLE video_pipeline_job_bindings (
    job_id INTEGER NOT NULL REFERENCES video_pipeline_job_binding_sets(job_id),
    candidate_id INTEGER NOT NULL REFERENCES subject_video_candidates(id),
    PRIMARY KEY(job_id, candidate_id)
);

CREATE TABLE heatmap_cells (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    source_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    projection_batch_id INTEGER NOT NULL
        REFERENCES forecast_projection_batches(id),
    granularity TEXT NOT NULL CHECK (granularity IN ('week', 'month')),
    period_key TEXT NOT NULL,
    slot_start TEXT,
    slot_end TEXT,
    unknown_period INTEGER NOT NULL CHECK (unknown_period IN (0, 1)),
    asset TEXT NOT NULL CHECK (asset IN (
        'nikkei_225', 'topix', 'sp500', 'xau_usd'
    )),
    condition_kind TEXT NOT NULL CHECK (condition_kind IN (
        'unconditional', 'conditional'
    )),
    condition_texts_json TEXT NOT NULL CHECK (
        json_valid(condition_texts_json)
        AND json_type(condition_texts_json) = 'array'
        AND json(condition_texts_json) = condition_texts_json
    ),
    primary_direction TEXT NOT NULL CHECK (primary_direction IN (
        'strong_up', 'up', 'flat', 'down', 'strong_down',
        'turning_point', 'unknown'
    )),
    directions_json TEXT NOT NULL CHECK (
        json_valid(directions_json)
        AND json_type(directions_json) = 'array'
        AND json_array_length(directions_json) > 0
        AND json(directions_json) = directions_json
    ),
    view_relation TEXT NOT NULL CHECK (view_relation IN (
        'current', 'changed', 'disagreement'
    )),
    selected_published_at TEXT NOT NULL CHECK (
        length(selected_published_at) = 27
        AND strftime(
            '%Y-%m-%dT%H:%M:%S', selected_published_at
        ) = substr(selected_published_at, 1, 19)
        AND substr(selected_published_at, 20, 1) = '.'
        AND substr(selected_published_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(selected_published_at, 27, 1) = 'Z'
    ),
    selected_forecast_basis TEXT NOT NULL CHECK (
        selected_forecast_basis IN (
            'direct', 'inferred_from_subject_statements'
        )
    ),
    period_specificity INTEGER NOT NULL CHECK (
        period_specificity BETWEEN 0 AND 3
    ),
    mapping_kind TEXT NOT NULL CHECK (
        mapping_kind IN ('direct', 'inferred')
    ),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('high', 'medium', 'low', 'unresolved')
    ),
    evidence_count INTEGER NOT NULL CHECK (evidence_count > 0),
    supporting_statement_ids_json TEXT NOT NULL CHECK (
        json_valid(supporting_statement_ids_json)
        AND json_type(supporting_statement_ids_json) = 'array'
        AND json_array_length(supporting_statement_ids_json) > 0
        AND json(supporting_statement_ids_json) = supporting_statement_ids_json
    ),
    counterevidence_statement_ids_json TEXT NOT NULL CHECK (
        json_valid(counterevidence_statement_ids_json)
        AND json_type(counterevidence_statement_ids_json) = 'array'
        AND json(counterevidence_statement_ids_json)
            = counterevidence_statement_ids_json
    ),
    UNIQUE(
        scope_id, subject_id, asset, granularity, period_key, condition_kind
    ),
    UNIQUE(id, scope_id, source_run_id, projection_batch_id),
    FOREIGN KEY(scope_id, source_run_id, projection_batch_id)
        REFERENCES current_result_sets(
            scope_id, source_run_id, projection_batch_id
        ) ON DELETE CASCADE,
    CHECK (
        (
            unknown_period = 1
            AND period_key = 'unknown'
            AND slot_start IS NULL
            AND slot_end IS NULL
            AND period_specificity = 0
        )
        OR (
            unknown_period = 0
            AND slot_start IS NOT NULL
            AND slot_end IS NOT NULL
            AND date(slot_start) = slot_start
            AND date(slot_end) = slot_end
            AND (
                (
                    granularity = 'week'
                    AND strftime('%w', slot_start) = '1'
                    AND slot_end = date(slot_start, '+6 days')
                    AND period_key = slot_start || '/' || slot_end
                )
                OR (
                    granularity = 'month'
                    AND substr(slot_start, 9, 2) = '01'
                    AND slot_end = date(slot_start, '+1 month', '-1 day')
                    AND period_key = substr(slot_start, 1, 7)
                )
            )
        )
    ),
    CHECK (
        (condition_kind = 'unconditional' AND condition_texts_json = '[]')
        OR (
            condition_kind = 'conditional'
            AND json_array_length(condition_texts_json) > 0
        )
    )
);

CREATE TABLE heatmap_cell_forecasts (
    heatmap_cell_id INTEGER NOT NULL,
    scope_id INTEGER NOT NULL,
    source_run_id INTEGER NOT NULL,
    projection_batch_id INTEGER NOT NULL,
    source_forecast_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY(heatmap_cell_id, source_forecast_id),
    UNIQUE(heatmap_cell_id, ordinal),
    FOREIGN KEY(
        heatmap_cell_id, scope_id, source_run_id, projection_batch_id
    ) REFERENCES heatmap_cells(
        id, scope_id, source_run_id, projection_batch_id
    ) ON DELETE CASCADE,
    FOREIGN KEY(
        scope_id, source_forecast_id, source_run_id, projection_batch_id
    ) REFERENCES current_forecasts(
        scope_id, analysis_forecast_id, source_run_id, projection_batch_id
    ) ON DELETE CASCADE
);

CREATE TABLE retention_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    retention_days INTEGER CHECK (
        retention_days IN (30, 90, 180, 365)
        OR retention_days IS NULL
    )
);

CREATE TABLE retention_deletion_previews (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    token TEXT NOT NULL UNIQUE CHECK (
        length(token) = 64
        AND token NOT GLOB '*[^0-9a-f]*'
    ),
    cutoff_utc TEXT NOT NULL CHECK (
        COALESCE(
            length(cutoff_utc) = 27
            AND strftime('%Y-%m-%dT%H:%M:%S', cutoff_utc)
                = substr(cutoff_utc, 1, 19)
            AND CAST(substr(cutoff_utc, 1, 4) AS INTEGER)
                BETWEEN 1 AND 9999
            AND CAST(substr(cutoff_utc, 12, 2) AS INTEGER)
                BETWEEN 0 AND 23
            AND substr(cutoff_utc, 20, 1) = '.'
            AND substr(cutoff_utc, 21, 6) NOT GLOB '*[^0-9]*'
            AND substr(cutoff_utc, 27, 1) = 'Z',
            0
        )
    ),
    retention_days INTEGER CHECK (
        retention_days IN (30, 90, 180, 365)
        OR retention_days IS NULL
    ),
    target_fingerprint TEXT NOT NULL CHECK (
        length(target_fingerprint) = 64
        AND target_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (
        COALESCE(
            length(created_at) = 27
            AND strftime('%Y-%m-%dT%H:%M:%S', created_at)
                = substr(created_at, 1, 19)
            AND CAST(substr(created_at, 1, 4) AS INTEGER)
                BETWEEN 1 AND 9999
            AND CAST(substr(created_at, 12, 2) AS INTEGER)
                BETWEEN 0 AND 23
            AND substr(created_at, 20, 1) = '.'
            AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND substr(created_at, 27, 1) = 'Z',
            0
        )
    ),
    expires_at TEXT NOT NULL CHECK (
        COALESCE(
            length(expires_at) = 27
            AND strftime('%Y-%m-%dT%H:%M:%S', expires_at)
                = substr(expires_at, 1, 19)
            AND CAST(substr(expires_at, 1, 4) AS INTEGER)
                BETWEEN 1 AND 9999
            AND CAST(substr(expires_at, 12, 2) AS INTEGER)
                BETWEEN 0 AND 23
            AND substr(expires_at, 20, 1) = '.'
            AND substr(expires_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND substr(expires_at, 27, 1) = 'Z'
            AND created_at < expires_at,
            0
        )
    )
);

CREATE TABLE local_artifacts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind = 'audio'),
    local_path TEXT NOT NULL CHECK (length(local_path) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'delete_failed', 'deleted')
    ),
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    safe_error_code TEXT CHECK (safe_error_code IN (
        'AUDIO_PATH_OUTSIDE_TEMP_ROOT',
        'AUDIO_DELETE_PERMISSION',
        'AUDIO_DELETE_OS_ERROR'
    )),
    created_at TEXT NOT NULL CHECK (
        COALESCE(
            length(created_at) = 27
            AND strftime('%Y-%m-%dT%H:%M:%S', created_at)
                = substr(created_at, 1, 19)
            AND CAST(substr(created_at, 1, 4) AS INTEGER)
                BETWEEN 1 AND 9999
            AND CAST(substr(created_at, 12, 2) AS INTEGER)
                BETWEEN 0 AND 23
            AND substr(created_at, 20, 1) = '.'
            AND substr(created_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND substr(created_at, 27, 1) = 'Z',
            0
        )
    ),
    deleted_at TEXT CHECK (
        deleted_at IS NULL
        OR COALESCE(
            length(deleted_at) = 27
            AND strftime('%Y-%m-%dT%H:%M:%S', deleted_at)
                = substr(deleted_at, 1, 19)
            AND CAST(substr(deleted_at, 1, 4) AS INTEGER)
                BETWEEN 1 AND 9999
            AND CAST(substr(deleted_at, 12, 2) AS INTEGER)
                BETWEEN 0 AND 23
            AND substr(deleted_at, 20, 1) = '.'
            AND substr(deleted_at, 21, 6) NOT GLOB '*[^0-9]*'
            AND substr(deleted_at, 27, 1) = 'Z',
            0
        )
    ),
    CHECK (
        (
            status = 'pending'
            AND retry_count = 0
            AND safe_error_code IS NULL
            AND deleted_at IS NULL
        )
        OR (
            status = 'delete_failed'
            AND retry_count > 0
            AND safe_error_code IS NOT NULL
            AND deleted_at IS NULL
        )
        OR (
            status = 'deleted'
            AND safe_error_code IS NULL
            AND deleted_at IS NOT NULL
        )
    )
);

CREATE TABLE discovery_profiles (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL UNIQUE REFERENCES analysis_subjects(id),
    current_version_id INTEGER,
    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(current_version_id) REFERENCES discovery_profile_versions(id)
);

CREATE TABLE discovery_profile_versions (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, config_hash),
    UNIQUE(id, profile_id, config_hash)
);

CREATE TABLE discovery_seed_channels (
    profile_version_id INTEGER NOT NULL REFERENCES discovery_profile_versions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    youtube_channel_id TEXT NOT NULL,
    PRIMARY KEY(profile_version_id, ordinal),
    UNIQUE(profile_version_id, youtube_channel_id)
);

CREATE TABLE discovery_search_terms (
    profile_version_id INTEGER NOT NULL REFERENCES discovery_profile_versions(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    search_term TEXT NOT NULL,
    PRIMARY KEY(profile_version_id, ordinal),
    UNIQUE(profile_version_id, search_term)
);

CREATE TABLE video_metadata_snapshots (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    youtube_video_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_title TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    published_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
    live_state TEXT NOT NULL CHECK (live_state IN ('not_live', 'live', 'upcoming')),
    actual_start_time TEXT,
    schema_version TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(video_id, canonical_hash),
    UNIQUE(id, video_id, canonical_hash)
);

CREATE TABLE discovery_observations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    metadata_snapshot_id INTEGER NOT NULL,
    metadata_snapshot_hash TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('seed_uploads', 'cross_channel_search', 'manual_url')),
    source_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observation_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    UNIQUE(id, profile_id, video_id),
    FOREIGN KEY(metadata_snapshot_id, video_id, metadata_snapshot_hash)
        REFERENCES video_metadata_snapshots(id, video_id, canonical_hash)
);

CREATE TABLE subject_video_candidates (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    first_observation_id INTEGER NOT NULL,
    current_presence_decision_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, video_id),
    FOREIGN KEY(first_observation_id, profile_id, video_id)
        REFERENCES discovery_observations(id, profile_id, video_id),
    FOREIGN KEY(current_presence_decision_id) REFERENCES presence_decisions(id)
);

CREATE TABLE presence_decisions (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES subject_video_candidates(id),
    state TEXT NOT NULL CHECK (state IN ('presence_unverified', 'presence_confirmed', 'presence_rejected')),
    decision_origin TEXT NOT NULL CHECK (decision_origin IN ('collection_initial', 'voice_verification')),
    evidence_ref TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, decision_hash)
);

CREATE TABLE manual_discovery_requests (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    youtube_video_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    UNIQUE(profile_id, youtube_video_id)
);

CREATE TABLE youtube_sync_manifests (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    sync_kind TEXT NOT NULL CHECK (sync_kind IN ('full_discovery', 'manual')),
    upper_bound TEXT NOT NULL,
    backfill_floor TEXT NOT NULL,
    quota_contract_version TEXT NOT NULL,
    profile_set_hash TEXT NOT NULL,
    manual_request_id INTEGER REFERENCES manual_discovery_requests(id),
    resume_not_before_utc TEXT,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((sync_kind='full_discovery' AND manual_request_id IS NULL) OR (sync_kind='manual' AND manual_request_id IS NOT NULL))
);

CREATE TABLE youtube_sync_manifest_profiles (
    job_id INTEGER NOT NULL REFERENCES youtube_sync_manifests(job_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    profile_version_id INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    discoverer_set_hash TEXT NOT NULL,
    PRIMARY KEY(job_id, ordinal),
    UNIQUE(job_id, profile_id),
    FOREIGN KEY(profile_version_id, profile_id, config_hash)
        REFERENCES discovery_profile_versions(id, profile_id, config_hash)
);

CREATE TABLE youtube_sync_checkpoints (
    job_id INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('seed_uploads', 'cross_channel_search', 'manual_url')),
    source_key TEXT NOT NULL,
    effective_lower_bound TEXT NOT NULL,
    upper_bound TEXT NOT NULL,
    uploads_playlist_id TEXT,
    next_page_token TEXT,
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal >= 0),
    completed_at TEXT,
    checkpoint_hash TEXT NOT NULL,
    PRIMARY KEY(job_id, unit_key),
    FOREIGN KEY(job_id, unit_key) REFERENCES job_units(job_id, unit_key)
);

CREATE TABLE youtube_search_windows (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    lower_bound TEXT NOT NULL,
    upper_bound TEXT NOT NULL,
    next_page_token TEXT,
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    split_parent_id INTEGER REFERENCES youtube_search_windows(id),
    completed_at TEXT,
    window_hash TEXT NOT NULL,
    UNIQUE(job_id, unit_key, ordinal),
    FOREIGN KEY(job_id, unit_key) REFERENCES youtube_sync_checkpoints(job_id, unit_key)
);

CREATE TABLE youtube_source_cursors (
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('seed_uploads', 'cross_channel_search')),
    source_key TEXT NOT NULL,
    completed_upper_bound TEXT NOT NULL,
    cursor_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(profile_id, source_kind, source_key)
);

CREATE TABLE youtube_sync_proposed_cursors (
    job_id INTEGER NOT NULL REFERENCES youtube_sync_manifests(job_id),
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('seed_uploads', 'cross_channel_search')),
    source_key TEXT NOT NULL,
    completed_upper_bound TEXT NOT NULL,
    cursor_hash TEXT NOT NULL,
    PRIMARY KEY(job_id, profile_id, source_kind, source_key)
);

CREATE TABLE youtube_quota_reservations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    unit_key TEXT NOT NULL,
    request_ordinal INTEGER NOT NULL CHECK (request_ordinal >= 1),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    endpoint_class TEXT NOT NULL CHECK (endpoint_class IN ('search_list', 'channels_list', 'playlist_items_list', 'videos_list')),
    attempted_at TEXT NOT NULL,
    UNIQUE(job_id, unit_key, request_ordinal, attempt_no),
    FOREIGN KEY(job_id, unit_key) REFERENCES job_units(job_id, unit_key)
);

CREATE TABLE youtube_daily_sync_requests (
    jst_day TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES youtube_sync_manifests(job_id),
    requested_at TEXT NOT NULL
);

INSERT INTO retention_settings(id, retention_days) VALUES (1, 365);


CREATE UNIQUE INDEX one_active_speaker_threshold_config
ON speaker_threshold_configs(is_active)
WHERE is_active = 1;

CREATE UNIQUE INDEX one_active_voice_reference_profile_per_subject
ON voice_reference_profiles(subject_id)
WHERE is_active = 1;

CREATE INDEX job_units_status_ordinal
ON job_units(job_id, status, ordinal);

CREATE INDEX job_events_job_id
ON job_events(job_id, id);

CREATE INDEX analysis_runs_scope_id ON analysis_runs(scope_id, id);

CREATE INDEX analysis_run_events_run_id ON analysis_run_events(run_id, id);

CREATE INDEX analysis_run_segments_run_id
ON analysis_run_segments(run_id, ordinal);

CREATE INDEX analysis_run_outputs_run_ordinal
ON analysis_run_outputs(run_id, batch_ordinal);

CREATE INDEX analysis_statements_run_ordinal
ON analysis_statements(run_id, ordinal);

CREATE INDEX analysis_statement_evidence_run_segment
ON analysis_statement_evidence_links(run_segment_id);

CREATE INDEX period_reviews_period_id_id
ON period_reviews(period_id, id);

CREATE INDEX analysis_asset_mappings_run_statement
ON analysis_asset_mappings(run_id, statement_id);

CREATE INDEX mapping_reviews_mapping_id_id
ON mapping_reviews(mapping_id, id);

CREATE UNIQUE INDEX analysis_forecasts_batch_group
ON analysis_forecasts(
    projection_batch_id,
    asset,
    COALESCE(period_start, ''),
    COALESCE(period_end, ''),
    unknown_period,
    condition_kind,
    COALESCE(condition_text, '')
);

CREATE INDEX analysis_forecasts_batch_id
ON analysis_forecasts(projection_batch_id, id);

CREATE INDEX analysis_run_segments_segment_run
ON analysis_run_segments(segment_id, run_id);

CREATE UNIQUE INDEX current_forecasts_cache_owner
ON current_forecasts(
    scope_id,
    analysis_forecast_id,
    source_run_id,
    projection_batch_id
);

CREATE INDEX heatmap_cells_scope_granularity
ON heatmap_cells(scope_id, granularity, subject_id, asset, period_key, id);

CREATE INDEX heatmap_cell_forecasts_scope_source
ON heatmap_cell_forecasts(scope_id, source_forecast_id, heatmap_cell_id);

CREATE INDEX analysis_run_segments_video_run
ON analysis_run_segments(video_id, run_id);

CREATE INDEX analysis_run_segments_metadata_run
ON analysis_run_segments(metadata_snapshot_id, run_id);
CREATE INDEX analysis_run_segments_presence_run
ON analysis_run_segments(presence_decision_id, run_id);
CREATE INDEX video_pipeline_job_bindings_candidate
ON video_pipeline_job_bindings(candidate_id, job_id);
CREATE UNIQUE INDEX one_active_youtube_sync_job
ON jobs((1))
WHERE job_kind='youtube_sync'
  AND status IN ('running', 'pause_requested', 'cancel_requested');

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

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

CREATE TRIGGER analysis_input_snapshots_no_delete
BEFORE DELETE ON analysis_input_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

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

CREATE TRIGGER analysis_statement_evidence_requires_same_run
BEFORE INSERT ON analysis_statement_evidence_links
WHEN (
    SELECT run_id FROM analysis_run_segments WHERE id = NEW.run_segment_id
) IS NOT (
    SELECT run_id FROM analysis_statements WHERE id = NEW.statement_id
)
BEGIN SELECT RAISE(ABORT, 'STATEMENT_EVIDENCE_RUN_MISMATCH'); END;

CREATE TRIGGER analysis_statement_evidence_requires_exact_times
BEFORE INSERT ON analysis_statement_evidence_links
WHEN EXISTS (
    SELECT 1
    FROM analysis_run_segments AS run_segment
    JOIN transcript_segments AS segment
        ON segment.id = run_segment.segment_id
    WHERE run_segment.id = NEW.run_segment_id
        AND (
            segment.start_ms IS NOT NEW.start_ms
            OR segment.end_ms IS NOT NEW.end_ms
        )
)
BEGIN SELECT RAISE(ABORT, 'STATEMENT_EVIDENCE_TIME_MISMATCH'); END;

CREATE TRIGGER analysis_statement_first_evidence_requires_source_video
BEFORE INSERT ON analysis_statement_evidence_links
WHEN NEW.ordinal = 1 AND (
    SELECT video_id FROM analysis_run_segments WHERE id = NEW.run_segment_id
) IS NOT (
    SELECT source_video_id
    FROM analysis_statements
    WHERE id = NEW.statement_id
)
BEGIN SELECT RAISE(ABORT, 'STATEMENT_SOURCE_VIDEO_MISMATCH'); END;

CREATE TRIGGER analysis_statements_no_update
BEFORE UPDATE ON analysis_statements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statements_no_delete
BEFORE DELETE ON analysis_statements
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statement_evidence_links_no_update
BEFORE UPDATE ON analysis_statement_evidence_links
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statement_evidence_links_no_delete
BEFORE DELETE ON analysis_statement_evidence_links
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER period_reviews_approve_requires_unknown
BEFORE INSERT ON period_reviews
WHEN NEW.decision = 'approve_unknown'
    AND NOT EXISTS (
        SELECT 1
        FROM analysis_statement_periods
        WHERE id = NEW.period_id AND is_unknown = 1
    )
BEGIN SELECT RAISE(ABORT, 'PERIOD_REVIEW_INVALID'); END;

CREATE TRIGGER analysis_statement_periods_no_update
BEFORE UPDATE ON analysis_statement_periods
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_statement_periods_no_delete
BEFORE DELETE ON analysis_statement_periods
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER period_reviews_no_update
BEFORE UPDATE ON period_reviews
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER period_reviews_no_delete
BEFORE DELETE ON period_reviews
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_asset_mappings_require_running_unit
BEFORE INSERT ON analysis_asset_mappings
WHEN NOT EXISTS (
    SELECT 1
    FROM analysis_run_job_attempts AS attempt
    JOIN job_units AS unit
        ON unit.job_id = attempt.job_id
        AND unit.unit_key = 'analysis:map-assets'
    WHERE attempt.run_id = NEW.run_id
        AND attempt.attempt_ordinal = (
            SELECT MAX(active.attempt_ordinal)
            FROM analysis_run_job_attempts AS active
            WHERE active.run_id = NEW.run_id
        )
        AND unit.status = 'running'
)
BEGIN SELECT RAISE(ABORT, 'ASSET_MAPPING_UNIT_NOT_RUNNING'); END;

CREATE TRIGGER analysis_asset_mappings_require_safe_rule_evidence
BEFORE INSERT ON analysis_asset_mappings
WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.rule_evidence_json) AS evidence
    WHERE json_type(evidence.value) IS NOT 'object'
        OR EXISTS (
            SELECT 1
            FROM json_each(evidence.value) AS member
            WHERE member.key NOT IN (
                'segment_id',
                'evidence_kind',
                'market_code',
                'is_competing'
            )
        )
        OR (SELECT COUNT(*) FROM json_each(evidence.value)) != 4
        OR (
            SELECT COUNT(*)
            FROM json_each(evidence.value)
            WHERE key = 'segment_id'
        ) != 1
        OR (
            SELECT COUNT(*)
            FROM json_each(evidence.value)
            WHERE key = 'evidence_kind'
        ) != 1
        OR (
            SELECT COUNT(*)
            FROM json_each(evidence.value)
            WHERE key = 'market_code'
        ) != 1
        OR (
            SELECT COUNT(*)
            FROM json_each(evidence.value)
            WHERE key = 'is_competing'
        ) != 1
        OR json_type(evidence.value, '$.segment_id') IS NOT 'integer'
        OR COALESCE(
            json_extract(evidence.value, '$.segment_id') <= 0,
            1
        )
        OR json_type(evidence.value, '$.evidence_kind') IS NOT 'text'
        OR COALESCE(json_extract(evidence.value, '$.evidence_kind') NOT IN (
            'direct_expression',
            'explicit_market_expression',
            'generic_expression',
            'surrounding_subject_statement',
            'interviewer_context',
            'organization_assigned_statement'
        ), 1)
        OR json_type(evidence.value, '$.market_code') IS NOT 'text'
        OR COALESCE(json_extract(evidence.value, '$.market_code') NOT IN (
            'japan', 'us', 'gold'
        ), 1)
        OR (
            json_type(evidence.value, '$.is_competing') IS NOT 'true'
            AND json_type(evidence.value, '$.is_competing') IS NOT 'false'
        )
)
BEGIN SELECT RAISE(ABORT, 'ASSET_MAPPING_EVIDENCE_UNSAFE'); END;

CREATE TRIGGER analysis_asset_mappings_require_statement_source
BEFORE INSERT ON analysis_asset_mappings
WHEN NOT EXISTS (
    SELECT 1
    FROM analysis_statements AS statement
    WHERE statement.id = NEW.statement_id
        AND statement.run_id = NEW.run_id
        AND statement.source_video_id = NEW.source_video_id
        AND statement.original_target_expression = NEW.original_expression
)
BEGIN SELECT RAISE(ABORT, 'ASSET_MAPPING_STATEMENT_MISMATCH'); END;

CREATE TRIGGER analysis_asset_mappings_no_update
BEFORE UPDATE ON analysis_asset_mappings
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_asset_mappings_no_delete
BEFORE DELETE ON analysis_asset_mappings
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER mapping_reviews_require_consistent_state
BEFORE INSERT ON mapping_reviews
WHEN NOT EXISTS (
        SELECT 1
        FROM analysis_asset_mappings
        WHERE id = NEW.mapping_id
            AND final_confidence IN ('low', 'unresolved')
    )
    OR NEW.before_asset != COALESCE(
        (
            SELECT review.after_asset
            FROM mapping_reviews AS review
            WHERE review.mapping_id = NEW.mapping_id
            ORDER BY review.id DESC
            LIMIT 1
        ),
        (
            SELECT mapping.asset
            FROM analysis_asset_mappings AS mapping
            WHERE mapping.id = NEW.mapping_id
        )
    )
    OR (
        NEW.decision = 'approve'
        AND NEW.after_asset != (
            SELECT mapping.asset
            FROM analysis_asset_mappings AS mapping
            WHERE mapping.id = NEW.mapping_id
        )
    )
    OR (
        NEW.decision = 'correct'
        AND NEW.after_asset = (
            SELECT mapping.asset
            FROM analysis_asset_mappings AS mapping
            WHERE mapping.id = NEW.mapping_id
        )
    )
    OR (NEW.decision = 'correct' AND NEW.after_asset = NEW.before_asset)
    OR (NEW.decision = 'reject' AND NEW.after_asset != NEW.before_asset)
BEGIN SELECT RAISE(ABORT, 'MAPPING_REVIEW_INVALID'); END;

CREATE TRIGGER mapping_reviews_no_replace
BEFORE INSERT ON mapping_reviews
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM mapping_reviews WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER mapping_reviews_require_latest_id
AFTER INSERT ON mapping_reviews
WHEN EXISTS (
    SELECT 1
    FROM mapping_reviews
    WHERE mapping_id = NEW.mapping_id
        AND id > NEW.id
)
BEGIN SELECT RAISE(ABORT, 'MAPPING_REVIEW_INVALID'); END;

CREATE TRIGGER mapping_reviews_no_update
BEFORE UPDATE ON mapping_reviews
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER mapping_reviews_no_delete
BEFORE DELETE ON mapping_reviews
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER period_reviews_require_positive_latest_id
AFTER INSERT ON period_reviews
WHEN NEW.id <= 0
    OR EXISTS (
        SELECT 1
        FROM period_reviews
        WHERE period_id = NEW.period_id
            AND id > NEW.id
    )
BEGIN SELECT RAISE(ABORT, 'PERIOD_REVIEW_INVALID'); END;

CREATE TRIGGER forecast_projection_batches_require_current_review_heads
BEFORE INSERT ON forecast_projection_batches
WHEN NEW.latest_mapping_review_id IS NOT (
        SELECT MAX(review.id)
        FROM mapping_reviews AS review
        JOIN analysis_asset_mappings AS mapping
            ON mapping.id = review.mapping_id
        WHERE mapping.run_id = NEW.run_id
    )
    OR NEW.latest_period_review_id IS NOT (
        SELECT MAX(review.id)
        FROM period_reviews AS review
        JOIN analysis_statement_periods AS period
            ON period.id = review.period_id
        JOIN analysis_statements AS statement
            ON statement.id = period.statement_id
        WHERE statement.run_id = NEW.run_id
    )
BEGIN SELECT RAISE(ABORT, 'FORECAST_REVIEW_HEAD_MISMATCH'); END;

CREATE TRIGGER analysis_forecasts_require_batch_run
BEFORE INSERT ON analysis_forecasts
WHEN NOT EXISTS (
    SELECT 1
    FROM forecast_projection_batches AS batch
    WHERE batch.id = NEW.projection_batch_id
        AND batch.run_id = NEW.run_id
)
BEGIN SELECT RAISE(ABORT, 'FORECAST_BATCH_RUN_MISMATCH'); END;

CREATE TRIGGER analysis_forecasts_require_safe_directions
BEFORE INSERT ON analysis_forecasts
WHEN EXISTS (
        SELECT 1
        FROM json_each(NEW.directions_json) AS direction
        WHERE direction.type IS NOT 'text'
            OR direction.value NOT IN (
                'strong_up', 'up', 'flat', 'down', 'strong_down',
                'turning_point', 'unknown'
            )
    )
    OR (
        SELECT COUNT(*) FROM json_each(NEW.directions_json)
    ) != (
        SELECT COUNT(DISTINCT value) FROM json_each(NEW.directions_json)
    )
    OR NOT EXISTS (
        SELECT 1
        FROM json_each(NEW.directions_json)
        WHERE value = NEW.primary_direction
    )
    OR (
        NEW.view_relation = 'disagreement'
        AND (
            json_array_length(NEW.directions_json) < 2
            OR NOT EXISTS (
                SELECT 1 FROM json_each(NEW.directions_json)
                WHERE value IN ('up', 'strong_up')
            )
            OR NOT EXISTS (
                SELECT 1 FROM json_each(NEW.directions_json)
                WHERE value IN ('down', 'strong_down')
            )
        )
    )
    OR (
        NEW.view_relation != 'disagreement'
        AND EXISTS (
            SELECT 1 FROM json_each(NEW.directions_json)
            WHERE value IN ('up', 'strong_up')
        )
        AND EXISTS (
            SELECT 1 FROM json_each(NEW.directions_json)
            WHERE value IN ('down', 'strong_down')
        )
    )
BEGIN SELECT RAISE(ABORT, 'FORECAST_DIRECTIONS_INVALID'); END;

CREATE TRIGGER analysis_forecast_links_require_same_run
BEFORE INSERT ON analysis_forecast_statement_links
WHEN NOT EXISTS (
    SELECT 1
    FROM analysis_forecasts AS forecast
    JOIN analysis_statements AS statement
        ON statement.id = NEW.statement_id
        AND statement.run_id = forecast.run_id
    WHERE forecast.id = NEW.forecast_id
)
BEGIN SELECT RAISE(ABORT, 'FORECAST_LINK_OWNERSHIP_MISMATCH'); END;

CREATE TRIGGER forecast_projection_batches_no_replace
BEFORE INSERT ON forecast_projection_batches
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM forecast_projection_batches WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecasts_no_replace
BEFORE INSERT ON analysis_forecasts
WHEN NEW.id IS NOT NULL
    AND EXISTS (SELECT 1 FROM analysis_forecasts WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecast_statement_links_no_replace
BEFORE INSERT ON analysis_forecast_statement_links
WHEN EXISTS (
    SELECT 1
    FROM analysis_forecast_statement_links
    WHERE forecast_id = NEW.forecast_id
        AND statement_id = NEW.statement_id
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER forecast_projection_batches_no_update
BEFORE UPDATE ON forecast_projection_batches
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER forecast_projection_batches_no_delete
BEFORE DELETE ON forecast_projection_batches
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecasts_no_update
BEFORE UPDATE ON analysis_forecasts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecasts_no_delete
BEFORE DELETE ON analysis_forecasts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecast_statement_links_no_update
BEFORE UPDATE ON analysis_forecast_statement_links
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER analysis_forecast_statement_links_no_delete
BEFORE DELETE ON analysis_forecast_statement_links
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER current_result_sets_validate_insert
BEFORE INSERT ON current_result_sets
WHEN NOT EXISTS (
        SELECT 1
        FROM analysis_runs AS run
        WHERE run.id = NEW.source_run_id
            AND run.scope_id = NEW.scope_id
    )
    OR NOT EXISTS (
        SELECT 1
        FROM forecast_projection_batches AS batch
        WHERE batch.id = NEW.projection_batch_id
            AND batch.run_id = NEW.source_run_id
    )
BEGIN SELECT RAISE(ABORT, 'CURRENT_RESULT_SET_OWNERSHIP_MISMATCH'); END;

CREATE TRIGGER current_result_sets_validate_update
BEFORE UPDATE ON current_result_sets
BEGIN SELECT RAISE(ABORT, 'CURRENT_RESULT_SET_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER current_statements_validate_insert
BEFORE INSERT ON current_statements
WHEN NOT EXISTS (
        SELECT 1
        FROM current_result_sets AS result_set
        WHERE result_set.scope_id = NEW.scope_id
            AND result_set.source_run_id = NEW.source_run_id
    )
    OR NOT EXISTS (
        SELECT 1
        FROM analysis_statements AS statement
        WHERE statement.id = NEW.analysis_statement_id
            AND statement.run_id = NEW.source_run_id
    )
BEGIN SELECT RAISE(ABORT, 'CURRENT_STATEMENT_OWNERSHIP_MISMATCH'); END;

CREATE TRIGGER current_statements_validate_update
BEFORE UPDATE ON current_statements
BEGIN SELECT RAISE(ABORT, 'CURRENT_STATEMENT_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER current_asset_mappings_validate_insert
BEFORE INSERT ON current_asset_mappings
WHEN NOT EXISTS (
        SELECT 1
        FROM current_result_sets AS result_set
        WHERE result_set.scope_id = NEW.scope_id
            AND result_set.source_run_id = NEW.source_run_id
    )
    OR NOT EXISTS (
        SELECT 1
        FROM analysis_asset_mappings AS mapping
        WHERE mapping.id = NEW.analysis_mapping_id
            AND mapping.run_id = NEW.source_run_id
    )
BEGIN SELECT RAISE(ABORT, 'CURRENT_MAPPING_OWNERSHIP_MISMATCH'); END;

CREATE TRIGGER current_asset_mappings_validate_update
BEFORE UPDATE ON current_asset_mappings
BEGIN SELECT RAISE(ABORT, 'CURRENT_MAPPING_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER current_forecasts_validate_insert
BEFORE INSERT ON current_forecasts
WHEN NOT EXISTS (
        SELECT 1
        FROM current_result_sets AS result_set
        WHERE result_set.scope_id = NEW.scope_id
            AND result_set.source_run_id = NEW.source_run_id
            AND result_set.projection_batch_id = NEW.projection_batch_id
    )
    OR NOT EXISTS (
        SELECT 1
        FROM analysis_forecasts AS forecast
        JOIN forecast_projection_batches AS batch
            ON batch.id = forecast.projection_batch_id
        WHERE forecast.id = NEW.analysis_forecast_id
            AND forecast.run_id = NEW.source_run_id
            AND forecast.projection_batch_id = NEW.projection_batch_id
            AND batch.run_id = NEW.source_run_id
    )
BEGIN SELECT RAISE(ABORT, 'CURRENT_FORECAST_OWNERSHIP_MISMATCH'); END;

CREATE TRIGGER current_forecasts_validate_update
BEFORE UPDATE ON current_forecasts
BEGIN SELECT RAISE(ABORT, 'CURRENT_FORECAST_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER heatmap_cells_validate_insert
BEFORE INSERT ON heatmap_cells
WHEN NOT EXISTS (
        SELECT 1
        FROM analysis_scopes AS scope
        WHERE scope.id = NEW.scope_id
            AND scope.subject_id = NEW.subject_id
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.directions_json)
        WHERE type IS NOT 'text'
            OR value NOT IN (
                'strong_up', 'up', 'flat', 'down', 'strong_down',
                'turning_point', 'unknown'
            )
    )
    OR (SELECT COUNT(*) FROM json_each(NEW.directions_json))
        != (SELECT COUNT(DISTINCT value) FROM json_each(NEW.directions_json))
    OR NOT EXISTS (
        SELECT 1 FROM json_each(NEW.directions_json)
        WHERE value = NEW.primary_direction
    )
    OR (
        NEW.view_relation = 'disagreement'
        AND (
            NOT EXISTS (
                SELECT 1 FROM json_each(NEW.directions_json)
                WHERE value IN ('up', 'strong_up')
            )
            OR NOT EXISTS (
                SELECT 1 FROM json_each(NEW.directions_json)
                WHERE value IN ('down', 'strong_down')
            )
        )
    )
    OR (
        NEW.view_relation != 'disagreement'
        AND EXISTS (
            SELECT 1 FROM json_each(NEW.directions_json)
            WHERE value IN ('up', 'strong_up')
        )
        AND EXISTS (
            SELECT 1 FROM json_each(NEW.directions_json)
            WHERE value IN ('down', 'strong_down')
        )
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.condition_texts_json)
        WHERE type IS NOT 'text' OR length(value) = 0
    )
    OR (SELECT COUNT(*) FROM json_each(NEW.condition_texts_json))
        != (
            SELECT COUNT(DISTINCT value)
            FROM json_each(NEW.condition_texts_json)
        )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.supporting_statement_ids_json)
        WHERE type IS NOT 'integer' OR value <= 0
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.counterevidence_statement_ids_json)
        WHERE type IS NOT 'integer' OR value <= 0
    )
    OR (SELECT COUNT(*) FROM json_each(NEW.supporting_statement_ids_json))
        != NEW.evidence_count
    OR (SELECT COUNT(*) FROM json_each(NEW.supporting_statement_ids_json))
        != (
            SELECT COUNT(DISTINCT value)
            FROM json_each(NEW.supporting_statement_ids_json)
        )
    OR (SELECT COUNT(*) FROM json_each(NEW.counterevidence_statement_ids_json))
        != (
            SELECT COUNT(DISTINCT value)
            FROM json_each(NEW.counterevidence_statement_ids_json)
        )
    OR EXISTS (
        SELECT 1
        FROM json_each(NEW.supporting_statement_ids_json) AS support
        JOIN json_each(NEW.counterevidence_statement_ids_json) AS counter
            ON counter.value = support.value
    )
BEGIN SELECT RAISE(ABORT, 'HEATMAP_CELL_INVALID'); END;

CREATE TRIGGER heatmap_cells_no_update
BEFORE UPDATE ON heatmap_cells
BEGIN SELECT RAISE(ABORT, 'HEATMAP_CELL_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER heatmap_cell_forecasts_no_update
BEFORE UPDATE ON heatmap_cell_forecasts
BEGIN SELECT RAISE(ABORT, 'HEATMAP_CELL_FORECAST_UPDATE_FORBIDDEN'); END;

CREATE TRIGGER retention_settings_limited_update
BEFORE UPDATE ON retention_settings
WHEN OLD.id IS NOT NEW.id
BEGIN SELECT RAISE(ABORT, 'RETENTION_SETTINGS_INVALID'); END;

CREATE TRIGGER retention_settings_no_delete
BEFORE DELETE ON retention_settings
BEGIN SELECT RAISE(ABORT, 'RETENTION_SETTINGS_REQUIRED'); END;

CREATE TRIGGER local_artifacts_limited_update
BEFORE UPDATE ON local_artifacts
WHEN NOT (
    OLD.id IS NEW.id
    AND OLD.kind IS NEW.kind
    AND OLD.local_path IS NEW.local_path
    AND OLD.created_at IS NEW.created_at
    AND COALESCE(retention_audio_transition_authorized(
        OLD.id,
        OLD.status,
        OLD.retry_count,
        NEW.status,
        NEW.retry_count,
        NEW.safe_error_code,
        NEW.deleted_at
    ), 0) = 1
    AND (
        (
            OLD.status IN ('pending', 'delete_failed')
            AND NEW.status = 'delete_failed'
            AND NEW.retry_count = OLD.retry_count + 1
            AND NEW.safe_error_code IS NOT NULL
            AND NEW.deleted_at IS NULL
        )
        OR (
            OLD.status IN ('pending', 'delete_failed')
            AND NEW.status = 'deleted'
            AND NEW.retry_count = OLD.retry_count
            AND NEW.safe_error_code IS NULL
            AND NEW.deleted_at IS NOT NULL
        )
    )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_LOCAL_ARTIFACT'); END;

CREATE TRIGGER local_artifacts_no_delete
BEFORE DELETE ON local_artifacts
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER local_artifacts_no_replace
BEFORE INSERT ON local_artifacts
WHEN EXISTS (
    SELECT 1 FROM local_artifacts WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_LOCAL_ARTIFACT'); END;

CREATE TRIGGER transcript_segments_limited_update
BEFORE UPDATE ON transcript_segments
WHEN NOT (
    OLD.text_body IS NOT NULL
    AND NEW.text_body IS NULL
    AND OLD.text_deleted_at IS NULL
    AND NEW.text_deleted_at IS NOT NULL
    AND COALESCE(
        length(NEW.text_deleted_at) = 27
        AND strftime('%Y-%m-%dT%H:%M:%S', NEW.text_deleted_at)
            = substr(NEW.text_deleted_at, 1, 19)
        AND CAST(substr(NEW.text_deleted_at, 1, 4) AS INTEGER)
            BETWEEN 1 AND 9999
        AND CAST(substr(NEW.text_deleted_at, 12, 2) AS INTEGER)
            BETWEEN 0 AND 23
        AND substr(NEW.text_deleted_at, 20, 1) = '.'
        AND substr(NEW.text_deleted_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(NEW.text_deleted_at, 27, 1) = 'Z',
        0
    )
    AND OLD.id IS NEW.id
    AND OLD.video_id IS NEW.video_id
    AND OLD.chunk_id IS NEW.chunk_id
    AND OLD.segment_no IS NEW.segment_no
    AND OLD.start_ms IS NEW.start_ms
    AND OLD.end_ms IS NEW.end_ms
    AND OLD.text_sha256 IS NEW.text_sha256
    AND OLD.anonymous_speaker_id IS NEW.anonymous_speaker_id
    AND OLD.transcript_created_at IS NEW.transcript_created_at
    AND OLD.expires_at IS NEW.expires_at
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_TRANSCRIPT_BODY'); END;

CREATE TRIGGER transcript_segments_no_delete
BEFORE DELETE ON transcript_segments
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER transcript_segments_no_replace
BEFORE INSERT ON transcript_segments
WHEN EXISTS (
    SELECT 1
    FROM transcript_segments
    WHERE id = NEW.id
        OR (
            video_id = NEW.video_id
            AND segment_no = NEW.segment_no
        )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_TRANSCRIPT_BODY'); END;

CREATE TRIGGER analysis_input_snapshots_limited_update
BEFORE UPDATE ON analysis_input_snapshots
WHEN NOT (
    OLD.input_text IS NOT NULL
    AND NEW.input_text IS NULL
    AND OLD.text_deleted_at IS NULL
    AND NEW.text_deleted_at IS NOT NULL
    AND COALESCE(
        length(NEW.text_deleted_at) = 27
        AND strftime('%Y-%m-%dT%H:%M:%S', NEW.text_deleted_at)
            = substr(NEW.text_deleted_at, 1, 19)
        AND CAST(substr(NEW.text_deleted_at, 1, 4) AS INTEGER)
            BETWEEN 1 AND 9999
        AND CAST(substr(NEW.text_deleted_at, 12, 2) AS INTEGER)
            BETWEEN 0 AND 23
        AND substr(NEW.text_deleted_at, 20, 1) = '.'
        AND substr(NEW.text_deleted_at, 21, 6) NOT GLOB '*[^0-9]*'
        AND substr(NEW.text_deleted_at, 27, 1) = 'Z',
        0
    )
    AND OLD.id IS NEW.id
    AND OLD.run_id IS NEW.run_id
    AND OLD.metadata_json IS NEW.metadata_json
    AND OLD.input_sha256 IS NEW.input_sha256
    AND OLD.snapshot_created_at IS NEW.snapshot_created_at
    AND OLD.expires_at IS NEW.expires_at
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ANALYSIS_SNAPSHOT'); END;

CREATE TRIGGER analysis_input_snapshots_no_replace
BEFORE INSERT ON analysis_input_snapshots
WHEN EXISTS (
    SELECT 1
    FROM analysis_input_snapshots
    WHERE id = NEW.id OR run_id = NEW.run_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ANALYSIS_SNAPSHOT'); END;

CREATE TRIGGER analysis_scopes_generation_monotonic
BEFORE UPDATE OF generation ON analysis_scopes
WHEN typeof(NEW.generation) != 'integer'
    OR NEW.generation != OLD.generation + 1
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_SCOPE_GENERATION_INVALID'); END;

CREATE TRIGGER analysis_scopes_no_replace
BEFORE INSERT ON analysis_scopes
WHEN EXISTS (
    SELECT 1
    FROM analysis_scopes
    WHERE id = NEW.id
        OR (
            subject_id = NEW.subject_id
            AND cutoff_day_jst = NEW.cutoff_day_jst
        )
)
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_SCOPE_GENERATION_INVALID'); END;

CREATE TRIGGER analysis_runs_scope_generation_match
BEFORE INSERT ON analysis_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM analysis_scopes AS scope
    WHERE scope.id = NEW.scope_id
        AND scope.generation = NEW.scope_generation
)
BEGIN SELECT RAISE(ABORT, 'ANALYSIS_RUN_GENERATION_MISMATCH'); END;

CREATE TRIGGER analysis_runs_no_replace
BEFORE INSERT ON analysis_runs
WHEN EXISTS (
    SELECT 1 FROM analysis_runs WHERE id = NEW.id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ANALYSIS_RUN_GENERATION'); END;

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

CREATE TRIGGER analysis_runs_reject_deleted_source_text
BEFORE INSERT ON analysis_runs
WHEN EXISTS (
    SELECT 1
    FROM analysis_scopes AS scope
    JOIN analysis_subjects AS subject ON subject.id=scope.subject_id
    JOIN discovery_profiles AS profile
        ON profile.subject_id=subject.id AND profile.is_active=1
    JOIN subject_video_candidates AS candidate
        ON candidate.profile_id=profile.id
    JOIN presence_decisions AS presence
        ON presence.id=candidate.current_presence_decision_id
        AND presence.candidate_id=candidate.id
        AND presence.state='presence_confirmed'
    JOIN videos AS video ON video.id=candidate.video_id
    JOIN video_metadata_snapshots AS metadata
        ON metadata.id=video.current_metadata_snapshot_id
        AND metadata.video_id=video.id
    JOIN transcript_segments AS segment ON segment.video_id=video.id
    JOIN speaker_assignments AS assignment
        ON assignment.segment_id=segment.id
    WHERE scope.id=NEW.scope_id
      AND subject.is_active=1
      AND metadata.published_at<scope.cutoff_exclusive_utc
      AND segment.text_body IS NULL
      AND segment.text_deleted_at IS NOT NULL
      AND (
        (
          assignment.assignment_kind='subject'
          AND assignment.assigned_subject_id=subject.id
        )
        OR (
          assignment.assignment_kind='interviewer'
          AND assignment.assigned_subject_id IS NULL
          AND EXISTS (
            SELECT 1
            FROM transcript_segments AS subject_segment
            JOIN speaker_assignments AS subject_assignment
              ON subject_assignment.segment_id=subject_segment.id
            WHERE subject_segment.video_id=video.id
              AND subject_segment.text_body IS NOT NULL
              AND subject_assignment.assignment_kind='subject'
              AND subject_assignment.assigned_subject_id=subject.id
          )
        )
      )
)
BEGIN SELECT RAISE(ABORT, 'SOURCE_TEXT_DELETED'); END;

CREATE TRIGGER discovery_profiles_current_version_owner_insert
BEFORE INSERT ON discovery_profiles
WHEN NEW.current_version_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM discovery_profile_versions
    WHERE id=NEW.current_version_id AND profile_id=NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER discovery_profiles_current_version_owner_update
BEFORE UPDATE OF current_version_id ON discovery_profiles
WHEN (OLD.current_version_id IS NOT NULL AND NEW.current_version_id IS NULL)
 OR (
    NEW.current_version_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM discovery_profile_versions
        WHERE id=NEW.current_version_id AND profile_id=OLD.id
    )
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER videos_current_metadata_snapshot_owner_insert
BEFORE INSERT ON videos
WHEN NEW.current_metadata_snapshot_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM video_metadata_snapshots
    WHERE id=NEW.current_metadata_snapshot_id AND video_id=NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER videos_current_metadata_snapshot_owner_update
BEFORE UPDATE OF current_metadata_snapshot_id ON videos
WHEN (
    OLD.current_metadata_snapshot_id IS NOT NULL
    AND NEW.current_metadata_snapshot_id IS NULL
 )
 OR (
    NEW.current_metadata_snapshot_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM video_metadata_snapshots
        WHERE id=NEW.current_metadata_snapshot_id AND video_id=OLD.id
    )
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER subject_video_candidates_current_presence_owner_insert
BEFORE INSERT ON subject_video_candidates
WHEN NEW.current_presence_decision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM presence_decisions
    WHERE id=NEW.current_presence_decision_id AND candidate_id=NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER subject_video_candidates_current_presence_owner_update
BEFORE UPDATE OF current_presence_decision_id ON subject_video_candidates
WHEN (
    OLD.current_presence_decision_id IS NOT NULL
    AND NEW.current_presence_decision_id IS NULL
 )
 OR (
    NEW.current_presence_decision_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM presence_decisions
        WHERE id=NEW.current_presence_decision_id AND candidate_id=OLD.id
    )
 )
BEGIN SELECT RAISE(ABORT, 'POINTER_OWNER_MISMATCH'); END;

CREATE TRIGGER discovery_profiles_limited_update
BEFORE UPDATE ON discovery_profiles
WHEN OLD.id IS NOT NEW.id
 OR OLD.subject_id IS NOT NEW.subject_id
 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROFILE'); END;

CREATE TRIGGER discovery_profiles_no_delete BEFORE DELETE ON discovery_profiles
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROFILE'); END;

CREATE TRIGGER discovery_profiles_no_replace BEFORE INSERT ON discovery_profiles
WHEN EXISTS (
    SELECT 1 FROM discovery_profiles
    WHERE id=NEW.id OR subject_id=NEW.subject_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROFILE'); END;

CREATE TRIGGER videos_limited_update
BEFORE UPDATE ON videos
WHEN OLD.id IS NOT NEW.id
 OR OLD.youtube_video_id IS NOT NEW.youtube_video_id
 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VIDEO'); END;

CREATE TRIGGER videos_no_delete BEFORE DELETE ON videos
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VIDEO'); END;

CREATE TRIGGER videos_no_replace BEFORE INSERT ON videos
WHEN EXISTS (
    SELECT 1 FROM videos
    WHERE id=NEW.id OR youtube_video_id=NEW.youtube_video_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VIDEO'); END;

CREATE TRIGGER subject_video_candidates_limited_update
BEFORE UPDATE ON subject_video_candidates
WHEN OLD.id IS NOT NEW.id
 OR OLD.profile_id IS NOT NEW.profile_id
 OR OLD.video_id IS NOT NEW.video_id
 OR OLD.first_observation_id IS NOT NEW.first_observation_id
 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_CANDIDATE'); END;

CREATE TRIGGER subject_video_candidates_no_delete
BEFORE DELETE ON subject_video_candidates
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_CANDIDATE'); END;

CREATE TRIGGER subject_video_candidates_no_replace
BEFORE INSERT ON subject_video_candidates
WHEN EXISTS (
    SELECT 1 FROM subject_video_candidates
    WHERE id=NEW.id
       OR (profile_id=NEW.profile_id AND video_id=NEW.video_id)
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_CANDIDATE'); END;

CREATE TRIGGER youtube_sync_manifests_limited_update
BEFORE UPDATE ON youtube_sync_manifests
WHEN OLD.job_id IS NOT NEW.job_id
 OR OLD.sync_kind IS NOT NEW.sync_kind
 OR OLD.upper_bound IS NOT NEW.upper_bound
 OR OLD.backfill_floor IS NOT NEW.backfill_floor
 OR OLD.quota_contract_version IS NOT NEW.quota_contract_version
 OR OLD.profile_set_hash IS NOT NEW.profile_set_hash
 OR OLD.manual_request_id IS NOT NEW.manual_request_id
 OR OLD.manifest_hash IS NOT NEW.manifest_hash
 OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_YOUTUBE_MANIFEST'); END;

CREATE TRIGGER youtube_sync_manifests_no_delete
BEFORE DELETE ON youtube_sync_manifests
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_YOUTUBE_MANIFEST'); END;

CREATE TRIGGER youtube_sync_manifests_no_replace
BEFORE INSERT ON youtube_sync_manifests
WHEN EXISTS (SELECT 1 FROM youtube_sync_manifests WHERE job_id=NEW.job_id)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_YOUTUBE_MANIFEST'); END;

CREATE TRIGGER youtube_sync_manifests_require_job
BEFORE INSERT ON youtube_sync_manifests
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE id=NEW.job_id
      AND job_kind='youtube_sync'
      AND manifest_hash=NEW.manifest_hash
)
BEGIN SELECT RAISE(ABORT, 'YOUTUBE_SYNC_JOB_REQUIRED'); END;

CREATE TRIGGER youtube_sync_manifest_profiles_require_owner
BEFORE INSERT ON youtube_sync_manifest_profiles
WHEN NOT EXISTS (
    SELECT 1 FROM discovery_profile_versions
    WHERE id=NEW.profile_version_id
      AND profile_id=NEW.profile_id
      AND config_hash=NEW.config_hash
)
BEGIN SELECT RAISE(ABORT, 'PROFILE_VERSION_OWNER_MISMATCH'); END;

CREATE TRIGGER youtube_sync_manifest_profiles_require_contiguous_ordinal
BEFORE INSERT ON youtube_sync_manifest_profiles
WHEN NEW.ordinal != (
    SELECT COUNT(*) + 1
    FROM youtube_sync_manifest_profiles
    WHERE job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'INVALID_MANIFEST_ORDINALS'); END;

CREATE TRIGGER youtube_sync_checkpoints_require_manifest_unit
BEFORE INSERT ON youtube_sync_checkpoints
WHEN NOT EXISTS (
    SELECT 1
    FROM youtube_sync_manifests AS manifest
    JOIN jobs AS job ON job.id=manifest.job_id
    JOIN job_units AS unit ON unit.job_id=job.id
    WHERE manifest.job_id=NEW.job_id
      AND unit.unit_key=NEW.unit_key
      AND (
        (NEW.source_kind='seed_uploads'
          AND unit.stage='youtube_seed_discovery')
        OR (NEW.source_kind='cross_channel_search'
          AND unit.stage='youtube_search_discovery')
        OR (NEW.source_kind='manual_url'
          AND unit.stage='youtube_manual_discovery')
      )
      AND job.total_units=(
        SELECT COUNT(*) FROM job_units WHERE job_id=job.id
      )
)
BEGIN SELECT RAISE(ABORT, 'YOUTUBE_MANIFEST_UNIT_REQUIRED'); END;

CREATE TRIGGER youtube_daily_sync_requests_require_full_manifest
BEFORE INSERT ON youtube_daily_sync_requests
WHEN COALESCE((
    SELECT sync_kind FROM youtube_sync_manifests WHERE job_id=NEW.job_id
), '')!='full_discovery'
BEGIN SELECT RAISE(ABORT, 'FULL_DISCOVERY_MANIFEST_REQUIRED'); END;

CREATE TRIGGER youtube_quota_reservations_require_youtube_unit
BEFORE INSERT ON youtube_quota_reservations
WHEN NOT EXISTS (
    SELECT 1
    FROM jobs AS job
    JOIN job_units AS unit ON unit.job_id=job.id
    WHERE job.id=NEW.job_id
      AND job.job_kind='youtube_sync'
      AND unit.unit_key=NEW.unit_key
      AND unit.stage IN (
        'youtube_seed_discovery',
        'youtube_search_discovery',
        'youtube_manual_discovery'
      )
)
BEGIN SELECT RAISE(ABORT, 'YOUTUBE_MANIFEST_UNIT_REQUIRED'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_require_video_job
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN COALESCE(
    (SELECT job_kind FROM jobs WHERE id=NEW.job_id),
    ''
)!='video_pipeline'
BEGIN SELECT RAISE(ABORT, 'VIDEO_PIPELINE_JOB_REQUIRED'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_require_open_insert
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN NEW.is_sealed!=0
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_seal_once
BEFORE UPDATE ON video_pipeline_job_binding_sets
WHEN NOT (
    OLD.job_id IS NEW.job_id
    AND OLD.expected_binding_count IS NEW.expected_binding_count
    AND OLD.is_sealed=0
    AND NEW.is_sealed=1
    AND (SELECT job_kind FROM jobs WHERE id=OLD.job_id)='video_pipeline'
    AND (
        SELECT COUNT(*)
        FROM video_pipeline_job_bindings
        WHERE job_id=OLD.job_id
    )=OLD.expected_binding_count
    AND (
        SELECT COUNT(DISTINCT candidate.video_id)
        FROM video_pipeline_job_bindings AS binding
        JOIN subject_video_candidates AS candidate
          ON candidate.id=binding.candidate_id
        WHERE binding.job_id=OLD.job_id
    )=1
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_no_delete
BEFORE DELETE ON video_pipeline_job_binding_sets
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_binding_sets_no_replace
BEFORE INSERT ON video_pipeline_job_binding_sets
WHEN EXISTS (
    SELECT 1 FROM video_pipeline_job_binding_sets WHERE job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_video_job
BEFORE INSERT ON video_pipeline_job_bindings
WHEN COALESCE(
    (SELECT job_kind FROM jobs WHERE id=NEW.job_id),
    ''
)!='video_pipeline'
BEGIN SELECT RAISE(ABORT, 'VIDEO_PIPELINE_JOB_REQUIRED'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_open_set
BEFORE INSERT ON video_pipeline_job_bindings
WHEN COALESCE((
    SELECT is_sealed
    FROM video_pipeline_job_binding_sets
    WHERE job_id=NEW.job_id
), 1)!=0
 OR (
    SELECT COUNT(*)
    FROM video_pipeline_job_bindings
    WHERE job_id=NEW.job_id
 )>=COALESCE((
    SELECT expected_binding_count
    FROM video_pipeline_job_binding_sets
    WHERE job_id=NEW.job_id
 ), 0)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_require_one_video
BEFORE INSERT ON video_pipeline_job_bindings
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_bindings AS existing
    JOIN subject_video_candidates AS bound_candidate
      ON bound_candidate.id=existing.candidate_id
    JOIN subject_video_candidates AS new_candidate
      ON new_candidate.id=NEW.candidate_id
    WHERE existing.job_id=NEW.job_id
      AND bound_candidate.video_id!=new_candidate.video_id
)
BEGIN SELECT RAISE(ABORT, 'VIDEO_PIPELINE_VIDEO_MISMATCH'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_update
BEFORE UPDATE ON video_pipeline_job_bindings
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_delete
BEFORE DELETE ON video_pipeline_job_bindings
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER video_pipeline_job_bindings_no_replace
BEFORE INSERT ON video_pipeline_job_bindings
WHEN EXISTS (
    SELECT 1
    FROM video_pipeline_job_bindings
    WHERE job_id=NEW.job_id AND candidate_id=NEW.candidate_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_JOB_BINDING'); END;

CREATE TRIGGER discovery_profile_versions_no_update BEFORE UPDATE ON discovery_profile_versions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_profile_versions_no_delete BEFORE DELETE ON discovery_profile_versions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_profile_versions_no_replace BEFORE INSERT ON discovery_profile_versions
WHEN EXISTS (
    SELECT 1 FROM discovery_profile_versions
    WHERE id=NEW.id
       OR (profile_id=NEW.profile_id AND config_hash=NEW.config_hash)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER discovery_seed_channels_no_update BEFORE UPDATE ON discovery_seed_channels
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_seed_channels_no_delete BEFORE DELETE ON discovery_seed_channels
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_seed_channels_no_replace BEFORE INSERT ON discovery_seed_channels
WHEN EXISTS (
    SELECT 1 FROM discovery_seed_channels
    WHERE profile_version_id=NEW.profile_version_id
      AND (
        ordinal=NEW.ordinal
        OR youtube_channel_id=NEW.youtube_channel_id
      )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER discovery_search_terms_no_update BEFORE UPDATE ON discovery_search_terms
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_search_terms_no_delete BEFORE DELETE ON discovery_search_terms
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_search_terms_no_replace BEFORE INSERT ON discovery_search_terms
WHEN EXISTS (
    SELECT 1 FROM discovery_search_terms
    WHERE profile_version_id=NEW.profile_version_id
      AND (ordinal=NEW.ordinal OR search_term=NEW.search_term)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER video_metadata_snapshots_no_update BEFORE UPDATE ON video_metadata_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER video_metadata_snapshots_no_delete BEFORE DELETE ON video_metadata_snapshots
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER video_metadata_snapshots_no_replace BEFORE INSERT ON video_metadata_snapshots
WHEN EXISTS (
    SELECT 1 FROM video_metadata_snapshots
    WHERE id=NEW.id
       OR (video_id=NEW.video_id AND canonical_hash=NEW.canonical_hash)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER discovery_observations_no_update BEFORE UPDATE ON discovery_observations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_observations_no_delete BEFORE DELETE ON discovery_observations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER discovery_observations_no_replace BEFORE INSERT ON discovery_observations
WHEN EXISTS (
    SELECT 1 FROM discovery_observations
    WHERE id=NEW.id OR idempotency_key=NEW.idempotency_key
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER presence_decisions_no_update BEFORE UPDATE ON presence_decisions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER presence_decisions_no_delete BEFORE DELETE ON presence_decisions
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER presence_decisions_no_replace BEFORE INSERT ON presence_decisions
WHEN EXISTS (
    SELECT 1 FROM presence_decisions
    WHERE id=NEW.id
       OR (candidate_id=NEW.candidate_id AND decision_hash=NEW.decision_hash)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER youtube_quota_reservations_no_update BEFORE UPDATE ON youtube_quota_reservations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_quota_reservations_no_delete BEFORE DELETE ON youtube_quota_reservations
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_quota_reservations_no_replace BEFORE INSERT ON youtube_quota_reservations
WHEN EXISTS (
    SELECT 1 FROM youtube_quota_reservations
    WHERE id=NEW.id
       OR (
        job_id=NEW.job_id
        AND unit_key=NEW.unit_key
        AND request_ordinal=NEW.request_ordinal
        AND attempt_no=NEW.attempt_no
       )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER manual_discovery_requests_no_update BEFORE UPDATE ON manual_discovery_requests
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER manual_discovery_requests_no_delete BEFORE DELETE ON manual_discovery_requests
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER manual_discovery_requests_no_replace BEFORE INSERT ON manual_discovery_requests
WHEN EXISTS (
    SELECT 1 FROM manual_discovery_requests
    WHERE id=NEW.id
       OR (
        profile_id=NEW.profile_id
        AND youtube_video_id=NEW.youtube_video_id
       )
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER youtube_sync_manifest_profiles_no_update BEFORE UPDATE ON youtube_sync_manifest_profiles
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_sync_manifest_profiles_no_delete BEFORE DELETE ON youtube_sync_manifest_profiles
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_sync_manifest_profiles_no_replace BEFORE INSERT ON youtube_sync_manifest_profiles
WHEN EXISTS (
    SELECT 1 FROM youtube_sync_manifest_profiles
    WHERE job_id=NEW.job_id
      AND (ordinal=NEW.ordinal OR profile_id=NEW.profile_id)
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;

CREATE TRIGGER youtube_daily_sync_requests_no_update BEFORE UPDATE ON youtube_daily_sync_requests
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_daily_sync_requests_no_delete BEFORE DELETE ON youtube_daily_sync_requests
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER youtube_daily_sync_requests_no_replace BEFORE INSERT ON youtube_daily_sync_requests
WHEN EXISTS (
    SELECT 1 FROM youtube_daily_sync_requests
    WHERE jst_day=NEW.jst_day OR job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
