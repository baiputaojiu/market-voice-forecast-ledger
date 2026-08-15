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
