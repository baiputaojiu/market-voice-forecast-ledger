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

CREATE INDEX analysis_asset_mappings_run_statement
ON analysis_asset_mappings(run_id, statement_id);

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
