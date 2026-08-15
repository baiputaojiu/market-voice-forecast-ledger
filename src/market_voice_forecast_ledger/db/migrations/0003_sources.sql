CREATE TABLE analysis_subjects (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    subject_kind TEXT NOT NULL CHECK (subject_kind IN ('person', 'organization')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);
CREATE TABLE subject_aliases (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    alias TEXT NOT NULL,
    UNIQUE(subject_id, alias)
);
CREATE TABLE subject_channel_policies (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL UNIQUE REFERENCES analysis_subjects(id),
    policy_kind TEXT NOT NULL CHECK (policy_kind IN ('all_channels', 'fixed_channel')),
    configuration_status TEXT NOT NULL CHECK (configuration_status IN ('configured', 'configuration_required')),
    youtube_channel_id TEXT,
    channel_display_name TEXT,
    policy_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (policy_kind = 'all_channels' AND youtube_channel_id IS NULL)
        OR
        (policy_kind = 'fixed_channel' AND (
            configuration_status = 'configuration_required'
            OR (
                configuration_status = 'configured'
                AND youtube_channel_id IS NOT NULL
                AND length(youtube_channel_id) = 24
                AND substr(youtube_channel_id, 1, 2) = 'UC'
            )
        ))
    )
);
CREATE TABLE videos (
    id INTEGER PRIMARY KEY,
    youtube_video_id TEXT NOT NULL UNIQUE,
    youtube_channel_id TEXT,
    channel_display_name TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds >= 0),
    live_kind TEXT NOT NULL CHECK (live_kind IN ('upload', 'live'))
);
CREATE TABLE subject_video_eligibility (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    discovery_method TEXT NOT NULL CHECK (discovery_method IN ('auto_search', 'manual_url')),
    status TEXT NOT NULL CHECK (status IN ('eligible', 'channel_out_of_scope', 'configuration_required', 'channel_unresolved')),
    policy_id INTEGER NOT NULL REFERENCES subject_channel_policies(id),
    policy_hash TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    UNIQUE(subject_id, video_id)
);
