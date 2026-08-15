import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.speakers import (
    PersonalAssignmentCommand,
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.sources import VideoInput
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.speaker_assignment import (
    SpeakerAssignmentService,
)


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _personal_segment(db):
    sources = SourceRepository(db)
    subject_id = sources.create_subject("Synthetic Person", SubjectKind.PERSON)
    video_id = sources.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-personal-assignment",
            youtube_channel_id="UC1000000000000000000000",
            channel_display_name="Synthetic Interview Channel",
            title="Synthetic speaker fixture",
            published_at=FIXED_UTC,
            duration_seconds=60,
            live_kind="upload",
        )
    )
    speakers = SpeakerRepository(db)
    chunk_id = speakers.add_chunk(
        video_id=video_id,
        chunk_no=0,
        start_ms=0,
        end_ms=60_000,
        input_hash="synthetic-input-hash",
        output_hash="synthetic-output-hash",
        status=UnitStatus.SUCCESS,
    )
    segment_id = speakers.add_segment(
        video_id=video_id,
        chunk_id=chunk_id,
        segment_no=0,
        start_ms=1_000,
        end_ms=4_000,
        text_body="Synthetic subject statement.",
        anonymous_speaker_id="synthetic-speaker-a",
        transcript_created_at=FIXED_UTC,
        expires_at=FIXED_UTC + timedelta(days=365),
    )
    config = SpeakerThresholdConfig(
        version="synthetic-threshold-v1",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.50),
        interviewer_rule=ScoreRule("lte", 0.50),
    )
    speakers.add_threshold_config(config, created_at=FIXED_UTC, is_active=True)
    return subject_id, segment_id


def test_transcript_round_trips_hash_and_fixed_utc_metadata(db):
    _, segment_id = _personal_segment(db)

    segment = SpeakerRepository(db).get_segment(segment_id)

    assert segment.text_body == "Synthetic subject statement."
    assert segment.text_sha256 == (
        "8fae4e1e5cb7e8098d6e709315123824899d3dd99b9b78c76f3a19c3f28ccf36"
    )
    assert segment.transcript_created_at == FIXED_UTC
    assert segment.expires_at == FIXED_UTC + timedelta(days=365)


def test_personal_assignment_persists_raw_score_model_contract_and_evidence(db):
    subject_id, segment_id = _personal_segment(db)

    assignment = SpeakerAssignmentService(db).record_personal(
        PersonalAssignmentCommand(
            segment_id=segment_id,
            subject_id=subject_id,
            raw_match_score=1.73,
            model_name="synthetic-fixed-model",
            model_version="1.0",
            threshold_config_version="synthetic-threshold-v1",
            evidence_hash="synthetic-personal-evidence-hash",
            assigned_at=FIXED_UTC,
        )
    )

    assert assignment.assignment_kind is AssignmentKind.SUBJECT
    assert assignment.assigned_subject_id == subject_id
    assert assignment.assignment_origin is AssignmentOrigin.AUTO_VOICE
    assert assignment.raw_match_score == 1.73
    assert assignment.model_name == "synthetic-fixed-model"
    assert assignment.model_version == "1.0"
    assert assignment.threshold_config_version == "synthetic-threshold-v1"
    assert assignment.evidence_hash == "synthetic-personal-evidence-hash"
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(speaker_assignments)")
    }
    assert "normalized_match_score" not in columns


def test_personal_middle_band_is_hold_and_not_attributed_to_subject(db):
    subject_id, segment_id = _personal_segment(db)

    assignment = SpeakerAssignmentService(db).record_personal(
        PersonalAssignmentCommand(
            segment_id=segment_id,
            subject_id=subject_id,
            raw_match_score=0.90,
            model_name="synthetic-fixed-model",
            model_version="1.0",
            threshold_config_version="synthetic-threshold-v1",
            evidence_hash="synthetic-hold-evidence-hash",
            assigned_at=FIXED_UTC,
        )
    )

    assert assignment.assignment_kind is AssignmentKind.HOLD
    assert assignment.assigned_subject_id is None


@pytest.mark.parametrize(
    ("model_name", "model_version", "config_version"),
    [
        ("synthetic-other-model", "1.0", "synthetic-threshold-v1"),
        ("synthetic-fixed-model", "2.0", "synthetic-threshold-v1"),
        ("synthetic-fixed-model", "1.0", "synthetic-threshold-v2"),
    ],
)
def test_personal_assignment_rejects_non_active_model_contract(
    db, model_name, model_version, config_version
):
    subject_id, segment_id = _personal_segment(db)

    with pytest.raises(ValueError, match="active speaker threshold config"):
        SpeakerAssignmentService(db).record_personal(
            PersonalAssignmentCommand(
                segment_id=segment_id,
                subject_id=subject_id,
                raw_match_score=1.73,
                model_name=model_name,
                model_version=model_version,
                threshold_config_version=config_version,
                evidence_hash="synthetic-mismatch-evidence-hash",
                assigned_at=FIXED_UTC,
            )
        )

    assert db.execute("SELECT COUNT(*) FROM speaker_assignments").fetchone()[0] == 0


def test_schema_allows_only_one_active_threshold_config(db):
    speakers = SpeakerRepository(db)
    first = SpeakerThresholdConfig(
        version="synthetic-threshold-v1",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.50),
        interviewer_rule=ScoreRule("lte", 0.50),
    )
    second = SpeakerThresholdConfig(
        version="synthetic-threshold-v2",
        model_name="synthetic-fixed-model",
        model_version="1.0",
        subject_rule=ScoreRule("gte", 1.60),
        interviewer_rule=ScoreRule("lte", 0.40),
    )
    speakers.add_threshold_config(first, created_at=FIXED_UTC, is_active=True)

    with pytest.raises(sqlite3.IntegrityError):
        speakers.add_threshold_config(second, created_at=FIXED_UTC, is_active=True)


def test_voice_profile_schema_stores_hash_metadata_without_features(db):
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(voice_reference_profiles)")
    }

    assert {
        "feature_hash",
        "model_name",
        "model_version",
        "adapter_version",
        "threshold_config_version",
    } <= columns
    assert columns.isdisjoint({"embedding", "features", "feature_blob"})
