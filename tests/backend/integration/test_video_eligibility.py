from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    DiscoveryMethod,
    EligibilityStatus,
)
from market_voice_forecast_ledger.domain.sources import VideoInput
from market_voice_forecast_ledger.repositories.audit import AuditRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _video(
    youtube_video_id: str,
    youtube_channel_id: str | None,
    channel_display_name: str = "Synthetic Channel",
) -> VideoInput:
    return VideoInput(
        youtube_video_id=youtube_video_id,
        youtube_channel_id=youtube_channel_id,
        channel_display_name=channel_display_name,
        title=f"Synthetic {youtube_video_id}",
        published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        duration_seconds=60,
        live_kind="upload",
    )


@pytest.fixture
def synthetic_other_channel_video(db):
    bootstrap_reference_data(db)
    return SourceRepository(db).upsert_video(
        _video(
            "synthetic-emori-guest-video",
            "UC9999999999999999999999",
            channel_display_name="江守哲の米国株投資チャンネル",
        )
    )


@pytest.fixture
def four_distinct_video_ids(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    return tuple(
        repo.upsert_video(_video(youtube_video_id, channel_id))
        for youtube_video_id, channel_id in (
            ("synthetic-original", "UC1000000000000000000000"),
            ("synthetic-clip", "UC2000000000000000000000"),
            ("synthetic-short", "UC3000000000000000000000"),
            ("synthetic-repost", "UC4000000000000000000000"),
        )
    )


def test_manual_url_cannot_bypass_emori_fixed_channel(
    db, synthetic_other_channel_video
):
    decision = ChannelPolicyService(db).evaluate_by_subject_name(
        "江守哲", synthetic_other_channel_video, DiscoveryMethod.MANUAL_URL
    )

    assert decision.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE
    assert decision.may_download_audio is False
    assert decision.may_analyze is False


def test_original_clip_short_and_repost_are_independent(db, four_distinct_video_ids):
    service = ChannelPolicyService(db)
    decisions = [
        service.evaluate_by_subject_name(
            "木野内栄治", video_id, DiscoveryMethod.AUTO_SEARCH
        )
        for video_id in four_distinct_video_ids
    ]

    assert [item.status for item in decisions] == [EligibilityStatus.ELIGIBLE] * 4
    assert SourceRepository(db).count_videos() == 4
    persisted = db.execute(
        """
        SELECT video_id, status
        FROM subject_video_eligibility
        ORDER BY video_id
        """
    ).fetchall()
    assert [(row["video_id"], row["status"]) for row in persisted] == [
        (video_id, EligibilityStatus.ELIGIBLE.value)
        for video_id in four_distinct_video_ids
    ]


def test_evaluation_persists_current_policy_snapshot_and_discovery_method(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    policy = repo.get_policy_by_subject_name("江守哲")
    video_id = repo.upsert_video(
        _video("synthetic-emori-official", policy.youtube_channel_id)
    )

    decision = ChannelPolicyService(db).evaluate(
        policy.subject_id, video_id, DiscoveryMethod.MANUAL_URL
    )

    row = db.execute(
        "SELECT * FROM subject_video_eligibility WHERE subject_id=? AND video_id=?",
        (policy.subject_id, video_id),
    ).fetchone()
    decided_at = datetime.fromisoformat(row["decided_at"].replace("Z", "+00:00"))
    assert decision.status is EligibilityStatus.ELIGIBLE
    assert row["discovery_method"] == DiscoveryMethod.MANUAL_URL.value
    assert row["status"] == EligibilityStatus.ELIGIBLE.value
    assert row["policy_id"] == policy.id
    assert row["policy_hash"] == policy.policy_hash
    assert row["decision_reason"] == "FIXED_CHANNEL_MATCH"
    assert decided_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("by_subject_name", [False, True], ids=["subject-id", "name"])
def test_evaluation_reads_video_after_acquiring_write_transaction(
    db, by_subject_name
):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    policy = repo.get_policy_by_subject_name("江守哲")
    video_id = repo.upsert_video(
        _video("synthetic-concurrent-channel-change", policy.youtube_channel_id)
    )
    database_path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    concurrent_db = open_database(database_path)
    updates = []
    callback_errors = []

    def update_channel_before_begin(statement):
        if statement != "BEGIN IMMEDIATE" or updates:
            return
        try:
            concurrent_db.execute(
                "UPDATE videos SET youtube_channel_id=? WHERE id=?",
                ("UC9999999999999999999999", video_id),
            )
            updates.append(concurrent_db.total_changes)
        except Exception as error:  # pragma: no cover - asserted diagnostic path
            callback_errors.append(error)

    db.set_trace_callback(update_channel_before_begin)
    try:
        service = ChannelPolicyService(db)
        if by_subject_name:
            service.evaluate_by_subject_name(
                "江守哲", video_id, DiscoveryMethod.AUTO_SEARCH
            )
        else:
            service.evaluate(
                policy.subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
            )
    finally:
        db.set_trace_callback(None)
        concurrent_db.close()

    row = db.execute(
        "SELECT status, decision_reason FROM subject_video_eligibility"
    ).fetchone()
    assert callback_errors == []
    assert updates == [1]
    assert row["status"] == EligibilityStatus.CHANNEL_OUT_OF_SCOPE.value
    assert row["decision_reason"] == "FIXED_CHANNEL_MISMATCH"


def test_reevaluation_replaces_one_current_decision_without_merging_video(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    policy = repo.get_policy_by_subject_name("江守哲")
    video_input = _video(
        "synthetic-current-decision", "UC9999999999999999999999"
    )
    video_id = repo.upsert_video(video_input)
    service = ChannelPolicyService(db)
    first = service.evaluate(
        policy.subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    repo.upsert_video(
        _video("synthetic-current-decision", policy.youtube_channel_id)
    )

    second = service.evaluate(
        policy.subject_id, video_id, DiscoveryMethod.MANUAL_URL
    )

    rows = db.execute(
        "SELECT * FROM subject_video_eligibility WHERE subject_id=? AND video_id=?",
        (policy.subject_id, video_id),
    ).fetchall()
    assert first.status is EligibilityStatus.CHANNEL_OUT_OF_SCOPE
    assert second.status is EligibilityStatus.ELIGIBLE
    assert len(rows) == 1
    assert rows[0]["discovery_method"] == DiscoveryMethod.MANUAL_URL.value
    assert rows[0]["status"] == EligibilityStatus.ELIGIBLE.value
    assert repo.count_videos() == 1


def test_eligibility_and_audit_persistence_roll_back_together(
    db, monkeypatch, synthetic_other_channel_video
):
    def fail_append(self, event):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(AuditRepository, "append", fail_append)

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        ChannelPolicyService(db).evaluate_by_subject_name(
            "江守哲", synthetic_other_channel_video, DiscoveryMethod.MANUAL_URL
        )

    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_eligibility"
    ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
