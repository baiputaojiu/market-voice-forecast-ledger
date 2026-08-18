from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import AssignmentKind
from market_voice_forecast_ledger.domain.speakers import (
    PersonalAssignmentCommand,
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.services.speaker_assignment import SpeakerAssignmentService
from tests.backend.synthetic_collection_fixture import create_synthetic_collection_candidate


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
CONFIG = SpeakerThresholdConfig(
    version="fixture-v1", model_name="fixture", model_version="1",
    subject_rule=ScoreRule("gte", 0.8), interviewer_rule=ScoreRule("lte", 0.2),
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    SpeakerRepository(conn).add_threshold_config(CONFIG, NOW, True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("score", "kind"), ((0.95, AssignmentKind.SUBJECT), (0.05, AssignmentKind.INTERVIEWER), (0.5, AssignmentKind.HOLD))
)
def test_personal_assignment_classifies_and_persists_current_row_id(db, score, kind):
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_confirmed", assignment_kind="hold"
    )
    assignment = SpeakerAssignmentService(db, clock=lambda: NOW).record_personal(
        PersonalAssignmentCommand(
            segment_id=fixture.segment_id, subject_id=fixture.subject_id,
            raw_match_score=score, model_name="fixture", model_version="1",
            threshold_config_version="fixture-v1", evidence_hash=f"evidence-{score}",
            assigned_at=NOW,
        )
    )
    assert assignment.assignment_kind is kind
    stored = SpeakerRepository(db).get_assignment(fixture.segment_id)
    assert stored.id is not None
    assert stored.assignment_kind is kind


def test_personal_assignment_requires_active_subject(db):
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_confirmed", assignment_kind="hold"
    )
    db.execute("UPDATE analysis_subjects SET is_active=0 WHERE id=?", (fixture.subject_id,))
    with pytest.raises(ValueError):
        SpeakerAssignmentService(db, clock=lambda: NOW).record_personal(
            PersonalAssignmentCommand(
                segment_id=fixture.segment_id, subject_id=fixture.subject_id,
                raw_match_score=0.95, model_name="fixture", model_version="1",
                threshold_config_version="fixture-v1", evidence_hash="inactive",
                assigned_at=NOW,
            )
        )
