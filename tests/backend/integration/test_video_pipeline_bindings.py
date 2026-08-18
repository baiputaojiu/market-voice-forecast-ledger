import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobKind, JobStage
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.synthetic_collection_fixture import create_synthetic_collection_candidate


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _manifest(label="one"):
    return JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (ManifestUnit("video", JobStage.VIDEO_METADATA, 1, label, (), "fixture-v1"),),
    )


def test_video_pipeline_binds_unverified_or_confirmed_candidate(db):
    for state in ("presence_unverified", "presence_confirmed"):
        fixture = create_synthetic_collection_candidate(
            db, presence_state=state, assignment_kind="subject"
        )
        job_id = JobStateService(db, clock=lambda: FIXED_UTC).create_video_pipeline(
            _manifest(state), (fixture.candidate_id,)
        )
        assert db.execute(
            "SELECT candidate_id FROM video_pipeline_job_bindings WHERE job_id=?", (job_id,)
        ).fetchone()[0] == fixture.candidate_id


def test_video_pipeline_rejects_rejected_candidate_atomically(db):
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_rejected", assignment_kind="subject"
    )
    before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with pytest.raises(DomainError) as caught:
        JobStateService(db, clock=lambda: FIXED_UTC).create_video_pipeline(
            _manifest("rejected"), (fixture.candidate_id,)
        )
    assert caught.value.code == "VIDEO_PIPELINE_INELIGIBLE"
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before


def test_video_pipeline_requires_one_video_across_candidates(db):
    first = create_synthetic_collection_candidate(
        db, presence_state="presence_unverified", assignment_kind="subject"
    )
    second = create_synthetic_collection_candidate(
        db, presence_state="presence_unverified", assignment_kind="subject"
    )
    with pytest.raises(sqlite3.IntegrityError, match="VIDEO_PIPELINE_VIDEO_MISMATCH"):
        JobStateService(db, clock=lambda: FIXED_UTC).create_video_pipeline(
            _manifest("mixed"), (first.candidate_id, second.candidate_id)
        )
