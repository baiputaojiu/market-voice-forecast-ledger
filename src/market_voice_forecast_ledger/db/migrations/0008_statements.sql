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

CREATE INDEX analysis_statements_run_ordinal
ON analysis_statements(run_id, ordinal);

CREATE INDEX analysis_statement_evidence_run_segment
ON analysis_statement_evidence_links(run_segment_id);

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
