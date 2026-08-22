import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.repositories.voice_verification import (
    VoiceVerificationRepository,
)
from tests.backend.integration.test_presence_pilot import (
    pilot_service,
    seed_pilot_environment,
)
from tests.backend.integration.test_voice_verification_jobs import (
    presence_worker_harness,
)


@pytest.fixture
def presence_db(tmp_path: Path):
    conn = open_database(tmp_path / "presence-flow.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_exact_twenty_job_flow_stops_at_human_review(
    presence_db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_pilot_environment(presence_db)
    service = pilot_service(presence_db)
    preview = service.preview_pilot()
    creation = service.create_pilot(preview.preview_hash)
    repository = VoiceVerificationRepository(presence_db)
    first_manifest = repository.get_manifest_for_job(creation.job_ids[0])
    harness = presence_worker_harness(
        presence_db,
        tmp_path,
        SimpleNamespace(snapshot=first_manifest.snapshot),
    )

    summaries = tuple(harness.worker.run_once() for _ in range(20))
    empty_wake = harness.worker.run_once()

    assert tuple(item.job_id for item in summaries) == creation.job_ids
    assert all(item.succeeded_jobs == 1 for item in summaries)
    assert all(item.failed_code is None for item in summaries)
    assert empty_wake.job_id is None
    assert empty_wake.succeeded_jobs == 0
    assert presence_db.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_kind='video_pipeline' "
        "AND status='succeeded'"
    ).fetchone()[0] == 20
    assert presence_db.execute(
        "SELECT COUNT(*) FROM voice_verification_runs"
    ).fetchone()[0] == 20
    assert presence_db.execute(
        "SELECT COUNT(*) FROM voice_verification_segments"
    ).fetchone()[0] == 40
    assert len(repository.list_pending_reviews()) == 20
    assert presence_db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0
    assert presence_db.execute(
        "SELECT COUNT(*) FROM presence_decisions "
        "WHERE decision_origin='voice_verification'"
    ).fetchone()[0] == 0
    selected = tuple(
        presence_db.execute(
            "SELECT decision.state FROM voice_verification_manifests AS manifest "
            "JOIN subject_video_candidates AS candidate "
            "ON candidate.id=manifest.candidate_id "
            "JOIN presence_decisions AS decision "
            "ON decision.id=candidate.current_presence_decision_id "
            "ORDER BY manifest.job_id"
        )
    )
    assert len(selected) == 20
    assert all(row["state"] == "presence_unverified" for row in selected)
    artifacts = tuple(
        presence_db.execute(
            "SELECT local_path, status FROM local_artifacts ORDER BY id"
        )
    )
    assert len(artifacts) == 60
    assert all(row["status"] == "deleted" for row in artifacts)
    assert all(not Path(row["local_path"]).exists() for row in artifacts)
    assert presence_db.execute(
        "SELECT COUNT(*) FROM transcript_segments"
    ).fetchone()[0] == 0
    assert presence_db.execute(
        "SELECT COUNT(*) FROM speaker_assignments"
    ).fetchone()[0] == 0
    assert presence_db.execute(
        "SELECT COUNT(*) FROM analysis_runs"
    ).fetchone()[0] == 0
