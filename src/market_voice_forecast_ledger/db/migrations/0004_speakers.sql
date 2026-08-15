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

CREATE UNIQUE INDEX one_active_speaker_threshold_config
ON speaker_threshold_configs(is_active)
WHERE is_active = 1;

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

CREATE UNIQUE INDEX one_active_voice_reference_profile_per_subject
ON voice_reference_profiles(subject_id)
WHERE is_active = 1;

CREATE TABLE speaker_assignments (
    segment_id INTEGER PRIMARY KEY REFERENCES transcript_segments(id),
    assignment_kind TEXT NOT NULL CHECK (assignment_kind IN ('subject', 'interviewer', 'hold')),
    assigned_subject_id INTEGER REFERENCES analysis_subjects(id),
    assignment_origin TEXT NOT NULL CHECK (assignment_origin IN ('auto_voice', 'manual', 'channel_organization')),
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
    ),
    CHECK (
        assignment_origin != 'channel_organization'
        OR (
            assignment_kind = 'subject'
            AND assigned_subject_id IS NOT NULL
            AND raw_match_score IS NULL
            AND model_name IS NULL
            AND model_version IS NULL
            AND threshold_config_version IS NULL
        )
    )
);
