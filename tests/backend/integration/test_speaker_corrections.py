from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import AssignmentKind, AssignmentOrigin
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.services.corrections import SpeakerCorrection, SpeakerCorrectionService
from tests.backend.synthetic_collection_fixture import create_synthetic_collection_candidate


NOW = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_manual_personal_correction_is_append_only_and_audited(db):
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_confirmed", assignment_kind="hold"
    )
    corrected = SpeakerCorrectionService(db, clock=lambda: NOW).correct(
        SpeakerCorrection(fixture.segment_id, AssignmentKind.SUBJECT, fixture.subject_id, "user", "verified speaker")
    )
    assert corrected.assignment_origin is AssignmentOrigin.MANUAL
    assert SpeakerRepository(db).get_assignment(fixture.segment_id).assigned_subject_id == fixture.subject_id
    events = db.execute(
        "SELECT after_json FROM audit_events WHERE entity_type='speaker_assignment' AND entity_id=?",
        (str(fixture.segment_id),),
    ).fetchall()
    assert len(events) == 1
    assert "Synthetic subject statement" not in events[0]["after_json"]


def test_subject_correction_requires_confirmed_presence(db):
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_unverified", assignment_kind="hold"
    )
    with pytest.raises(DomainError) as caught:
        SpeakerCorrectionService(db, clock=lambda: NOW).correct(
            SpeakerCorrection(fixture.segment_id, AssignmentKind.SUBJECT, fixture.subject_id, "user", "unsafe")
        )
    assert caught.value.code == "SPEAKER_CORRECTION_SUBJECT_INVALID"
