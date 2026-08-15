ALTER TABLE analysis_scopes
ADD COLUMN generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(generation) = 'integer' AND generation >= 0
);

ALTER TABLE analysis_runs
ADD COLUMN scope_generation INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(scope_generation) = 'integer' AND scope_generation >= 0
);

CREATE INDEX analysis_run_segments_video_run
ON analysis_run_segments(video_id, run_id);

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
