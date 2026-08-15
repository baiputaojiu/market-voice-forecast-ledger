from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    DiscoveryMethod,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.sources import VideoInput
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.speaker_assignment import (
    SpeakerAssignmentService,
)


FIXED_UTC = datetime(2026, 8, 15, tzinfo=timezone.utc)


@dataclass(frozen=True)
class OrganizationVideo:
    subject_id: int
    video_id: int


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def akatsuki_video_with_three_roles(db):
    bootstrap_reference_data(db)
    sources = SourceRepository(db)
    policy = sources.get_policy_by_subject_name("暁投資顧問")
    video_id = sources.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-akatsuki-official",
            youtube_channel_id=policy.youtube_channel_id,
            channel_display_name="Synthetic Organization Channel",
            title="Synthetic organization fixture",
            published_at=FIXED_UTC,
            duration_seconds=60,
            live_kind="upload",
        )
    )
    ChannelPolicyService(db).evaluate(
        policy.subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    speakers = SpeakerRepository(db)
    chunk_id = speakers.add_chunk(
        video_id=video_id,
        chunk_no=0,
        start_ms=0,
        end_ms=60_000,
        input_hash="synthetic-organization-input-hash",
        output_hash="synthetic-organization-output-hash",
        status=UnitStatus.SUCCESS,
    )
    for segment_no, anonymous_role in enumerate(
        ("internal-presenter", "external-guest", "interviewer")
    ):
        speakers.add_segment(
            video_id=video_id,
            chunk_id=chunk_id,
            segment_no=segment_no,
            start_ms=segment_no * 10_000,
            end_ms=(segment_no + 1) * 10_000,
            text_body=f"Synthetic statement {segment_no}.",
            anonymous_speaker_id=anonymous_role,
            transcript_created_at=FIXED_UTC,
            expires_at=None,
        )
    return OrganizationVideo(subject_id=policy.subject_id, video_id=video_id)


def test_akatsuki_assigns_every_official_channel_segment_to_organization(
    db, akatsuki_video_with_three_roles
):
    fixture = akatsuki_video_with_three_roles

    ids = SpeakerAssignmentService(db).assign_organization_video(
        fixture.subject_id,
        fixture.video_id,
    )
    rows = SpeakerRepository(db).list_assignments(ids)

    assert len(rows) == 3
    assert {row.assignment_kind for row in rows} == {AssignmentKind.SUBJECT}
    assert {row.assignment_origin for row in rows} == {
        AssignmentOrigin.CHANNEL_ORGANIZATION
    }
    assert {row.assigned_subject_id for row in rows} == {fixture.subject_id}
    assert {row.raw_match_score for row in rows} == {None}
    assert {row.model_name for row in rows} == {None}
    assert {row.model_version for row in rows} == {None}
    assert {row.threshold_config_version for row in rows} == {None}


def test_organization_assignment_rejects_stale_eligibility_policy_hash(
    db, akatsuki_video_with_three_roles
):
    fixture = akatsuki_video_with_three_roles
    db.execute(
        "UPDATE subject_channel_policies SET policy_hash=? WHERE subject_id=?",
        ("synthetic-new-policy-hash", fixture.subject_id),
    )

    with pytest.raises(ValueError, match="current eligible channel policy"):
        SpeakerAssignmentService(db).assign_organization_video(
            fixture.subject_id, fixture.video_id
        )

    assert db.execute("SELECT COUNT(*) FROM speaker_assignments").fetchone()[0] == 0


def test_organization_assignment_rejects_video_moved_off_official_channel(
    db, akatsuki_video_with_three_roles
):
    fixture = akatsuki_video_with_three_roles
    db.execute(
        "UPDATE videos SET youtube_channel_id=? WHERE id=?",
        ("UC9999999999999999999999", fixture.video_id),
    )

    with pytest.raises(ValueError, match="current eligible channel policy"):
        SpeakerAssignmentService(db).assign_organization_video(
            fixture.subject_id, fixture.video_id
        )

    assert db.execute("SELECT COUNT(*) FROM speaker_assignments").fetchone()[0] == 0


def test_organization_assignment_rejects_person_subject(
    db, akatsuki_video_with_three_roles
):
    fixture = akatsuki_video_with_three_roles
    person_id = SourceRepository(db).create_subject(
        "Synthetic Person", SubjectKind.PERSON
    )

    with pytest.raises(ValueError, match="organization subject"):
        SpeakerAssignmentService(db).assign_organization_video(
            person_id, fixture.video_id
        )
