CREATE TABLE retention_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    retention_days INTEGER CHECK (
        retention_days IN (30, 90, 180, 365)
        OR retention_days IS NULL
    )
);

INSERT INTO retention_settings(id, retention_days) VALUES (1, 365);

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

CREATE TRIGGER analysis_runs_reject_deleted_source_text
BEFORE INSERT ON analysis_runs
WHEN EXISTS (
    SELECT 1
    FROM analysis_scopes AS scope
    JOIN analysis_subjects AS subject ON subject.id = scope.subject_id
    JOIN subject_channel_policies AS policy
        ON policy.subject_id = subject.id
        AND policy.configuration_status = 'configured'
    JOIN subject_video_eligibility AS eligibility
        ON eligibility.subject_id = scope.subject_id
        AND eligibility.policy_id = policy.id
        AND eligibility.policy_hash = policy.policy_hash
        AND eligibility.status = 'eligible'
    JOIN videos AS video ON video.id = eligibility.video_id
    JOIN transcript_segments AS segment ON segment.video_id = video.id
    JOIN speaker_assignments AS assignment
        ON assignment.segment_id = segment.id
    WHERE scope.id = NEW.scope_id
        AND subject.is_active = 1
        AND (
            policy.policy_kind = 'all_channels'
            OR (
                policy.policy_kind = 'fixed_channel'
                AND video.youtube_channel_id = policy.youtube_channel_id
            )
        )
        AND video.published_at < scope.cutoff_exclusive_utc
        AND segment.text_body IS NULL
        AND segment.text_deleted_at IS NOT NULL
        AND (
            (
                assignment.assignment_kind = 'subject'
                AND assignment.assigned_subject_id = subject.id
                AND (
                    (
                        subject.subject_kind = 'person'
                        AND assignment.assignment_origin
                            != 'channel_organization'
                    )
                    OR (
                        subject.subject_kind = 'organization'
                        AND assignment.assignment_origin
                            = 'channel_organization'
                    )
                )
            )
            OR (
                subject.subject_kind = 'person'
                AND assignment.assignment_kind = 'interviewer'
                AND assignment.assigned_subject_id IS NULL
                AND assignment.assignment_origin != 'channel_organization'
                AND EXISTS (
                    SELECT 1
                    FROM transcript_segments AS subject_segment
                    JOIN speaker_assignments AS subject_assignment
                        ON subject_assignment.segment_id = subject_segment.id
                    WHERE subject_segment.video_id = video.id
                        AND subject_segment.text_body IS NOT NULL
                        AND subject_assignment.assignment_kind = 'subject'
                        AND subject_assignment.assigned_subject_id = subject.id
                        AND subject_assignment.assignment_origin
                            != 'channel_organization'
                )
            )
        )
)
BEGIN SELECT RAISE(ABORT, 'SOURCE_TEXT_DELETED'); END;

DROP TRIGGER analysis_input_snapshots_limited_update;

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
