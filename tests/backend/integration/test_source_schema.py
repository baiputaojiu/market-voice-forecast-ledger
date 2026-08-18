import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.repositories.sources import SourceRepository


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_person_source_schema_has_no_legacy_policy_or_embedded_video_metadata(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subject_channel_policies" not in tables
    assert "subject_video_eligibility" not in tables
    assert tuple(row[1] for row in db.execute("PRAGMA table_info(analysis_subjects)")) == (
        "id", "canonical_name", "is_active", "created_at"
    )
    assert tuple(row[1] for row in db.execute("PRAGMA table_info(videos)")) == (
        "id", "youtube_video_id", "current_metadata_snapshot_id", "created_at"
    )


def test_subject_repository_is_person_only_and_caller_transaction_owned(db):
    repo = SourceRepository(db)
    db.execute("BEGIN")
    subject_id = repo.create_subject("Synthetic Person", aliases=("Alias",))
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM analysis_subjects WHERE id=?", (subject_id,)).fetchone()[0] == 0
