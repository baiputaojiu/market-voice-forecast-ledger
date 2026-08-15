import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    PolicyKind,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.repositories.sources import SourceRepository


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_video_schema_has_no_recorded_or_analysis_acquisition_date(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
    assert "published_at" in columns
    assert "recorded_at" not in columns
    assert "acquired_at" not in columns


def test_source_schema_has_no_duplicate_canonical_or_exclusion_model(db):
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    video_columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
    eligibility_columns = {
        row[1]
        for row in db.execute("PRAGMA table_info(subject_video_eligibility)")
    }

    assert "duplicate_groups" not in tables
    assert "duplicate_group_members" not in tables
    assert "canonical_video_id" not in video_columns
    assert "analysis_exclusion" not in eligibility_columns


@pytest.mark.parametrize(
    ("policy_kind", "configuration_status", "youtube_channel_id"),
    [
        ("fixed_channel", "configured", None),
        ("fixed_channel", "configured", "not-a-channel-id"),
        ("fixed_channel", "configured", "UC123456789012345678901"),
        ("all_channels", "configured", "UCVXka7buS_WptsAzSE0LcKg"),
    ],
)
def test_policy_schema_rejects_invalid_authoritative_channel_configuration(
    db, policy_kind, configuration_status, youtube_channel_id
):
    subject_id = db.execute(
        "INSERT INTO analysis_subjects(canonical_name, subject_kind) VALUES (?, ?)",
        (f"synthetic-{policy_kind}-{youtube_channel_id}", "person"),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO subject_channel_policies(
                subject_id,
                policy_kind,
                configuration_status,
                youtube_channel_id,
                channel_display_name,
                policy_hash,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_id,
                policy_kind,
                configuration_status,
                youtube_channel_id,
                "Synthetic Channel",
                "synthetic-hash",
                "2026-08-15T00:00:00.000000Z",
            ),
        )


def test_video_upsert_round_trips_utc_published_metadata(db):
    repo = SourceRepository(db)
    published_at = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)
    video_id = repo.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-video-1",
            youtube_channel_id="UCVXka7buS_WptsAzSE0LcKg",
            channel_display_name="Synthetic Channel",
            title="Synthetic upload",
            published_at=published_at,
            duration_seconds=123,
            live_kind="upload",
        )
    )

    record = repo.get_video(video_id)
    assert record.youtube_video_id == "synthetic-video-1"
    assert record.published_at == published_at
    assert record.duration_seconds == 123
    assert repo.count_videos() == 1


def test_video_upsert_updates_metadata_without_changing_identity(db):
    repo = SourceRepository(db)
    original_id = repo.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-video-1",
            youtube_channel_id=None,
            channel_display_name="Unresolved Channel",
            title="Initial title",
            published_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
            duration_seconds=10,
            live_kind="live",
        )
    )
    updated_id = repo.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-video-1",
            youtube_channel_id="UCVXka7buS_WptsAzSE0LcKg",
            channel_display_name="Resolved Channel",
            title="Updated title",
            published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            duration_seconds=20,
            live_kind="upload",
        )
    )

    assert updated_id == original_id
    assert repo.count_videos() == 1
    assert repo.get_video(original_id).title == "Updated title"


def test_policy_hash_ignores_non_authoritative_channel_display_name(db):
    repo = SourceRepository(db)
    first_subject_id = repo.create_subject("Synthetic One", SubjectKind.PERSON)
    renamed_subject_id = repo.create_subject("Synthetic Two", SubjectKind.PERSON)
    shared_policy = {
        "policy_kind": PolicyKind.FIXED_CHANNEL,
        "configuration_status": ConfigurationStatus.CONFIGURED,
        "youtube_channel_id": "UCVXka7buS_WptsAzSE0LcKg",
    }
    repo.create_policy(
        first_subject_id,
        ChannelPolicy(
            **shared_policy,
            channel_display_name="Original Channel Name",
        ),
    )
    repo.create_policy(
        renamed_subject_id,
        ChannelPolicy(
            **shared_policy,
            channel_display_name="Renamed Channel",
        ),
    )

    original = repo.get_policy(first_subject_id)
    renamed = repo.get_policy(renamed_subject_id)
    assert original.channel_display_name == "Original Channel Name"
    assert renamed.channel_display_name == "Renamed Channel"
    assert original.policy_hash == renamed.policy_hash
