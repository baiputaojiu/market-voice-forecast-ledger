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

CREATE INDEX mapping_reviews_mapping_id_id
ON mapping_reviews(mapping_id, id);

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
