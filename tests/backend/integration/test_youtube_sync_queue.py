import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.discovery_profiles import (
    DiscoveryProfileService,
    ReplaceDiscoveryProfileVersion,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService


FIXED_NOW = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
LATER = FIXED_NOW + timedelta(minutes=10)
TOMORROW = FIXED_NOW + timedelta(days=1)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def db(db_path):
    conn = open_database(db_path)
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _bootstrap(db):
    bootstrap_reference_data(db)
    return DiscoveryRepository(db).list_active_profile_versions()


def _service(db, now: datetime = FIXED_NOW) -> YouTubeSyncService:
    return YouTubeSyncService(db, clock=lambda: now)


def _insert_manual_request(db, profile_id: int, suffix: str = "1") -> int:
    video_id = f"manual{suffix:0>5}"[-11:]
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO manual_discovery_requests("
            "profile_id, youtube_video_id, requested_at) VALUES (?, ?, ?)",
            (profile_id, video_id, _utc_text(FIXED_NOW)),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _unit_rows(db, job_id: int) -> tuple[dict[str, object], ...]:
    return tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        )
    )


def _cursor_rows(db) -> tuple[dict[str, object], ...]:
    return tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_source_cursors "
            "ORDER BY profile_id, source_kind, source_key"
        )
    )


def _seed_cursor_sentinel(db, profile_id: int) -> None:
    db.execute(
        "INSERT INTO youtube_source_cursors("
        "profile_id, source_kind, source_key, completed_upper_bound, "
        "cursor_hash, updated_at) VALUES (?, 'cross_channel_search', "
        "'sentinel-search-source', ?, ?, ?)",
        (profile_id, _utc_text(FIXED_NOW), "a" * 64, _utc_text(FIXED_NOW)),
    )


def _running_unit_key(db, job_id: int) -> str:
    rows = tuple(
        db.execute(
            "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
            (job_id,),
        )
    )
    assert len(rows) == 1
    return rows[0]["unit_key"]


def _claim_and_fail_first(db, job_id: int) -> str:
    claimed = _service(db).claim_next_runnable(FIXED_NOW)
    assert claimed is not None and claimed.job_id == job_id
    unit_key = _running_unit_key(db, job_id)
    JobStateService(db, clock=lambda: FIXED_NOW).fail_unit(
        job_id, unit_key, "SYNTHETIC_RETRYABLE"
    )
    assert JobStateService(db).status(job_id) is JobStatus.FAILED
    return unit_key


def _complete_first_and_fail_second(db, job_id: int) -> tuple[str, str, str]:
    service = _service(db)
    first_claim = service.claim_next_runnable(FIXED_NOW)
    assert first_claim is not None and first_claim.job_id == job_id
    first_key = _running_unit_key(db, job_id)
    checkpoint_hash = db.execute(
        "SELECT checkpoint_hash FROM youtube_sync_checkpoints "
        "WHERE job_id=? AND unit_key=?",
        (job_id, first_key),
    ).fetchone()["checkpoint_hash"]
    db.execute(
        "UPDATE youtube_sync_checkpoints SET completed_at=? "
        "WHERE job_id=? AND unit_key=?",
        (_utc_text(FIXED_NOW), job_id, first_key),
    )
    JobStateService(db, clock=lambda: FIXED_NOW).complete_unit(
        job_id, first_key, checkpoint_hash
    )

    second_claim = service.claim_next_runnable(FIXED_NOW)
    assert second_claim is not None and second_claim.job_id == job_id
    second_key = _running_unit_key(db, job_id)
    JobStateService(db, clock=lambda: FIXED_NOW).fail_unit(
        job_id, second_key, "SYNTHETIC_RETRYABLE"
    )
    assert JobStateService(db).status(job_id) is JobStatus.FAILED
    return first_key, second_key, checkpoint_hash


@pytest.mark.parametrize("prepared_status", ("queued", "running", "retrying"))
def test_compatible_full_request_reuses_existing_queue_or_active_job(
    db, prepared_status
):
    _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    if prepared_status == "running":
        claimed = _service(db).claim_next_runnable(FIXED_NOW)
        assert claimed is not None and claimed.job_id == first.job_id
    elif prepared_status == "retrying":
        _claim_and_fail_first(db, first.job_id)
        with transaction(db):
            JobStateService(
                db, clock=lambda: FIXED_NOW
            ).retry_failed_in_transaction(first.job_id, {})

    repeated = _service(db, LATER).request_full_sync(LATER)

    assert repeated.job_id == first.job_id
    assert repeated.reused is True
    assert repeated.status.value == prepared_status
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_manifests "
        "WHERE sync_kind='full_discovery'"
    ).fetchone()[0] == 1


def test_failed_compatible_request_retries_same_job_and_preserves_verified_success(
    db,
):
    _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    successful_key, failed_key, successful_hash = _complete_first_and_fail_second(
        db, first.job_id
    )

    repeated = _service(db, LATER).request_full_sync(LATER)

    assert repeated.job_id == first.job_id
    assert repeated.reused is True
    assert repeated.status is JobStatus.RETRYING
    assert JobStateService(db).unit(
        first.job_id, successful_key
    ).status is UnitStatus.SUCCESS
    assert JobStateService(db).unit(
        first.job_id, successful_key
    ).output_hash == successful_hash
    assert JobStateService(db).unit(
        first.job_id, failed_key
    ).status is UnitStatus.PENDING
    assert db.execute(
        "SELECT COUNT(*) FROM jobs WHERE id=?", (first.job_id,)
    ).fetchone()[0] == 1
    retry_events = tuple(
        db.execute(
        "SELECT metadata_json FROM job_events WHERE job_id=? "
        "AND event_kind='job_status_changed'",
        (first.job_id,),
        )
    )
    assert sum(
        json.loads(row["metadata_json"])
        == {"from_status": "failed", "to_status": "retrying"}
        for row in retry_events
    ) == 1


def test_retry_failed_in_transaction_uses_only_checkpoint_verified_artifacts(db):
    _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    successful_key, failed_key, successful_hash = _complete_first_and_fail_second(
        db, first.job_id
    )
    artifacts = DiscoveryRepository(db).verified_youtube_artifact_hashes(
        first.job_id
    )

    assert artifacts == {successful_key: successful_hash}
    with transaction(db):
        plan = JobStateService(
            db, clock=lambda: LATER
        ).retry_failed_in_transaction(first.job_id, artifacts)

    assert plan.reused_unit_keys == (successful_key,)
    assert plan.pending_unit_keys[0] == failed_key
    assert plan.next_unit_key == failed_key
    assert JobStateService(db).status(first.job_id) is JobStatus.RETRYING


def test_failed_coalescing_rejects_checkpoint_artifact_tamper_without_retry(db):
    _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    successful_key, _, _ = _complete_first_and_fail_second(db, first.job_id)
    db.execute(
        "UPDATE youtube_sync_checkpoints SET checkpoint_hash=? "
        "WHERE job_id=? AND unit_key=?",
        ("f" * 64, first.job_id, successful_key),
    )
    before_units = _unit_rows(db, first.job_id)

    with pytest.raises(DomainError) as caught:
        _service(db, LATER).request_full_sync(LATER)

    assert caught.value.code == "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID"
    assert JobStateService(db).status(first.job_id) is JobStatus.FAILED
    assert _unit_rows(db, first.job_id) == before_units


def test_profile_change_queues_incompatible_full_job_behind_current_active_job(db):
    profiles = _bootstrap(db)
    original = _service(db).request_full_sync(FIXED_NOW)
    claimed = _service(db).claim_next_runnable(FIXED_NOW)
    assert claimed is not None and claimed.job_id == original.job_id
    target = profiles[0]
    changed = DiscoveryProfileService(
        db, clock=lambda: LATER
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=target.subject_id,
            seed_channel_ids=target.seed_channel_ids,
            search_terms=(target.search_terms[0], "synthetic changed search"),
            reason="verified synthetic discovery change",
        )
    )

    queued = _service(db, LATER).request_full_sync(LATER)

    assert changed.id != target.id
    assert queued.job_id != original.job_id
    assert queued.status is JobStatus.QUEUED
    assert queued.reused is False
    assert JobStateService(db).status(original.job_id) is JobStatus.RUNNING
    assert (
        DiscoveryRepository(db)
        .get_youtube_sync_manifest(queued.job_id)
        .profile_set_hash
        != DiscoveryRepository(db)
        .get_youtube_sync_manifest(original.job_id)
        .profile_set_hash
    )
    assert _service(db, LATER).claim_next_runnable(LATER) is None
    assert JobStateService(db).status(queued.job_id) is JobStatus.QUEUED


def test_oldest_runnable_job_is_claimed_fifo_and_only_one_pending_unit_starts(db):
    profiles = _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    manual_request_id = _insert_manual_request(db, profiles[0].profile_id)
    second = _service(db, LATER).request_manual_sync(manual_request_id, LATER)

    claimed = _service(db).claim_next_runnable(FIXED_NOW)

    assert claimed is not None
    assert claimed.job_id == first.job_id
    assert claimed.kind == "full"
    assert claimed.manifest.job_id == first.job_id
    assert JobStateService(db).status(first.job_id) is JobStatus.RUNNING
    assert JobStateService(db).status(second.job_id) is JobStatus.QUEUED
    first_units = _unit_rows(db, first.job_id)
    assert [row["status"] for row in first_units].count("running") == 1
    assert [row["status"] for row in first_units].count("pending") == len(
        first_units
    ) - 1
    assert first_units[0]["status"] == "running"
    assert _service(db).claim_next_runnable(FIXED_NOW) is None


def test_deferred_retrying_head_is_skipped_and_next_fifo_job_is_claimed(db):
    profiles = _bootstrap(db)
    first = _service(db).request_full_sync(FIXED_NOW)
    _claim_and_fail_first(db, first.job_id)
    with transaction(db):
        JobStateService(db, clock=lambda: FIXED_NOW).retry_failed_in_transaction(
            first.job_id, {}
        )
        DiscoveryRepository(db).set_youtube_resume_not_before(
            first.job_id, TOMORROW
        )
    request_id = _insert_manual_request(db, profiles[0].profile_id)
    second = _service(db, LATER).request_manual_sync(request_id, LATER)

    claimed = _service(db).claim_next_runnable(FIXED_NOW)

    assert claimed is not None and claimed.job_id == second.job_id
    assert claimed.kind == "manual"
    assert JobStateService(db).status(first.job_id) is JobStatus.RETRYING
    assert all(
        row["status"] == "pending" for row in _unit_rows(db, first.job_id)
    )


def test_database_partial_unique_index_rejects_second_active_youtube_job(db):
    profiles = _bootstrap(db)
    full = _service(db).request_full_sync(FIXED_NOW)
    request_id = _insert_manual_request(db, profiles[0].profile_id)
    manual = _service(db, LATER).request_manual_sync(request_id, LATER)
    db.execute(
        "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
        (_utc_text(FIXED_NOW), full.job_id),
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
            (_utc_text(LATER), manual.job_id),
        )

    assert JobStateService(db).status(full.job_id) is JobStatus.RUNNING
    assert JobStateService(db).status(manual.job_id) is JobStatus.QUEUED


def test_two_connection_barrier_race_coalesces_one_full_job(db_path):
    setup = open_database(db_path)
    from market_voice_forecast_ledger.db.migrate import apply_migrations

    apply_migrations(setup)
    bootstrap_reference_data(setup)
    setup.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def request() -> None:
        conn = open_database(db_path)
        try:
            barrier.wait(timeout=10)
            results.append(_service(conn).request_full_sync(FIXED_NOW))
        except BaseException as error:
            errors.append(error)
        finally:
            conn.close()

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert len({result.job_id for result in results}) == 1
    assert sorted(result.reused for result in results) == [False, True]
    verify = open_database(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert verify.execute(
            "SELECT COUNT(*) FROM youtube_sync_manifests"
        ).fetchone()[0] == 1
    finally:
        verify.close()


def test_two_connection_barrier_race_claims_at_most_one_active_unit(db_path):
    setup = open_database(db_path)
    from market_voice_forecast_ledger.db.migrate import apply_migrations

    apply_migrations(setup)
    bootstrap_reference_data(setup)
    job_id = _service(setup).request_full_sync(FIXED_NOW).job_id
    setup.close()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def claim() -> None:
        conn = open_database(db_path)
        try:
            barrier.wait(timeout=10)
            results.append(_service(conn).claim_next_runnable(FIXED_NOW))
        except BaseException as error:
            errors.append(error)
        finally:
            conn.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result is not None).job_id == job_id
    verify = open_database(db_path)
    try:
        assert verify.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_kind='youtube_sync' "
            "AND status IN ('running', 'pause_requested', 'cancel_requested')"
        ).fetchone()[0] == 1
        assert verify.execute(
            "SELECT COUNT(*) FROM job_units WHERE status='running'"
        ).fetchone()[0] == 1
    finally:
        verify.close()


def _tamper_total_units(db, job_id: int) -> None:
    db.execute("DROP TRIGGER jobs_manifest_immutable")
    db.execute("UPDATE jobs SET total_units=total_units+1 WHERE id=?", (job_id,))


def _tamper_unit_ordinal(db, job_id: int) -> None:
    db.execute("DROP TRIGGER job_units_manifest_immutable")
    db.execute(
        "UPDATE job_units SET ordinal=99 WHERE job_id=? AND ordinal=1",
        (job_id,),
    )


def _tamper_profile_version(db, job_id: int) -> None:
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER youtube_sync_manifest_profiles_no_update")
    db.execute(
        "UPDATE youtube_sync_manifest_profiles SET profile_version_id=999999 "
        "WHERE job_id=? AND ordinal=1",
        (job_id,),
    )
    db.execute("PRAGMA foreign_keys=ON")


def _tamper_config_hash(db, job_id: int) -> None:
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER youtube_sync_manifest_profiles_no_update")
    db.execute(
        "UPDATE youtube_sync_manifest_profiles SET config_hash=? "
        "WHERE job_id=? AND ordinal=1",
        ("0" * 64, job_id),
    )
    db.execute("PRAGMA foreign_keys=ON")


def _tamper_sync_kind(db, job_id: int) -> None:
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("DROP TRIGGER youtube_sync_manifests_limited_update")
    db.execute(
        "UPDATE youtube_sync_manifests SET sync_kind='manual' WHERE job_id=?",
        (job_id,),
    )
    db.execute("PRAGMA ignore_check_constraints=OFF")


def _tamper_upper_bound(db, job_id: int) -> None:
    db.execute("DROP TRIGGER youtube_sync_manifests_limited_update")
    db.execute(
        "UPDATE youtube_sync_manifests SET upper_bound=? WHERE job_id=?",
        (_utc_text(TOMORROW), job_id),
    )


def _tamper_backfill_floor(db, job_id: int) -> None:
    db.execute("DROP TRIGGER youtube_sync_manifests_limited_update")
    db.execute(
        "UPDATE youtube_sync_manifests SET backfill_floor=? WHERE job_id=?",
        (_utc_text(TOMORROW), job_id),
    )


def _tamper_manual_shape(db, job_id: int) -> None:
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("DROP TRIGGER youtube_sync_manifests_limited_update")
    db.execute(
        "UPDATE youtube_sync_manifests SET manual_request_id=NULL WHERE job_id=?",
        (job_id,),
    )
    db.execute("PRAGMA ignore_check_constraints=OFF")


def _tamper_checkpoint_hash(db, job_id: int) -> None:
    db.execute(
        "UPDATE youtube_sync_checkpoints SET checkpoint_hash=? "
        "WHERE job_id=? AND unit_key=(SELECT unit_key FROM job_units "
        "WHERE job_id=? ORDER BY ordinal LIMIT 1)",
        ("0" * 64, job_id, job_id),
    )


def _tamper_defer_timestamp(db, job_id: int) -> None:
    db.execute(
        "UPDATE youtube_sync_manifests SET resume_not_before_utc='not-a-time' "
        "WHERE job_id=?",
        (job_id,),
    )


@pytest.mark.parametrize(
    ("case", "tamper", "manual"),
    (
        ("total_units", _tamper_total_units, False),
        ("unit_ordinal", _tamper_unit_ordinal, False),
        ("profile_version", _tamper_profile_version, False),
        ("config_hash", _tamper_config_hash, False),
        ("sync_kind", _tamper_sync_kind, False),
        ("upper_bound", _tamper_upper_bound, False),
        ("backfill_floor", _tamper_backfill_floor, False),
        ("manual_shape", _tamper_manual_shape, True),
        ("checkpoint_hash", _tamper_checkpoint_hash, False),
        ("defer_timestamp", _tamper_defer_timestamp, False),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_stored_sync_corruption_fails_before_claim_or_cursor_mutation(
    db, case, tamper, manual
):
    profiles = _bootstrap(db)
    if manual:
        request_id = _insert_manual_request(db, profiles[0].profile_id)
        job_id = _service(db).request_manual_sync(request_id, FIXED_NOW).job_id
    else:
        job_id = _service(db).request_full_sync(FIXED_NOW).job_id
    _seed_cursor_sentinel(db, profiles[0].profile_id)
    tamper(db, job_id)
    before_job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    before_units = _unit_rows(db, job_id)
    before_cursors = _cursor_rows(db)

    with pytest.raises(DomainError):
        _service(db).claim_next_runnable(FIXED_NOW)

    assert dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()) == before_job
    assert _unit_rows(db, job_id) == before_units
    assert _cursor_rows(db) == before_cursors
    assert not db.in_transaction


def test_public_manifest_read_rejects_corrupt_compatible_job_before_new_write(db):
    profiles = _bootstrap(db)
    result = _service(db).request_full_sync(FIXED_NOW)
    _seed_cursor_sentinel(db, profiles[0].profile_id)
    _tamper_config_hash(db, result.job_id)
    before_jobs = tuple(dict(row) for row in db.execute("SELECT * FROM jobs"))
    before_cursors = _cursor_rows(db)

    with pytest.raises(DomainError):
        _service(db, LATER).get_sync_manifest(result.job_id)
    with pytest.raises(DomainError):
        _service(db, LATER).request_full_sync(LATER)

    assert tuple(dict(row) for row in db.execute("SELECT * FROM jobs")) == before_jobs
    assert _cursor_rows(db) == before_cursors
