CREATE TABLE voice_reference_clips (
    id INTEGER PRIMARY KEY,
    reference_profile_id INTEGER NOT NULL REFERENCES voice_reference_profiles(id),
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    clip_kind TEXT NOT NULL CHECK (
        typeof(clip_kind) = 'text'
        AND clip_kind IN ('enrollment', 'held_out_positive', 'negative')
    ),
    subject_id INTEGER NOT NULL REFERENCES analysis_subjects(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    start_ms INTEGER NOT NULL CHECK (
        typeof(start_ms) = 'integer' AND start_ms >= 0
    ),
    end_ms INTEGER NOT NULL CHECK (typeof(end_ms) = 'integer'),
    normalized_audio_sha256 TEXT NOT NULL CHECK (
        typeof(normalized_audio_sha256) = 'text'
        AND length(CAST(normalized_audio_sha256 AS BLOB)) = 64
        AND normalized_audio_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    approval_actor TEXT NOT NULL CHECK (
        typeof(approval_actor) = 'text' AND approval_actor = 'local_user'
    ),
    approval_reason TEXT NOT NULL CHECK (length(approval_reason) BETWEEN 1 AND 240),
    approved_at TEXT NOT NULL,
    clip_hash TEXT NOT NULL CHECK (
        typeof(clip_hash) = 'text'
        AND length(CAST(clip_hash AS BLOB)) = 64
        AND clip_hash NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (start_ms < end_ms),
    UNIQUE(reference_profile_id, ordinal)
);

CREATE TABLE voice_reference_features (
    id INTEGER PRIMARY KEY,
    reference_profile_id INTEGER NOT NULL UNIQUE
        REFERENCES voice_reference_profiles(id),
    encoding_version TEXT NOT NULL CHECK (
        typeof(encoding_version) = 'text'
        AND length(CAST(encoding_version AS BLOB)) BETWEEN 1 AND 256
        AND encoding_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    float_dtype TEXT NOT NULL CHECK (
        typeof(float_dtype) = 'text'
        AND length(CAST(float_dtype AS BLOB)) BETWEEN 1 AND 256
        AND float_dtype NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    dimension INTEGER NOT NULL CHECK (
        typeof(dimension) = 'integer' AND dimension > 0
    ),
    embedding_blob BLOB NOT NULL CHECK (
        typeof(embedding_blob) = 'blob' AND length(embedding_blob) > 0
    ),
    feature_sha256 TEXT NOT NULL CHECK (
        typeof(feature_sha256) = 'text'
        AND length(CAST(feature_sha256 AS BLOB)) = 64
        AND feature_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE voice_verification_manifests (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    candidate_id INTEGER NOT NULL REFERENCES subject_video_candidates(id),
    video_id INTEGER NOT NULL REFERENCES videos(id),
    profile_id INTEGER NOT NULL REFERENCES discovery_profiles(id),
    presence_decision_id INTEGER NOT NULL REFERENCES presence_decisions(id),
    presence_decision_hash TEXT NOT NULL CHECK (
        typeof(presence_decision_hash) = 'text'
        AND length(CAST(presence_decision_hash AS BLOB)) = 64
        AND presence_decision_hash NOT GLOB '*[^0-9a-f]*'
    ),
    reference_profile_id INTEGER NOT NULL REFERENCES voice_reference_profiles(id),
    reference_feature_hash TEXT NOT NULL CHECK (
        typeof(reference_feature_hash) = 'text'
        AND length(CAST(reference_feature_hash AS BLOB)) = 64
        AND reference_feature_hash NOT GLOB '*[^0-9a-f]*'
    ),
    threshold_config_version TEXT NOT NULL CHECK (
        typeof(threshold_config_version) = 'text'
        AND length(CAST(threshold_config_version AS BLOB)) BETWEEN 1 AND 256
        AND threshold_config_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ) REFERENCES speaker_threshold_configs(version),
    model_name TEXT NOT NULL CHECK (
        typeof(model_name) = 'text'
        AND length(CAST(model_name AS BLOB)) BETWEEN 1 AND 256
        AND model_name NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    model_version TEXT NOT NULL CHECK (
        typeof(model_version) = 'text'
        AND length(CAST(model_version AS BLOB)) BETWEEN 1 AND 256
        AND model_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    adapter_version TEXT NOT NULL CHECK (
        typeof(adapter_version) = 'text'
        AND length(CAST(adapter_version AS BLOB)) BETWEEN 1 AND 256
        AND adapter_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    vad_contract_version TEXT NOT NULL CHECK (
        typeof(vad_contract_version) = 'text'
        AND length(CAST(vad_contract_version AS BLOB)) BETWEEN 1 AND 256
        AND vad_contract_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    selection_contract_version TEXT NOT NULL CHECK (
        typeof(selection_contract_version) = 'text'
        AND length(CAST(selection_contract_version AS BLOB)) BETWEEN 1 AND 256
        AND selection_contract_version NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    manifest_hash TEXT NOT NULL CHECK (
        typeof(manifest_hash) = 'text'
        AND length(CAST(manifest_hash AS BLOB)) = 64
        AND manifest_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, manifest_hash)
);

CREATE TABLE voice_verification_runs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    candidate_id INTEGER NOT NULL REFERENCES subject_video_candidates(id),
    input_hash TEXT NOT NULL CHECK (
        typeof(input_hash) = 'text'
        AND length(CAST(input_hash AS BLOB)) = 64
        AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    output_hash TEXT NOT NULL CHECK (
        typeof(output_hash) = 'text'
        AND length(CAST(output_hash AS BLOB)) = 64
        AND output_hash NOT GLOB '*[^0-9a-f]*'
    ),
    proposal TEXT NOT NULL CHECK (
        typeof(proposal) = 'text'
        AND proposal IN ('likely_present', 'likely_absent', 'needs_review')
    ),
    result_code TEXT NOT NULL CHECK (
        typeof(result_code) = 'text'
        AND length(CAST(result_code AS BLOB)) BETWEEN 1 AND 64
        AND result_code NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    completed_at TEXT NOT NULL,
    UNIQUE(candidate_id, output_hash)
);

CREATE TABLE voice_verification_segments (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES voice_verification_runs(id),
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    start_ms INTEGER NOT NULL CHECK (
        typeof(start_ms) = 'integer' AND start_ms >= 0
    ),
    end_ms INTEGER NOT NULL CHECK (typeof(end_ms) = 'integer'),
    raw_match_score REAL NOT NULL CHECK (
        typeof(raw_match_score) IN ('integer', 'real')
        AND raw_match_score = raw_match_score
        AND abs(raw_match_score) <= 1.0e6
    ),
    evidence_hash TEXT NOT NULL CHECK (
        typeof(evidence_hash) = 'text'
        AND length(CAST(evidence_hash AS BLOB)) = 64
        AND evidence_hash NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (start_ms < end_ms),
    UNIQUE(run_id, ordinal)
);

CREATE TABLE voice_verification_reviews (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE REFERENCES voice_verification_runs(id),
    action TEXT NOT NULL CHECK (
        typeof(action) = 'text' AND action IN ('confirm', 'reject', 'hold')
    ),
    actor TEXT NOT NULL CHECK (
        typeof(actor) = 'text' AND actor = 'local_user'
    ),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 240),
    prior_presence_decision_id INTEGER NOT NULL REFERENCES presence_decisions(id),
    prior_presence_decision_hash TEXT NOT NULL CHECK (
        typeof(prior_presence_decision_hash) = 'text'
        AND length(CAST(prior_presence_decision_hash AS BLOB)) = 64
        AND prior_presence_decision_hash NOT GLOB '*[^0-9a-f]*'
    ),
    review_hash TEXT NOT NULL CHECK (
        typeof(review_hash) = 'text'
        AND length(CAST(review_hash AS BLOB)) = 64
        AND review_hash NOT GLOB '*[^0-9a-f]*'
    ),
    reviewed_at TEXT NOT NULL
);

CREATE TRIGGER voice_reference_clips_require_owner
BEFORE INSERT ON voice_reference_clips
WHEN NOT EXISTS (
    SELECT 1
    FROM voice_reference_profiles AS reference
    JOIN analysis_subjects AS subject ON subject.id=reference.subject_id
    JOIN videos AS video ON video.id=NEW.video_id
    WHERE reference.id=NEW.reference_profile_id
        AND reference.subject_id=NEW.subject_id
)
BEGIN SELECT RAISE(ABORT, 'VOICE_REFERENCE_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_reference_clips_require_contiguous_ordinal
BEFORE INSERT ON voice_reference_clips
WHEN NEW.ordinal != COALESCE(
    (
        SELECT MAX(existing.ordinal)
        FROM voice_reference_clips AS existing
        WHERE existing.reference_profile_id=NEW.reference_profile_id
    ),
    0
) + 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_clips_no_update
BEFORE UPDATE ON voice_reference_clips
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_clips_no_delete
BEFORE DELETE ON voice_reference_clips
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_clips_no_replace
BEFORE INSERT ON voice_reference_clips
WHEN EXISTS (
    SELECT 1
    FROM voice_reference_clips AS existing
    WHERE existing.id=NEW.id
        OR (
            existing.reference_profile_id=NEW.reference_profile_id
            AND existing.ordinal=NEW.ordinal
        )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_features_require_owner
BEFORE INSERT ON voice_reference_features
WHEN NOT EXISTS (
    SELECT 1
    FROM voice_reference_profiles AS reference
    WHERE reference.id=NEW.reference_profile_id
        AND reference.feature_hash=NEW.feature_sha256
)
BEGIN SELECT RAISE(ABORT, 'VOICE_REFERENCE_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_reference_features_no_update
BEFORE UPDATE ON voice_reference_features
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_features_no_delete
BEFORE DELETE ON voice_reference_features
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_reference_features_no_replace
BEFORE INSERT ON voice_reference_features
WHEN EXISTS (
    SELECT 1
    FROM voice_reference_features AS existing
    WHERE existing.id=NEW.id
        OR existing.reference_profile_id=NEW.reference_profile_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REFERENCE'); END;

CREATE TRIGGER voice_verification_manifests_require_owner
BEFORE INSERT ON voice_verification_manifests
WHEN NOT EXISTS (
    SELECT 1
    FROM jobs AS job
    JOIN video_pipeline_job_bindings AS binding ON binding.job_id=job.id
    JOIN subject_video_candidates AS candidate
        ON candidate.id=binding.candidate_id
    JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id
    JOIN presence_decisions AS decision
        ON decision.id=candidate.current_presence_decision_id
    JOIN voice_reference_profiles AS reference
        ON reference.id=NEW.reference_profile_id
    JOIN voice_reference_features AS feature
        ON feature.reference_profile_id=reference.id
    JOIN speaker_threshold_configs AS threshold
        ON threshold.version=reference.threshold_config_version
    WHERE job.id=NEW.job_id
        AND job.job_kind='video_pipeline'
        AND job.manifest_hash=NEW.manifest_hash
        AND binding.candidate_id=NEW.candidate_id
        AND candidate.id=NEW.candidate_id
        AND candidate.video_id=NEW.video_id
        AND candidate.profile_id=NEW.profile_id
        AND candidate.current_presence_decision_id=NEW.presence_decision_id
        AND decision.candidate_id=NEW.candidate_id
        AND decision.decision_hash=NEW.presence_decision_hash
        AND reference.subject_id=profile.subject_id
        AND reference.feature_hash=NEW.reference_feature_hash
        AND feature.feature_sha256=NEW.reference_feature_hash
        AND reference.threshold_config_version=NEW.threshold_config_version
        AND reference.model_name=NEW.model_name
        AND reference.model_version=NEW.model_version
        AND reference.adapter_version=NEW.adapter_version
        AND threshold.model_name=NEW.model_name
        AND threshold.model_version=NEW.model_version
)
BEGIN SELECT RAISE(ABORT, 'VOICE_MANIFEST_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_verification_manifests_no_update
BEFORE UPDATE ON voice_verification_manifests
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_MANIFEST'); END;

CREATE TRIGGER voice_verification_manifests_no_delete
BEFORE DELETE ON voice_verification_manifests
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_MANIFEST'); END;

CREATE TRIGGER voice_verification_manifests_no_replace
BEFORE INSERT ON voice_verification_manifests
WHEN EXISTS (
    SELECT 1
    FROM voice_verification_manifests AS existing
    WHERE existing.id=NEW.id
        OR existing.job_id=NEW.job_id
        OR (
            existing.candidate_id=NEW.candidate_id
            AND existing.manifest_hash=NEW.manifest_hash
        )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_MANIFEST'); END;

CREATE TRIGGER voice_verification_runs_require_owner
BEFORE INSERT ON voice_verification_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM voice_verification_manifests AS manifest
    WHERE manifest.job_id=NEW.job_id
        AND manifest.candidate_id=NEW.candidate_id
)
BEGIN SELECT RAISE(ABORT, 'VOICE_RUN_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_verification_runs_no_update
BEFORE UPDATE ON voice_verification_runs
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_runs_no_delete
BEFORE DELETE ON voice_verification_runs
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_runs_no_replace
BEFORE INSERT ON voice_verification_runs
WHEN EXISTS (
    SELECT 1
    FROM voice_verification_runs AS existing
    WHERE existing.id=NEW.id
        OR existing.job_id=NEW.job_id
        OR (
            existing.candidate_id=NEW.candidate_id
            AND existing.output_hash=NEW.output_hash
        )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_segments_require_owner
BEFORE INSERT ON voice_verification_segments
WHEN NOT EXISTS (
    SELECT 1
    FROM voice_verification_runs AS run
    WHERE run.id=NEW.run_id
)
BEGIN SELECT RAISE(ABORT, 'VOICE_RUN_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_verification_segments_require_contiguous_ordinal
BEFORE INSERT ON voice_verification_segments
WHEN NEW.ordinal != COALESCE(
    (
        SELECT MAX(existing.ordinal)
        FROM voice_verification_segments AS existing
        WHERE existing.run_id=NEW.run_id
    ),
    0
) + 1
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_segments_no_update
BEFORE UPDATE ON voice_verification_segments
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_segments_no_delete
BEFORE DELETE ON voice_verification_segments
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_segments_no_replace
BEFORE INSERT ON voice_verification_segments
WHEN EXISTS (
    SELECT 1
    FROM voice_verification_segments AS existing
    WHERE existing.id=NEW.id
        OR (
            existing.run_id=NEW.run_id
            AND existing.ordinal=NEW.ordinal
        )
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_RUN'); END;

CREATE TRIGGER voice_verification_reviews_require_owner
BEFORE INSERT ON voice_verification_reviews
WHEN NOT EXISTS (
    SELECT 1
    FROM voice_verification_runs AS run
    JOIN voice_verification_manifests AS manifest ON manifest.job_id=run.job_id
    JOIN presence_decisions AS decision
        ON decision.id=NEW.prior_presence_decision_id
    WHERE run.id=NEW.run_id
        AND manifest.candidate_id=run.candidate_id
        AND manifest.presence_decision_id=NEW.prior_presence_decision_id
        AND manifest.presence_decision_hash=NEW.prior_presence_decision_hash
        AND decision.candidate_id=run.candidate_id
        AND decision.decision_hash=NEW.prior_presence_decision_hash
)
BEGIN SELECT RAISE(ABORT, 'VOICE_REVIEW_OWNER_MISMATCH'); END;

CREATE TRIGGER voice_verification_reviews_no_update
BEFORE UPDATE ON voice_verification_reviews
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REVIEW'); END;

CREATE TRIGGER voice_verification_reviews_no_delete
BEFORE DELETE ON voice_verification_reviews
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REVIEW'); END;

CREATE TRIGGER voice_verification_reviews_no_replace
BEFORE INSERT ON voice_verification_reviews
WHEN EXISTS (
    SELECT 1
    FROM voice_verification_reviews AS existing
    WHERE existing.id=NEW.id OR existing.run_id=NEW.run_id
)
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_VOICE_REVIEW'); END;
