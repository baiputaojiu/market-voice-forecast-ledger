import sqlite3

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from tests.backend.synthetic_collection_fixture import (
    create_synthetic_collection_candidate,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _current_state_counts(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "analysis_scopes",
            "analysis_runs",
            "current_result_sets",
            "current_statements",
            "current_asset_mappings",
            "current_forecasts",
        )
    )


@pytest.mark.parametrize("assignment_kind", ("subject", "interviewer", "hold"))
def test_analysis_selects_only_confirmed_subject_speech(db, assignment_kind):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_confirmed",
        assignment_kind=assignment_kind,
    )

    selected = AnalysisRunService(db).preview_input(
        fixture.subject_id, fixture.cutoff
    )

    assert tuple(item.segment_id for item in selected) == (
        (fixture.segment_id,) if assignment_kind == "subject" else ()
    )
    assert all(
        item.presence_decision_id == fixture.presence_decision_id
        for item in selected
    )
    assert all(
        item.speaker_assignment_id == fixture.speaker_assignment_id
        for item in selected
    )


@pytest.mark.parametrize(
    "presence_state", ("presence_unverified", "presence_rejected")
)
def test_analysis_rejects_nonconfirmed_presence_without_mutating_current_state(
    db, presence_state
):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state=presence_state,
        assignment_kind="subject",
    )
    before = _current_state_counts(db)

    selected = AnalysisRunService(db).preview_input(
        fixture.subject_id, fixture.cutoff
    )

    assert selected == ()
    assert _current_state_counts(db) == before


def test_analysis_rejects_current_assignment_for_a_different_subject(db):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_confirmed",
        assignment_kind="subject",
        assigned_subject_id="different",
    )
    before = _current_state_counts(db)

    selected = AnalysisRunService(db).preview_input(
        fixture.subject_id, fixture.cutoff
    )

    assert selected == ()
    assert _current_state_counts(db) == before


def test_analysis_fails_closed_on_metadata_snapshot_hash_mismatch(db):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_confirmed",
        assignment_kind="subject",
    )
    db.execute("DROP TRIGGER video_metadata_snapshots_no_update")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "UPDATE video_metadata_snapshots SET canonical_hash='corrupt' WHERE id=?",
        (fixture.metadata_snapshot_id,),
    )
    db.execute("PRAGMA foreign_keys=ON")
    before = _current_state_counts(db)

    with pytest.raises(DomainError) as caught:
        AnalysisRunService(db).preview_input(fixture.subject_id, fixture.cutoff)

    assert caught.value.code == "STORED_METADATA_HASH_MISMATCH"
    assert _current_state_counts(db) == before


def test_analysis_fails_closed_on_presence_decision_hash_mismatch(db):
    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_confirmed",
        assignment_kind="subject",
    )
    db.execute("DROP TRIGGER presence_decisions_no_update")
    db.execute(
        "UPDATE presence_decisions SET decision_hash='corrupt' WHERE id=?",
        (fixture.presence_decision_id,),
    )
    before = _current_state_counts(db)

    with pytest.raises(DomainError) as caught:
        AnalysisRunService(db).preview_input(fixture.subject_id, fixture.cutoff)

    assert caught.value.code == "STORED_PRESENCE_HASH_MISMATCH"
    assert _current_state_counts(db) == before
