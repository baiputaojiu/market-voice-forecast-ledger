from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import ChannelUploads, YouTubePage
from tests.backend.youtube_fakes import (
    FakeYouTubeClient,
    synthetic_search_item,
    synthetic_video_item,
)


NOW = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
HELPER = Path(__file__).with_name("crash_youtube_sync_worker.py")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def db(db_path):
    conn = open_database(db_path)
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _single_seed_profile(db):
    profiles = DiscoveryRepository(db).list_active_profile_versions()
    selected = profiles[0]
    db.execute(
        "UPDATE discovery_profiles SET is_active=0 WHERE id<>?",
        (selected.profile_id,),
    )
    return selected


def _prepare_seed_success(db, *, with_observation: bool = False):
    profile = _single_seed_profile(db)
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    video_id = "recover0001"
    playlist_items = ()
    video_responses = ()
    if with_observation:
        from tests.backend.youtube_fakes import synthetic_playlist_item

        playlist_items = (synthetic_playlist_item(video_id, playlist_id),)
        video_responses = ((synthetic_video_item(video_id=video_id),),)
    client = FakeYouTubeClient(
        channel_responses=((ChannelUploads(channel_id, playlist_id),),),
        playlist_responses=(YouTubePage(playlist_items, None),),
        video_responses=video_responses,
    )
    service = YouTubeSyncService(db, clock=lambda: NOW, youtube_client=client)
    request = service.request_full_sync(NOW)
    claimed = service.claim_next_runnable(NOW)
    assert claimed is not None and claimed.job_id == request.job_id
    running = db.execute(
        "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
        (request.job_id,),
    ).fetchone()
    service.execute_seed_unit(request.job_id, running["unit_key"])
    return profile, request.job_id, running["unit_key"]


def _run_crash_helper(db_path: Path, job_id: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), "crash", str(db_path), str(job_id), _utc_text(NOW)],
        shell=False,
        check=False,
        capture_output=True,
        timeout=30,
    )


def test_process_crash_recovery_preserves_verified_success_and_finishes_once(
    db, db_path
):
    profile, job_id, seed_key = _prepare_seed_success(db)
    seed_before = dict(
        db.execute(
            "SELECT status, output_hash, attempt_count FROM job_units "
            "WHERE job_id=? AND unit_key=?",
            (job_id, seed_key),
        ).fetchone()
    )

    crashed = _run_crash_helper(db_path, job_id)

    assert crashed.returncode == 91
    assert JobStateService(db).status(job_id) is JobStatus.RUNNING
    running = db.execute(
        "SELECT unit_key, status FROM job_units "
        "WHERE job_id=? AND status='running'",
        (job_id,),
    ).fetchone()
    assert running is not None
    search_key = running["unit_key"]
    crash_checkpoint = DiscoveryRepository(db).get_youtube_sync_checkpoint(
        job_id, search_key
    )
    assert crash_checkpoint.page_count == 1
    crash_window = DiscoveryRepository(db).next_search_window(job_id, search_key)
    assert crash_window is not None
    assert crash_window.next_page_token == "crash_checkpoint_token"

    plan = YouTubeSyncService(db, clock=lambda: NOW).recover_interrupted_job(job_id)

    assert plan.reused_unit_keys == (seed_key,)
    assert search_key in plan.pending_unit_keys
    assert dict(
        db.execute(
            "SELECT status, output_hash, attempt_count FROM job_units "
            "WHERE job_id=? AND unit_key=?",
            (job_id, seed_key),
        ).fetchone()
    ) == seed_before
    interrupted = tuple(
        db.execute(
            "SELECT attempt_no, result_status, output_hash, error_code "
            "FROM job_unit_attempts WHERE job_id=? AND unit_key=? ORDER BY attempt_no",
            (job_id, search_key),
        )
    )
    assert tuple(tuple(row) for row in interrupted) == ((1, "interrupted", None, None),)

    video_id = "recover0002"
    resumed = YouTubeSyncService(
        db,
        clock=lambda: NOW,
        youtube_client=FakeYouTubeClient(
            search_responses=(
                YouTubePage((synthetic_search_item(video_id),), None),
            ),
            video_responses=((synthetic_video_item(video_id=video_id),),),
        ),
    )
    claimed = resumed.claim_next_runnable(NOW)
    assert claimed is not None and claimed.job_id == job_id
    resumed.execute_search_unit(job_id, search_key)
    resumed.finalize_full_job(job_id)

    assert JobStateService(db).status(job_id) is JobStatus.SUCCEEDED
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations WHERE job_id=?", (job_id,)
    ).fetchone()[0] == 1
    cursor_rows = tuple(
        db.execute(
            "SELECT profile_id, source_kind, completed_upper_bound "
            "FROM youtube_source_cursors ORDER BY source_kind"
        )
    )
    assert len(cursor_rows) == 2
    assert {row["source_kind"] for row in cursor_rows} == {
        "seed_uploads",
        "cross_channel_search",
    }
    assert all(row["profile_id"] == profile.profile_id for row in cursor_rows)
    assert all(row["completed_upper_bound"] == _utc_text(NOW) for row in cursor_rows)


def test_recovery_recomputes_success_artifacts_and_fails_closed_on_domain_drift(
    db, db_path
):
    _, job_id, seed_key = _prepare_seed_success(db, with_observation=True)
    assert _run_crash_helper(db_path, job_id).returncode == 91
    observation = db.execute(
        "SELECT id FROM discovery_observations WHERE job_id=?", (job_id,)
    ).fetchone()
    db.execute("DROP TRIGGER discovery_observations_no_update")
    db.execute(
        "UPDATE discovery_observations SET observation_hash=? WHERE id=?",
        ("0" * 64, observation["id"]),
    )
    before = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT unit_key, status, output_hash, attempt_count "
            "FROM job_units WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        )
    )

    with pytest.raises(DomainError) as caught:
        YouTubeSyncService(db, clock=lambda: NOW).recover_interrupted_job(job_id)

    assert caught.value.code == "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID"
    assert JobStateService(db).unit(job_id, seed_key).status is UnitStatus.SUCCESS
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT unit_key, status, output_hash, attempt_count "
            "FROM job_units WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        )
    ) == before


def test_defer_is_one_transaction_and_reuses_the_same_job_when_due(db):
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    service = YouTubeSyncService(db, clock=lambda: NOW)
    request = service.request_manual_candidate(
        profile.subject_id, "https://youtu.be/defer000001", NOW
    )
    claimed = service.claim_next_runnable(NOW)
    assert claimed is not None and claimed.job_id == request.job_id
    unit_key = db.execute(
        "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
        (request.job_id,),
    ).fetchone()["unit_key"]
    due = NOW + timedelta(seconds=61)

    service.defer_current_unit(
        request.job_id,
        unit_key,
        "YOUTUBE_PROVIDER_DEFERRED",
        due,
    )

    assert JobStateService(db).status(request.job_id) is JobStatus.RETRYING
    assert JobStateService(db).unit(request.job_id, unit_key).status is UnitStatus.PENDING
    assert service.get_sync_manifest(request.job_id).resume_not_before_utc == due
    attempt = db.execute(
        "SELECT result_status, error_code FROM job_unit_attempts "
        "WHERE job_id=? AND unit_key=?",
        (request.job_id, unit_key),
    ).fetchone()
    assert tuple(attempt) == ("failed", "YOUTUBE_PROVIDER_DEFERRED")
    assert service.claim_next_runnable(due - timedelta(microseconds=1)) is None
    due_claim = service.claim_next_runnable(due)
    assert due_claim is not None and due_claim.job_id == request.job_id
