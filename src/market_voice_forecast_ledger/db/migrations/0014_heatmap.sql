CREATE UNIQUE INDEX current_forecasts_cache_owner
ON current_forecasts(
    scope_id,
    analysis_forecast_id,
    source_run_id,
    projection_batch_id
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

CREATE INDEX heatmap_cells_scope_granularity
ON heatmap_cells(scope_id, granularity, subject_id, asset, period_key, id);

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

CREATE INDEX heatmap_cell_forecasts_scope_source
ON heatmap_cell_forecasts(scope_id, source_forecast_id, heatmap_cell_id);

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
