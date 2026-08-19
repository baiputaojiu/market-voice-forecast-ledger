from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from tests.backend.youtube_fakes import FakeYouTubeClient, synthetic_video_item


REQUESTED_AT = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
VIDEO_ID = "abcdefghijk"
OLD_PUBLISHED_AT = "2021-08-19T03:04:05Z"
URL_SHORT = f"https://youtu.be/{VIDEO_ID}"
URL_WATCH = f"https://youtube.com/watch?v={VIDEO_ID}"
PRIVATE_SENTINEL = "synthetic-secret-never-store"
TITLE_SENTINEL = "Synthetic old manual candidate title"


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


def _profiles(db):
    return DiscoveryRepository(db).list_active_profile_versions()


def _service(db, *, client=None, failpoint=None):
    return YouTubeSyncService(
        db,
        clock=lambda: REQUESTED_AT,
        youtube_client=client,
        failpoint=failpoint,
    )


def _counts(db):
    names = (
        "manual_discovery_requests",
        "jobs",
        "job_units",
        "job_events",
        "youtube_sync_manifests",
        "youtube_sync_manifest_profiles",
        "youtube_sync_checkpoints",
        "videos",
        "video_metadata_snapshots",
        "discovery_observations",
        "subject_video_candidates",
        "presence_decisions",
        "youtube_source_cursors",
        "youtube_sync_proposed_cursors",
    )
    return {
        name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in names
    }


def _manual_rows(db, request_id: int, job_id: int):
    request = db.execute(
        "SELECT * FROM manual_discovery_requests WHERE id=?", (request_id,)
    ).fetchone()
    manifest = db.execute(
        "SELECT * FROM youtube_sync_manifests WHERE job_id=?", (job_id,)
    ).fetchone()
    unit = db.execute(
        "SELECT * FROM job_units WHERE job_id=?", (job_id,)
    ).fetchone()
    checkpoint = db.execute(
        "SELECT * FROM youtube_sync_checkpoints WHERE job_id=?", (job_id,)
    ).fetchone()
    return request, manifest, unit, checkpoint


def _claim_manual(db, service: YouTubeSyncService, job_id: int) -> str:
    claimed = service.claim_next_runnable(REQUESTED_AT)
    assert claimed is not None
    assert claimed.job_id == job_id
    assert claimed.kind == "manual"
    row = db.execute(
        "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
        (job_id,),
    ).fetchone()
    assert row is not None
    return row["unit_key"]


def _available_client() -> FakeYouTubeClient:
    return FakeYouTubeClient(
        video_responses=((synthetic_video_item(
            video_id=VIDEO_ID,
            title=TITLE_SENTINEL,
            snippet_published_at=OLD_PUBLISHED_AT,
        ),),)
    )


def _safe_manual_storage_text(db, request_id: int, job_id: int) -> str:
    request, manifest, unit, checkpoint = _manual_rows(db, request_id, job_id)
    events = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT event_kind, metadata_json FROM job_events WHERE job_id=?",
            (job_id,),
        )
    )
    values = (
        tuple(request),
        tuple(manifest),
        tuple(unit),
        tuple(checkpoint),
        events,
    )
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


def test_equivalent_urls_reuse_one_durable_request_and_exact_linked_job(db):
    profile = _profiles(db)[0]
    service = _service(db)

    first = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    second = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )
    request, manifest, unit, checkpoint = _manual_rows(
        db, first.request_id, first.job_id
    )

    assert type(first).__name__ == "ManualRequestResult"
    assert first.status is JobStatus.QUEUED
    assert first.reused is False
    assert second == type(first)(
        first.request_id, first.job_id, JobStatus.QUEUED, True
    )
    assert dict(request) == {
        "id": first.request_id,
        "profile_id": profile.profile_id,
        "youtube_video_id": VIDEO_ID,
        "requested_at": "2026-08-19T03:04:05.000000Z",
    }
    assert manifest["manual_request_id"] == first.request_id
    assert manifest["sync_kind"] == "manual"
    assert unit["unit_key"] == f"youtube:manual-request:{first.request_id}"
    assert checkpoint["source_key"] == f"manual-request:{first.request_id}"
    assert db.execute(
        "SELECT COUNT(*) FROM manual_discovery_requests"
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    stored = _safe_manual_storage_text(db, first.request_id, first.job_id)
    assert URL_SHORT not in stored
    assert URL_WATCH not in stored
    assert PRIVATE_SENTINEL not in stored
    assert TITLE_SENTINEL not in stored


def test_same_video_for_different_profiles_creates_distinct_request_and_job(db):
    first_profile, second_profile = _profiles(db)[:2]
    service = _service(db)

    first = service.request_manual_candidate(
        first_profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    second = service.request_manual_candidate(
        second_profile.subject_id, URL_WATCH, REQUESTED_AT
    )

    assert first.request_id != second.request_id
    assert first.job_id != second.job_id
    assert db.execute(
        "SELECT COUNT(DISTINCT profile_id) FROM manual_discovery_requests "
        "WHERE youtube_video_id=?",
        (VIDEO_ID,),
    ).fetchone()[0] == 2


@pytest.mark.parametrize("inactive_kind", ("subject", "profile"))
def test_inactive_subject_or_profile_is_rejected_without_any_write(db, inactive_kind):
    profile = _profiles(db)[0]
    table = "analysis_subjects" if inactive_kind == "subject" else "discovery_profiles"
    identity = profile.subject_id if inactive_kind == "subject" else profile.profile_id
    db.execute(f"UPDATE {table} SET is_active=0 WHERE id=?", (identity,))
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _service(db).request_manual_candidate(
            profile.subject_id, URL_SHORT, REQUESTED_AT
        )

    assert caught.value.code == "DISCOVERY_PROFILE_NOT_ACTIVE"
    assert _counts(db) == before


def test_missing_subject_is_rejected_without_any_write(db):
    before = _counts(db)

    with pytest.raises(LookupError):
        _service(db).request_manual_candidate(999_999, URL_SHORT, REQUESTED_AT)

    assert _counts(db) == before


@pytest.mark.parametrize(
    "bad_url",
    (
        "https://youtube.com.evil.invalid/watch?v=abcdefghijk",
        "https://youtube.com/watch?v=abcdefghijk&feature=tracking",
        "https://youtu.be/abcdefghijk?tracking=1",
        "not a URL",
    ),
)
def test_malformed_url_is_parsed_before_any_database_write(db, bad_url):
    profile = _profiles(db)[0]
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _service(db).request_manual_candidate(
            profile.subject_id, bad_url, REQUESTED_AT
        )

    assert caught.value.code == "INVALID_YOUTUBE_URL"
    assert _counts(db) == before


def test_request_insert_failure_rolls_back_the_partial_request(db, monkeypatch):
    profile = _profiles(db)[0]
    before = _counts(db)

    class SyntheticRequestInsertFailure(RuntimeError):
        pass

    def fail_after_insert(repository, *, profile_id, youtube_video_id, requested_at):
        assert repository._conn.in_transaction
        repository._conn.execute(
            "INSERT INTO manual_discovery_requests("
            "profile_id, youtube_video_id, requested_at) VALUES (?, ?, ?)",
            (
                profile_id,
                youtube_video_id,
                requested_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        raise SyntheticRequestInsertFailure("synthetic request insert failure")

    monkeypatch.setattr(
        DiscoveryRepository,
        "create_manual_discovery_request",
        fail_after_insert,
    )

    with pytest.raises(SyntheticRequestInsertFailure):
        _service(db).request_manual_candidate(
            profile.subject_id, URL_SHORT, REQUESTED_AT
        )

    assert _counts(db) == before


def test_manifest_seal_failure_rolls_back_request_job_link_and_children(
    db, monkeypatch
):
    profile = _profiles(db)[0]
    before = _counts(db)
    original = DiscoveryRepository.create_youtube_sync_manifest

    class SyntheticSealFailure(RuntimeError):
        pass

    def fail_after_seal(repository, **kwargs):
        value = original(repository, **kwargs)
        assert repository._conn.in_transaction
        assert repository._conn.execute(
            "SELECT COUNT(*) FROM youtube_sync_manifests WHERE job_id=?",
            (kwargs["job_id"],),
        ).fetchone()[0] == 1
        raise SyntheticSealFailure("synthetic link/seal failure")

    monkeypatch.setattr(
        DiscoveryRepository, "create_youtube_sync_manifest", fail_after_seal
    )

    with pytest.raises(SyntheticSealFailure):
        _service(db).request_manual_candidate(
            profile.subject_id, URL_SHORT, REQUESTED_AT
        )

    assert _counts(db) == before


def test_two_connection_race_converges_on_one_request_and_one_job(db, db_path):
    subject_id = _profiles(db)[0].subject_id
    barrier = threading.Barrier(2)
    results: list[object] = []
    failures: list[BaseException] = []

    def request(url: str) -> None:
        conn = open_database(db_path)
        try:
            barrier.wait(timeout=5)
            results.append(
                _service(conn).request_manual_candidate(
                    subject_id, url, REQUESTED_AT
                )
            )
        except BaseException as cause:
            failures.append(cause)
        finally:
            conn.close()

    threads = (
        threading.Thread(target=request, args=(URL_SHORT,)),
        threading.Thread(target=request, args=(URL_WATCH,)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not failures
    assert len(results) == 2
    assert {result.request_id for result in results} == {results[0].request_id}
    assert {result.job_id for result in results} == {results[0].job_id}
    assert sorted(result.reused for result in results) == [False, True]
    assert db.execute(
        "SELECT COUNT(*) FROM manual_discovery_requests"
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_queued_running_and_retrying_requests_reuse_the_exact_linked_job(db):
    profile = _profiles(db)[0]
    service = _service(db)
    queued = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    queued_again = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, queued.job_id)
    running = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    JobStateService(db, clock=lambda: REQUESTED_AT).fail_unit(
        queued.job_id, unit_key, "SYNTHETIC_MANUAL_FAILURE"
    )
    retrying = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    retrying_again = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )

    assert queued_again == type(queued)(
        queued.request_id, queued.job_id, JobStatus.QUEUED, True
    )
    assert running == type(queued)(
        queued.request_id, queued.job_id, JobStatus.RUNNING, True
    )
    assert retrying == type(queued)(
        queued.request_id, queued.job_id, JobStatus.RETRYING, True
    )
    assert retrying_again == retrying
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert db.execute(
        "SELECT status FROM job_units WHERE job_id=?", (queued.job_id,)
    ).fetchone()[0] == UnitStatus.PENDING.value


def test_stopped_request_is_reused_without_restart(db):
    profile = _profiles(db)[0]
    service = _service(db)
    first = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    assert JobStateService(db, clock=lambda: REQUESTED_AT).request_stop(
        first.job_id
    ) is JobStatus.STOPPED

    repeated = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )

    assert repeated == type(first)(
        first.request_id, first.job_id, JobStatus.STOPPED, True
    )
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_five_year_old_manual_video_uses_one_videos_call_and_common_persistence(db):
    profile = _profiles(db)[0]
    client = _available_client()
    service = _service(db, client=client)
    request = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, request.job_id)
    cursor_before = tuple(
        tuple(row)
        for row in db.execute("SELECT * FROM youtube_source_cursors ORDER BY 1,2,3")
    )

    result = service.execute_manual_unit(request.job_id, unit_key)

    assert result.discovered_count == 1
    assert result.persisted_count == 1
    assert result.unavailable_count == 0
    assert client.video_calls == [(VIDEO_ID,)]
    assert db.execute(
        "SELECT published_at FROM video_metadata_snapshots"
    ).fetchone()[0] == "2021-08-19T03:04:05.000000Z"
    observation = db.execute("SELECT * FROM discovery_observations").fetchone()
    assert observation["source_kind"] == "manual_url"
    assert observation["source_key"] == f"manual-request:{request.request_id}"
    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_candidates"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions "
        "WHERE state='presence_unverified'"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT status FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0] == JobStatus.SUCCEEDED.value
    assert tuple(
        tuple(row)
        for row in db.execute("SELECT * FROM youtube_source_cursors ORDER BY 1,2,3")
    ) == cursor_before
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors"
    ).fetchone()[0] == 0

    replay = service.execute_manual_unit(request.job_id, unit_key)
    repeated = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )
    assert replay == result
    assert repeated == type(request)(
        request.request_id, request.job_id, JobStatus.SUCCEEDED, True
    )
    assert client.video_calls == [(VIDEO_ID,)]
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "video_response",
    (
        (),
        (synthetic_video_item(video_id=VIDEO_ID, privacy_status="private"),),
        (synthetic_video_item(video_id=VIDEO_ID, upload_status="deleted"),),
    ),
)
def test_unavailable_private_or_deleted_manual_video_succeeds_without_candidate(
    db, video_response
):
    profile = _profiles(db)[0]
    client = FakeYouTubeClient(video_responses=(video_response,))
    service = _service(db, client=client)
    request = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, request.job_id)

    result = service.execute_manual_unit(request.job_id, unit_key)

    assert result.discovered_count == 1
    assert result.persisted_count == 0
    assert result.unavailable_count == 1
    assert client.video_calls == [(VIDEO_ID,)]
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_metadata_snapshots"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_candidates"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT status FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0] == JobStatus.SUCCEEDED.value
    stored = _safe_manual_storage_text(db, request.request_id, request.job_id)
    assert URL_SHORT not in stored
    assert TITLE_SENTINEL not in stored
    assert PRIVATE_SENTINEL not in stored
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors"
    ).fetchone()[0] == 0


def test_manual_execution_never_reads_or_writes_full_discovery_cursor_tables(db):
    profile = _profiles(db)[0]
    client = _available_client()
    service = _service(db, client=client)
    request = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, request.job_id)
    forbidden_tables = {
        "youtube_source_cursors",
        "youtube_sync_proposed_cursors",
        "youtube_search_windows",
    }

    def authorizer(action, arg1, _arg2, _db_name, _trigger_name):
        if action in {sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and arg1 in forbidden_tables:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorizer)
    try:
        result = service.execute_manual_unit(request.job_id, unit_key)
    finally:
        db.set_authorizer(None)

    assert result == service.execute_manual_unit(request.job_id, unit_key)


def test_failed_reregistration_verifies_completed_artifact_and_retries_same_job(db):
    profile = _profiles(db)[0]
    service = _service(db, client=_available_client())
    request = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, request.job_id)
    completed = service.execute_manual_unit(request.job_id, unit_key)
    db.execute(
        "UPDATE jobs SET status='failed' WHERE id=?", (request.job_id,)
    )

    retried = service.request_manual_candidate(
        profile.subject_id, URL_WATCH, REQUESTED_AT
    )

    assert retried == type(request)(
        request.request_id, request.job_id, JobStatus.RETRYING, True
    )
    unit = JobStateService(db).unit(request.job_id, unit_key)
    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == completed.output_hash


def test_failed_reregistration_fails_closed_on_completed_artifact_corruption(db):
    profile = _profiles(db)[0]
    service = _service(db, client=_available_client())
    request = service.request_manual_candidate(
        profile.subject_id, URL_SHORT, REQUESTED_AT
    )
    unit_key = _claim_manual(db, service, request.job_id)
    service.execute_manual_unit(request.job_id, unit_key)
    db.execute("DROP TRIGGER discovery_observations_no_update")
    db.execute(
        "UPDATE discovery_observations SET source_key='manual-request:999999' "
        "WHERE job_id=?",
        (request.job_id,),
    )
    db.execute(
        "UPDATE jobs SET status='failed' WHERE id=?", (request.job_id,)
    )
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        service.request_manual_candidate(
            profile.subject_id, URL_WATCH, REQUESTED_AT
        )

    assert caught.value.code == "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID"
    assert _counts(db) == before
    assert db.execute(
        "SELECT status FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0] == JobStatus.FAILED.value
