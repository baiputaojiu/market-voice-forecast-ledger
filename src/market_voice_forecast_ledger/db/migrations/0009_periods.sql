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

CREATE INDEX period_reviews_period_id_id
ON period_reviews(period_id, id);

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
