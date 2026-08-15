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
