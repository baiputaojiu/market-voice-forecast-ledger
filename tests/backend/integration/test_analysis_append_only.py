import json
import sqlite3

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import AnalysisRunStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
    _prepare_personal_analysis,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "id_query"),
    [
        ("analysis_runs", "SELECT id FROM analysis_runs LIMIT 1"),
        ("analysis_run_events", "SELECT id FROM analysis_run_events LIMIT 1"),
        ("analysis_run_segments", "SELECT id FROM analysis_run_segments LIMIT 1"),
        (
            "analysis_run_job_attempts",
            "SELECT id FROM analysis_run_job_attempts LIMIT 1",
        ),
    ],
)
def test_run_owned_rows_reject_raw_update_and_delete(db, table, id_query):
    _begin(db, _prepare_personal_analysis(db))
    row_id = db.execute(id_query).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(f"UPDATE {table} SET id=id WHERE id=?", (row_id,))
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))


def test_snapshot_rejects_content_metadata_and_partial_expiry_edits(db):
    run = _begin(db, _prepare_personal_analysis(db))

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_ANALYSIS_SNAPSHOT"):
        db.execute(
            "UPDATE analysis_input_snapshots SET input_text=? WHERE run_id=?",
            ("Edited synthetic content.", run.id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_ANALYSIS_SNAPSHOT"):
        db.execute(
            "UPDATE analysis_input_snapshots SET metadata_json=? WHERE run_id=?",
            ("{}", run.id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_ANALYSIS_SNAPSHOT"):
        db.execute(
            "UPDATE analysis_input_snapshots SET input_text=NULL WHERE run_id=?",
            (run.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "DELETE FROM analysis_input_snapshots WHERE run_id=?", (run.id,)
        )


def test_snapshot_allows_exactly_one_text_expiry_transition(db):
    run = _begin(db, _prepare_personal_analysis(db))
    before = dict(
        db.execute(
            "SELECT * FROM analysis_input_snapshots WHERE run_id=?", (run.id,)
        ).fetchone()
    )
    deleted_at = "2027-08-15T00:00:00.000000Z"

    db.execute(
        """
        UPDATE analysis_input_snapshots
        SET input_text=NULL, text_deleted_at=?
        WHERE run_id=?
        """,
        (deleted_at, run.id),
    )

    after = dict(
        db.execute(
            "SELECT * FROM analysis_input_snapshots WHERE run_id=?", (run.id,)
        ).fetchone()
    )
    assert after == before | {"input_text": None, "text_deleted_at": deleted_at}
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_ANALYSIS_SNAPSHOT"):
        db.execute(
            "UPDATE analysis_input_snapshots SET text_deleted_at=? WHERE run_id=?",
            ("2027-08-16T00:00:00.000000Z", run.id),
        )


def test_full_text_exists_only_in_private_snapshot_body(db):
    run = _begin(db, _prepare_personal_analysis(db))
    snapshot = db.execute(
        "SELECT input_text, metadata_json FROM analysis_input_snapshots WHERE run_id=?",
        (run.id,),
    ).fetchone()
    metadata = json.loads(snapshot["metadata_json"])
    serialized_job_events = " ".join(
        row["metadata_json"]
        for row in db.execute("SELECT metadata_json FROM job_events")
    )

    assert "Synthetic subject evidence." in snapshot["input_text"]
    assert "Synthetic subject evidence." not in snapshot["metadata_json"]
    assert "Synthetic subject evidence." not in serialized_job_events
    assert "input_sha256" in metadata
    assert metadata["segments"][0]["text_sha256"]
    assert "metadata_json" not in {
        row["name"] for row in db.execute("PRAGMA table_info(analysis_run_events)")
    }


def test_run_events_append_and_latest_id_defines_effective_status(db):
    run = _begin(db, _prepare_personal_analysis(db))
    repository = AnalysisRepository(db)

    with transaction(db):
        event_id = repository.append_run_event(
            run.id, AnalysisRunStatus.FAILED, "synthetic_safe_failure"
        )

    assert event_id > 0
    assert repository.get_effective_run_status(run.id) is AnalysisRunStatus.FAILED
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events WHERE run_id=?", (run.id,)
    ).fetchone()[0] == 2


def test_run_event_rejects_unsafe_error_code_without_writing(db):
    run = _begin(db, _prepare_personal_analysis(db))
    repository = AnalysisRepository(db)

    with pytest.raises(DomainError) as error:
        with transaction(db):
            repository.append_run_event(
                run.id, AnalysisRunStatus.FAILED, "unsafe body: synthetic text"
            )

    assert error.value.code == "UNSAFE_ANALYSIS_ERROR_CODE"
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events WHERE run_id=?", (run.id,)
    ).fetchone()[0] == 1
