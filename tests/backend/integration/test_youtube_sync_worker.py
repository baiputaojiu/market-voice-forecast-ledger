from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.workers.scheduled_sync import (
    WorkerDependencies,
    WorkerSummary,
    run_once,
)
from market_voice_forecast_ledger.windows.task_scheduler import ScheduledTaskStatus
from market_voice_forecast_ledger.youtube.client import SafeTransportFailure
from tests.backend.youtube_fakes import (
    EndpointYouTubeTransport,
    FakeCredentialStore,
    FakeScheduleReader,
)


NOW = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
VIDEO_ONE = "worker00001"
VIDEO_TWO = "worker00002"
VIDEO_THREE = "worker00003"
HELPER = Path(__file__).with_name("crash_youtube_sync_worker.py")


@pytest.fixture
def settings(tmp_path):
    return Settings.for_data_dir(tmp_path / "data")


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _open_ready(settings: Settings):
    conn = open_database(settings.database_path)
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    return conn


def _first_profile(conn):
    return DiscoveryRepository(conn).list_active_profile_versions()[0]


def _manual_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def _create_manual(conn, video_id: str, now: datetime = NOW):
    profile = _first_profile(conn)
    return YouTubeSyncService(conn, clock=lambda: now).request_manual_candidate(
        profile.subject_id, _manual_url(video_id), now
    )


def _dependencies(
    *,
    now: datetime = NOW,
    transport=None,
    credential_store=None,
    schedule=None,
    schedule_error: BaseException | None = None,
    delays: list[float] | None = None,
):
    status = ScheduledTaskStatus.unavailable() if schedule is None else schedule
    recorded_delays = [] if delays is None else delays
    return WorkerDependencies(
        credential_store=credential_store or FakeCredentialStore(),
        transport=transport or EndpointYouTubeTransport(),
        schedule_reader=FakeScheduleReader(status, error=schedule_error),
        clock=lambda: now,
        sleeper=lambda seconds: recorded_delays.append(seconds),
    )


def _job_state(settings: Settings, job_id: int):
    conn = open_database(settings.database_path)
    try:
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        unit = conn.execute(
            "SELECT status, error_code, attempt_count FROM job_units WHERE job_id=?",
            (job_id,),
        ).fetchone()
        attempts = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT attempt_no, result_status, error_code FROM job_unit_attempts "
                "WHERE job_id=? ORDER BY attempt_no",
                (job_id,),
            )
        )
        reservations = tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT request_ordinal, attempt_no, endpoint_class, attempted_at "
                "FROM youtube_quota_reservations WHERE job_id=? "
                "ORDER BY request_ordinal, attempt_no",
                (job_id,),
            )
        )
        manifest = conn.execute(
            "SELECT resume_not_before_utc FROM youtube_sync_manifests WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return job["status"], dict(unit), attempts, reservations, manifest[0]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("credential_store", "expected_code"),
    (
        (
            FakeCredentialStore(
                read_error=DomainError(
                    "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
                    "private credential detail",
                )
            ),
            "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
        ),
        (FakeCredentialStore(secret="invalid whitespace secret"), "YOUTUBE_CREDENTIAL_INVALID"),
    ),
)
def test_missing_or_corrupt_credential_fails_safely_without_quota_attempt(
    settings, credential_store, expected_code
):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()

    summary = run_once(
        settings,
        _dependencies(credential_store=credential_store),
    )

    status, unit, attempts, reservations, resume_at = _job_state(
        settings, request.job_id
    )
    assert summary == WorkerSummary(1, 0, 0, 1)
    assert status == "failed"
    assert unit == {
        "status": "failed",
        "error_code": expected_code,
        "attempt_count": 1,
    }
    assert attempts == ((1, "failed", expected_code),)
    assert reservations == ()
    assert resume_at is None


def test_credential_repair_resumes_the_same_job_on_the_next_wake(settings):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()
    broken = FakeCredentialStore(read_error=DomainError("PRIVATE", "private"))

    first = run_once(settings, _dependencies(credential_store=broken))
    second = run_once(settings, _dependencies())

    status, unit, attempts, reservations, _ = _job_state(settings, request.job_id)
    conn = open_database(settings.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()
    assert first == WorkerSummary(1, 0, 0, 1)
    assert second == WorkerSummary(1, 1, 0, 0)
    assert status == "succeeded"
    assert unit["attempt_count"] == 2
    assert attempts[0] == (1, "failed", "YOUTUBE_CREDENTIAL_STORAGE_FAILED")
    assert attempts[1][:2] == (2, "success")
    assert reservations == ((1, 1, "videos_list", _utc_text(NOW)),)


def test_network_exhaustion_records_four_reservations_and_one_failed_unit_attempt(
    settings
):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()
    delays: list[float] = []
    transport = EndpointYouTubeTransport(
        responses=tuple(SafeTransportFailure(kind="network") for _ in range(4))
    )

    summary = run_once(
        settings,
        _dependencies(transport=transport, delays=delays),
    )

    status, unit, attempts, reservations, resume_at = _job_state(
        settings, request.job_id
    )
    assert summary == WorkerSummary(1, 0, 0, 1)
    assert status == "failed"
    assert unit["error_code"] == "YOUTUBE_PROVIDER_TRANSIENT"
    assert attempts == ((1, "failed", "YOUTUBE_PROVIDER_TRANSIENT"),)
    assert tuple(row[:3] for row in reservations) == (
        (1, 1, "videos_list"),
        (1, 2, "videos_list"),
        (1, 3, "videos_list"),
        (1, 4, "videos_list"),
    )
    assert all(row[3] == _utc_text(NOW) for row in reservations)
    assert delays == [1, 4, 16]
    assert resume_at is None


@pytest.mark.parametrize(
    "failure",
    (
        SafeTransportFailure(kind="http", status_code=429),
        SafeTransportFailure(kind="http", status_code=503),
    ),
)
def test_nonquota_429_and_5xx_retry_then_complete_in_the_same_attempt(
    settings, failure
):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()
    delays: list[float] = []
    transport = EndpointYouTubeTransport(
        responses=(
            failure,
            {
                "items": [],
                "nextPageToken": None,
            },
        )
    )

    summary = run_once(
        settings,
        _dependencies(transport=transport, delays=delays),
    )

    status, unit, attempts, reservations, resume_at = _job_state(
        settings, request.job_id
    )
    assert summary == WorkerSummary(1, 1, 0, 0)
    assert status == "succeeded"
    assert unit["attempt_count"] == 1
    assert attempts[0][:2] == (1, "success")
    assert tuple(row[:3] for row in reservations) == (
        (1, 1, "videos_list"),
        (1, 2, "videos_list"),
    )
    assert delays == [1]
    assert resume_at is None


@pytest.mark.parametrize(
    ("failure", "expected_code", "delay"),
    (
        (
            SafeTransportFailure(
                kind="http", status_code=403, provider_signal="quota"
            ),
            "YOUTUBE_QUOTA_EXHAUSTED",
            timedelta(hours=24),
        ),
        (
            SafeTransportFailure(
                kind="http", status_code=429, retry_after_seconds=61
            ),
            "YOUTUBE_PROVIDER_DEFERRED",
            timedelta(seconds=61),
        ),
    ),
)
def test_provider_quota_and_long_retry_after_defer_without_busy_retry(
    settings, failure, expected_code, delay
):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()
    delays: list[float] = []

    summary = run_once(
        settings,
        _dependencies(
            transport=EndpointYouTubeTransport(responses=(failure,)),
            delays=delays,
        ),
    )

    status, unit, attempts, reservations, resume_at = _job_state(
        settings, request.job_id
    )
    assert summary == WorkerSummary(1, 0, 1, 0)
    assert status == "retrying"
    assert unit["status"] == "pending"
    assert unit["error_code"] is None
    assert attempts == ((1, "failed", expected_code),)
    assert tuple(row[:3] for row in reservations) == ((1, 1, "videos_list"),)
    assert resume_at == _utc_text(NOW + delay)
    assert delays == []

    before = run_once(
        settings,
        _dependencies(now=NOW + delay - timedelta(microseconds=1)),
    )
    due = run_once(settings, _dependencies(now=NOW + delay))

    assert before == WorkerSummary(0, 0, 0, 0)
    assert due == WorkerSummary(1, 1, 0, 0)
    assert _job_state(settings, request.job_id)[0] == "succeeded"


def test_one_wake_does_not_repeat_a_new_failure_or_create_a_failure_chain(settings):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_ONE)
    conn.close()
    broken = FakeCredentialStore(read_error=DomainError("PRIVATE", "private"))

    first = run_once(settings, _dependencies(credential_store=broken))
    second = run_once(settings, _dependencies(credential_store=broken))

    assert first == WorkerSummary(1, 0, 0, 1)
    assert second == WorkerSummary(1, 0, 0, 1)
    status, unit, attempts, _, _ = _job_state(settings, request.job_id)
    conn = open_database(settings.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()
    assert status == "failed"
    assert unit["attempt_count"] == 2
    assert attempts == (
        (1, "failed", "YOUTUBE_CREDENTIAL_STORAGE_FAILED"),
        (2, "failed", "YOUTUBE_CREDENTIAL_STORAGE_FAILED"),
    )


def test_worker_drains_full_and_manual_jobs_in_fifo_including_work_queued_mid_run(
    settings
):
    conn = _open_ready(settings)
    service = YouTubeSyncService(conn, clock=lambda: NOW)
    full = service.request_full_sync(NOW)
    manual_one = _create_manual(conn, VIDEO_ONE)
    conn.close()
    created_mid_run: list[int] = []

    def enqueue_on_first_request(_endpoint, _params, ordinal):
        if ordinal != 1:
            return
        other = open_database(settings.database_path)
        try:
            created_mid_run.append(_create_manual(other, VIDEO_TWO).job_id)
        finally:
            other.close()

    summary = run_once(
        settings,
        _dependencies(transport=EndpointYouTubeTransport(on_request=enqueue_on_first_request)),
    )

    assert summary == WorkerSummary(3, 3, 0, 0)
    assert len(created_mid_run) == 1
    manual_two = created_mid_run[0]
    conn = open_database(settings.database_path)
    try:
        assert tuple(
            row["status"] for row in conn.execute("SELECT status FROM jobs ORDER BY id")
        ) == ("succeeded", "succeeded", "succeeded")
        starts = tuple(
            row["job_id"]
            for row in conn.execute(
                "SELECT job_id FROM job_events WHERE event_kind='unit_started' ORDER BY id"
            )
        )
        first_seen = tuple(dict.fromkeys(starts))
        assert first_seen == (full.job_id, manual_one.job_id, manual_two)
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN "
            "('running','pause_requested','cancel_requested')"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_worker_stops_at_a_deferred_fifo_head_without_claiming_later_work(settings):
    conn = _open_ready(settings)
    first = _create_manual(conn, VIDEO_ONE)
    second = _create_manual(conn, VIDEO_TWO)
    conn.close()
    transport = EndpointYouTubeTransport(
        responses=(
            SafeTransportFailure(
                kind="http", status_code=429, retry_after_seconds=61
            ),
        )
    )

    summary = run_once(settings, _dependencies(transport=transport))
    early = run_once(settings, _dependencies(now=NOW + timedelta(seconds=30)))

    assert summary == WorkerSummary(1, 0, 1, 0)
    assert early == WorkerSummary(0, 0, 0, 0)
    assert _job_state(settings, first.job_id)[0] == "retrying"
    assert _job_state(settings, second.job_id)[0] == "queued"


@pytest.mark.parametrize(
    ("status", "now", "existing", "expected_daily"),
    (
        (ScheduledTaskStatus.unavailable(), NOW, False, 0),
        (ScheduledTaskStatus(True, "06:00", True, "Queue"), datetime(2026, 8, 18, 20, 59, tzinfo=timezone.utc), False, 0),
        (ScheduledTaskStatus(True, "06:00", True, "Queue"), datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc), False, 1),
        (ScheduledTaskStatus(True, "06:00", True, "Queue"), NOW, False, 1),
        (ScheduledTaskStatus(True, "06:00", True, "Queue"), NOW, True, 1),
    ),
)
def test_daily_schedule_matrix_uses_task_scheduler_time_and_jst_day(
    settings, status, now, existing, expected_daily
):
    conn = _open_ready(settings)
    service = YouTubeSyncService(conn, clock=lambda: now)
    if existing:
        service.ensure_daily_full_request(status.jst_day(now))
    conn.close()

    run_once(settings, _dependencies(now=now, schedule=status))
    run_once(settings, _dependencies(now=now, schedule=status))

    conn = open_database(settings.database_path)
    try:
        rows = tuple(conn.execute("SELECT * FROM youtube_daily_sync_requests"))
        assert len(rows) == expected_daily
        if expected_daily:
            assert rows[0]["jst_day"] == status.jst_day(now).isoformat()
        assert conn.execute(
            "SELECT COUNT(*) FROM youtube_daily_sync_requests"
        ).fetchone()[0] == expected_daily
    finally:
        conn.close()


def test_cross_day_daily_request_creates_a_distinct_job_and_drains_both_fifo(
    settings,
):
    prior_now = NOW - timedelta(hours=13)
    prior_day = date(2026, 8, 18)
    today = date(2026, 8, 19)
    schedule = ScheduledTaskStatus(True, "06:00", True, "Queue")
    conn = _open_ready(settings)
    service = YouTubeSyncService(conn, clock=lambda: prior_now)
    prior = service.ensure_daily_full_request(prior_day)
    prior_manifest = service.get_sync_manifest(prior.job_id)
    conn.close()

    summary = run_once(
        settings,
        _dependencies(now=NOW, schedule=schedule),
    )

    assert summary == WorkerSummary(2, 2, 0, 0)
    conn = open_database(settings.database_path)
    try:
        daily = tuple(
            conn.execute(
                "SELECT jst_day, job_id, requested_at "
                "FROM youtube_daily_sync_requests ORDER BY jst_day"
            )
        )
        assert tuple(row["jst_day"] for row in daily) == (
            prior_day.isoformat(),
            today.isoformat(),
        )
        assert daily[0]["job_id"] == prior.job_id
        assert daily[1]["job_id"] != prior.job_id
        assert daily[0]["requested_at"] == _utc_text(prior_now)
        assert daily[1]["requested_at"] == _utc_text(NOW)
        current_job_id = daily[1]["job_id"]
        current_manifest = YouTubeSyncService(
            conn, clock=lambda: NOW
        ).get_sync_manifest(current_job_id)
        assert prior_manifest.upper_bound == prior_now
        assert current_manifest.upper_bound == NOW
        assert current_manifest.profile_set_hash == prior_manifest.profile_set_hash
        assert current_manifest.profiles == prior_manifest.profiles
        assert tuple(
            row["status"]
            for row in conn.execute("SELECT status FROM jobs ORDER BY id")
        ) == ("succeeded", "succeeded")
        starts = tuple(
            row["job_id"]
            for row in conn.execute(
                "SELECT job_id FROM job_events "
                "WHERE event_kind='unit_started' ORDER BY id"
            )
        )
        assert tuple(dict.fromkeys(starts)) == (prior.job_id, current_job_id)
    finally:
        conn.close()


def test_cross_day_daily_request_is_atomic_across_connections_and_repeated_wakes(
    settings,
):
    prior_now = NOW - timedelta(hours=13)
    prior_day = date(2026, 8, 18)
    today = date(2026, 8, 19)
    conn = _open_ready(settings)
    prior = YouTubeSyncService(
        conn, clock=lambda: prior_now
    ).ensure_daily_full_request(prior_day)
    conn.close()
    barrier = threading.Barrier(2)
    results: list[int] = []
    failures: list[BaseException] = []

    def request_today() -> None:
        other = open_database(settings.database_path)
        try:
            barrier.wait(timeout=5)
            result = YouTubeSyncService(
                other, clock=lambda: NOW
            ).ensure_daily_full_request(today)
            results.append(result.job_id)
        except BaseException as cause:
            failures.append(cause)
        finally:
            other.close()

    threads = tuple(threading.Thread(target=request_today) for _ in range(2))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    conn = open_database(settings.database_path)
    try:
        repeated = YouTubeSyncService(
            conn, clock=lambda: NOW
        ).ensure_daily_full_request(today)
        rows = tuple(
            conn.execute(
                "SELECT jst_day, job_id FROM youtube_daily_sync_requests "
                "ORDER BY jst_day"
            )
        )
        assert failures == []
        assert len(results) == 2
        assert results[0] == results[1] == repeated.job_id
        assert repeated.job_id != prior.job_id
        assert tuple((row["jst_day"], row["job_id"]) for row in rows) == (
            (prior_day.isoformat(), prior.job_id),
            (today.isoformat(), repeated.job_id),
        )
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        conn.close()


def test_cross_day_daily_request_stays_queued_behind_a_deferred_prior_day_head(
    settings,
):
    prior_now = NOW - timedelta(hours=13)
    prior_day = date(2026, 8, 18)
    today = date(2026, 8, 19)
    schedule = ScheduledTaskStatus(True, "06:00", True, "Queue")
    conn = _open_ready(settings)
    prior = YouTubeSyncService(
        conn, clock=lambda: prior_now
    ).ensure_daily_full_request(prior_day)
    conn.close()
    quota = SafeTransportFailure(
        kind="http", status_code=403, provider_signal="quota"
    )
    deferred = run_once(
        settings,
        _dependencies(
            now=prior_now,
            transport=EndpointYouTubeTransport(responses=(quota,)),
        ),
    )

    current = run_once(
        settings,
        _dependencies(now=NOW, schedule=schedule),
    )

    assert deferred == WorkerSummary(1, 0, 1, 0)
    assert current == WorkerSummary(0, 0, 0, 0)
    conn = open_database(settings.database_path)
    try:
        rows = tuple(
            conn.execute(
                "SELECT daily.jst_day, daily.job_id, jobs.status "
                "FROM youtube_daily_sync_requests AS daily "
                "JOIN jobs ON jobs.id=daily.job_id ORDER BY daily.jst_day"
            )
        )
        assert tuple(row["jst_day"] for row in rows) == (
            prior_day.isoformat(),
            today.isoformat(),
        )
        assert rows[0]["job_id"] == prior.job_id
        assert rows[0]["status"] == "retrying"
        assert rows[1]["job_id"] != prior.job_id
        assert rows[1]["status"] == "queued"
        current_manifest = YouTubeSyncService(
            conn, clock=lambda: NOW
        ).get_sync_manifest(rows[1]["job_id"])
        assert current_manifest.upper_bound == NOW
    finally:
        conn.close()


def test_schedule_status_failure_suppresses_daily_creation_but_drains_manual_queue(
    settings
):
    conn = _open_ready(settings)
    manual = _create_manual(conn, VIDEO_ONE)
    conn.close()

    summary = run_once(
        settings,
        _dependencies(
            schedule_error=DomainError(
                "YOUTUBE_SCHEDULE_STATUS_UNAVAILABLE", "private native detail"
            )
        ),
    )

    assert summary == WorkerSummary(1, 1, 0, 0)
    assert _job_state(settings, manual.job_id)[0] == "succeeded"
    conn = open_database(settings.database_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM youtube_daily_sync_requests"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_two_worker_processes_never_claim_the_same_active_database_job(settings, tmp_path):
    conn = _open_ready(settings)
    request = _create_manual(conn, VIDEO_THREE)
    conn.close()
    ready_one = tmp_path / "ready-one"
    ready_two = tmp_path / "ready-two"
    release = tmp_path / "release"
    summary_one = tmp_path / "summary-one.json"
    summary_two = tmp_path / "summary-two.json"
    base_args = [
        sys.executable,
        str(HELPER),
        "worker",
        str(settings.database_path),
    ]
    first = subprocess.Popen(
        base_args
        + [str(ready_one), str(release), str(summary_one), _utc_text(NOW)],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 15
    while not ready_one.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready_one.exists()
    second = subprocess.Popen(
        base_args
        + [str(ready_two), str(release), str(summary_two), _utc_text(NOW)],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second_stdout, second_stderr = second.communicate(timeout=15)
    assert second.returncode == 0, (second_stdout, second_stderr)
    assert not ready_two.exists()
    release.write_text("release", encoding="ascii")
    first_stdout, first_stderr = first.communicate(timeout=15)
    assert first.returncode == 0, (first_stdout, first_stderr)

    assert json.loads(summary_one.read_text(encoding="ascii")) == {
        "claimed_jobs": 1,
        "completed_jobs": 1,
        "deferred_jobs": 0,
        "failed_jobs": 0,
    }
    assert json.loads(summary_two.read_text(encoding="ascii")) == {
        "claimed_jobs": 0,
        "completed_jobs": 0,
        "deferred_jobs": 0,
        "failed_jobs": 0,
    }
    status, unit, attempts, reservations, _ = _job_state(settings, request.job_id)
    assert status == "succeeded"
    assert unit["attempt_count"] == 1
    assert attempts[0][:2] == (1, "success")
    assert reservations == ((1, 1, "videos_list", _utc_text(NOW)),)
