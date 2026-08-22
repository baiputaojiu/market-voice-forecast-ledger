import hashlib
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoverySourceKind,
    LiveState,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.voice_verification import ReferenceClipCommand
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.voice_verification import (
    VoiceVerificationRepository,
)


NOW = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)
FEATURE_BYTES = struct.pack("<4f", 0.25, -0.5, 0.75, 1.0)
FEATURE_HASH = hashlib.sha256(FEATURE_BYTES).hexdigest()


@dataclass(frozen=True)
class ReferenceSeed:
    subject_id: int
    profile_id: int
    video_id: int
    candidate_id: int
    decision_id: int
    decision_hash: str
    reference_profile_id: int


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "voice-reference.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def seed_reference_profile(db: sqlite3.Connection) -> ReferenceSeed:
    profile = db.execute(
        "SELECT id, subject_id FROM discovery_profiles ORDER BY id LIMIT 1"
    ).fetchone()
    discovery_job_id = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
        """,
        ("discovery-fixture", utc_iso(NOW), utc_iso(NOW)),
    ).lastrowid
    metadata = CanonicalVideoMetadata.build(
        youtube_video_id="abcdefghijk",
        channel_id="UCabcdefghijklmnopqrstuv",
        channel_title="Synthetic channel",
        title="Synthetic reference source",
        description="",
        published_at=NOW,
        duration_seconds=600,
        live_state=LiveState.NOT_LIVE,
        actual_start_time=None,
        schema_version="youtube-video-metadata.v1",
        fetched_at=NOW,
    )
    with transaction(db):
        candidate = DiscoveryRepository(db).create_initial_candidate(
            profile_id=profile["id"],
            job_id=discovery_job_id,
            metadata=metadata,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key="synthetic-search",
            observation_hash="1" * 64,
            idempotency_key="2" * 64,
            observed_at=NOW,
        )
        SpeakerRepository(db).add_threshold_config(
            SpeakerThresholdConfig(
                version="voice-threshold-v1",
                model_name="speaker-model",
                model_version="1.0",
                subject_rule=ScoreRule("gte", 0.7),
                interviewer_rule=ScoreRule("lte", 0.2),
            ),
            NOW,
            True,
        )
        reference_profile_id = db.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (?, 'speaker-model', '1.0', 'adapter-v1', ?,
                      'voice-threshold-v1', ?, 1)
            """,
            (profile["subject_id"], FEATURE_HASH, utc_iso(NOW)),
        ).lastrowid
    decision = db.execute(
        "SELECT decision_hash FROM presence_decisions WHERE id=?",
        (candidate.current_presence_decision_id,),
    ).fetchone()
    return ReferenceSeed(
        subject_id=profile["subject_id"],
        profile_id=profile["id"],
        video_id=candidate.video_id,
        candidate_id=candidate.id,
        decision_id=candidate.current_presence_decision_id,
        decision_hash=decision["decision_hash"],
        reference_profile_id=reference_profile_id,
    )


def expected_clip_hash(
    seed: ReferenceSeed,
    *,
    ordinal: int,
    clip_kind: str,
    start_ms: int,
    end_ms: int,
    audio_hash: str,
    reason: str,
    approved_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "approval_actor": "local_user",
                "approval_reason": reason,
                "approved_at": utc_iso(approved_at),
                "clip_kind": clip_kind,
                "end_ms": end_ms,
                "normalized_audio_sha256": audio_hash,
                "ordinal": ordinal,
                "reference_profile_id": seed.reference_profile_id,
                "schema": "voice-reference-clip.v1",
                "start_ms": start_ms,
                "subject_id": seed.subject_id,
                "video_id": seed.video_id,
            }
        )
    )


def add_valid_clip(
    db: sqlite3.Connection, seed: ReferenceSeed, *, ordinal: int = 1
) -> int:
    return VoiceVerificationRepository(db).add_reference_clip(
        seed.reference_profile_id,
        ordinal,
        "enrollment",
        ReferenceClipCommand(
            subject_id=seed.subject_id,
            video_id=seed.video_id,
            start_ms=1_000,
            end_ms=18_000,
            actor="local_user",
            reason="clear solo speech",
        ),
        normalized_audio_sha256="a" * 64,
        approved_at=NOW,
    )


def test_reference_bundle_recomputes_clip_and_feature_hashes(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)

    feature_id = repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    clip_id = add_valid_clip(db, seed)

    bundle = repository.get_reference_bundle(seed.reference_profile_id)
    assert bundle.feature.id == feature_id
    assert bundle.feature.embedding_blob == FEATURE_BYTES
    assert bundle.feature.feature_sha256 == FEATURE_HASH
    assert tuple(clip.id for clip in bundle.clips) == (clip_id,)
    assert bundle.clips[0].clip_hash == expected_clip_hash(
        seed,
        ordinal=1,
        clip_kind="enrollment",
        start_ms=1_000,
        end_ms=18_000,
        audio_hash="a" * 64,
        reason="clear solo speech",
        approved_at=NOW,
    )


def test_reference_write_prevalidates_before_insert(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)

    with pytest.raises(DomainError, match="VOICE_REFERENCE_INVALID"):
        repository.add_reference_clip(
            seed.reference_profile_id,
            1,
            "enrollment",
            ReferenceClipCommand(
                subject_id=seed.subject_id,
                video_id=seed.video_id,
                start_ms=2_000,
                end_ms=1_000,
                actor="local_user",
                reason="clear solo speech",
            ),
            normalized_audio_sha256="a" * 64,
            approved_at=NOW,
        )

    assert db.execute("SELECT COUNT(*) FROM voice_reference_clips").fetchone()[0] == 0


def test_reference_read_rejects_corrupt_blob_without_healing(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    add_valid_clip(db, seed)
    db.execute("DROP TRIGGER voice_reference_features_no_update")
    db.execute(
        "UPDATE voice_reference_features SET embedding_blob=? "
        "WHERE reference_profile_id=?",
        (sqlite3.Binary(b"tampered-private-feature"), seed.reference_profile_id),
    )

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        repository.get_reference_bundle(seed.reference_profile_id)

    assert db.execute(
        "SELECT embedding_blob FROM voice_reference_features "
        "WHERE reference_profile_id=?",
        (seed.reference_profile_id,),
    ).fetchone()[0] == b"tampered-private-feature"


def test_reference_read_rejects_non_integer_storage_type(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    add_valid_clip(db, seed)
    db.execute("DROP TRIGGER voice_reference_clips_no_update")
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute(
        "UPDATE voice_reference_clips SET ordinal=1.5 WHERE reference_profile_id=?",
        (seed.reference_profile_id,),
    )

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        repository.get_reference_bundle(seed.reference_profile_id)
