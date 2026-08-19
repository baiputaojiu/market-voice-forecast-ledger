from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from market_voice_forecast_ledger.api import dependencies
from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.enums import JobKind
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.discovery_profiles import (
    DiscoveryProfileService,
    ReplaceDiscoveryProfileVersion,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from tests.backend.youtube_fakes import FakeYouTubeClient, synthetic_video_item


VIDEO_ID = "abcdefghijk"
WATCH_URL = f"https://youtube.com/watch?v={VIDEO_ID}"
SHORT_URL = f"https://youtu.be/{VIDEO_ID}"
PRIVATE_QUERY = "private_query_sentinel"
PRIVATE_PAGE_TOKEN = "private_page_token_sentinel"
PRIVATE_TITLE = "private title sentinel"
PRIVATE_DESCRIPTION = "private description sentinel"
PRIVATE_WAKE_DETAILS = (
    "private retry detail sentinel synthetic-youtube-key-000001 "
    "C:/private/ledger.sqlite3 provider body sentinel"
)


class FakeWakeAdapter:
    def __init__(self, *, on_request=None, error: BaseException | None = None):
        self._on_request = on_request
        self._error = error
        self.request_count = 0

    def request_start(self) -> None:
        self.request_count += 1
        if self._on_request is not None:
            self._on_request()
        if self._error is not None:
            raise self._error


@pytest.fixture
def settings(tmp_path) -> Settings:
    value = Settings.for_data_dir(tmp_path / "runtime")
    dependencies.initialize_database(value)
    return value


@contextmanager
def _client_for_app(app, wake: FakeWakeAdapter):
    wake_dependency = getattr(dependencies, "get_task_wake_adapter", None)
    dependency_factory = getattr(dependencies, "task_wake_dependency", None)
    if wake_dependency is not None and dependency_factory is not None:
        app.dependency_overrides[wake_dependency] = dependency_factory(wake)
    with TestClient(app) as value:
        yield value


@contextmanager
def _client(settings: Settings, wake: FakeWakeAdapter):
    with _client_for_app(create_app(settings), wake) as value:
        yield value


def _subject_id(settings: Settings, name: str = "大川智宏") -> int:
    conn = open_database(settings.database_path)
    try:
        row = conn.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name=?", (name,)
        ).fetchone()
        assert row is not None
        return row["id"]
    finally:
        conn.close()


def _job_state(settings: Settings, job_id: int):
    conn = open_database(settings.database_path)
    try:
        row = conn.execute(
            "SELECT job_kind, status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        conn.close()


def _claim_running_unit(settings: Settings, job_id: int) -> tuple[YouTubeSyncService, object, str]:
    conn = open_database(settings.database_path)
    service = YouTubeSyncService(conn)
    claimed = service.claim_next_runnable(
        datetime.now(timezone.utc) + timedelta(days=1)
    )
    assert claimed is not None
    assert claimed.job_id == job_id
    row = conn.execute(
        "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
        (job_id,),
    ).fetchone()
    assert row is not None
    return service, conn, row["unit_key"]


def _assert_private_absent(response, values: tuple[str, ...]) -> None:
    surface = response.text + "\n" + repr(tuple(response.headers.items()))
    for value in values:
        assert value not in surface


def test_post_sync_persists_before_wake_and_reuses_the_same_job(settings: Settings):
    observed: list[tuple[int, str]] = []

    def inspect_durable_job() -> None:
        conn = open_database(settings.database_path)
        try:
            rows = tuple(
                conn.execute(
                    "SELECT id, status FROM jobs WHERE job_kind='youtube_sync'"
                )
            )
            assert len(rows) == 1
            observed.append((rows[0]["id"], rows[0]["status"]))
        finally:
            conn.close()

    wake = FakeWakeAdapter(on_request=inspect_durable_job)
    with _client(settings, wake) as client:
        first = client.post("/api/youtube-syncs", json={})
        second = client.post("/api/youtube-syncs", json={})

    assert first.status_code == 202
    assert first.json() == {
        "job_id": 1,
        "status": "queued",
        "reused": False,
    }
    assert second.status_code == 202
    assert second.json() == {
        "job_id": 1,
        "status": "queued",
        "reused": True,
    }
    assert observed == [(1, "queued"), (1, "queued")]
    assert wake.request_count == 2


def test_failed_full_request_retries_the_same_job_before_wake(settings: Settings):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        first = client.post("/api/youtube-syncs", json={})
    job_id = first.json()["job_id"]
    service, conn, unit_key = _claim_running_unit(settings, job_id)
    try:
        JobStateService(conn).fail_unit(
            job_id, unit_key, "YOUTUBE_SYNTHETIC_FAILURE"
        )
    finally:
        conn.close()

    with _client(settings, wake) as client:
        retried = client.post("/api/youtube-syncs", json={})

    assert retried.status_code == 202
    assert retried.json() == {
        "job_id": job_id,
        "status": "retrying",
        "reused": True,
    }
    assert _job_state(settings, job_id) == (JobKind.YOUTUBE_SYNC.value, "retrying")
    assert wake.request_count == 2


@pytest.mark.parametrize(
    ("json_body", "path"),
    (
        (None, "/api/youtube-syncs"),
        ([], "/api/youtube-syncs"),
        ({"unknown": "private body sentinel"}, "/api/youtube-syncs"),
        ({}, "/api/youtube-syncs?unknown=private-query-sentinel"),
    ),
)
def test_post_sync_requires_an_exact_empty_json_object(
    settings: Settings, json_body, path: str
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = (
            client.post(path)
            if json_body is None
            else client.post(path, json=json_body)
        )

    assert response.status_code == 422
    assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"
    assert wake.request_count == 0
    _assert_private_absent(
        response,
        ("private body sentinel", "private-query-sentinel"),
    )


def test_full_wake_failure_is_503_and_keeps_the_durable_queue(settings: Settings):
    failing = FakeWakeAdapter(error=OSError(PRIVATE_WAKE_DETAILS))
    with _client(settings, failing) as client:
        response = client.post("/api/youtube-syncs", json={})

    assert response.status_code == 503
    assert response.json() == {"error": "YOUTUBE_SYNC_UNAVAILABLE"}
    _assert_private_absent(response, (PRIVATE_WAKE_DETAILS,))
    assert _job_state(settings, 1) == (JobKind.YOUTUBE_SYNC.value, "queued")

    working = FakeWakeAdapter()
    with _client(settings, working) as client:
        retry = client.post("/api/youtube-syncs", json={})
    assert retry.status_code == 202
    assert retry.json() == {"job_id": 1, "status": "queued", "reused": True}


def test_manual_candidate_is_strict_idempotent_and_wakes_after_persistence(
    settings: Settings,
):
    subject_id = _subject_id(settings)
    durable: list[tuple[int, int, str]] = []

    def inspect_request_and_job() -> None:
        conn = open_database(settings.database_path)
        try:
            row = conn.execute(
                "SELECT request.id, manifest.job_id, job.status "
                "FROM manual_discovery_requests AS request "
                "JOIN youtube_sync_manifests AS manifest "
                "ON manifest.manual_request_id=request.id "
                "JOIN jobs AS job ON job.id=manifest.job_id"
            ).fetchone()
            assert row is not None
            durable.append(tuple(row))
        finally:
            conn.close()

    wake = FakeWakeAdapter(on_request=inspect_request_and_job)
    with _client(settings, wake) as client:
        first = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        )
        second = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": WATCH_URL},
        )

    assert first.status_code == 202
    assert first.json() == {
        "request_id": 1,
        "job_id": 1,
        "status": "queued",
        "reused": False,
    }
    assert second.status_code == 202
    assert second.json() == {
        "request_id": 1,
        "job_id": 1,
        "status": "queued",
        "reused": True,
    }
    assert durable == [(1, 1, "queued"), (1, 1, "queued")]
    assert wake.request_count == 2


@pytest.mark.parametrize("subject_id", (True, 1.0, "1", 0, -1))
def test_manual_candidate_rejects_nonexact_positive_integer_subjects(
    settings: Settings, subject_id
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"
    assert wake.request_count == 0


@pytest.mark.parametrize("url", (True, 1, 1.0, "", "x" * 2049))
def test_manual_candidate_rejects_nonexact_bounded_string_urls(
    settings: Settings, url
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": _subject_id(settings), "url": url},
        )

    assert response.status_code == 422
    assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"
    assert wake.request_count == 0


@pytest.mark.parametrize(
    ("path", "body"),
    (
        (
            "/api/youtube-manual-candidates",
            {"subject_id": 1, "url": SHORT_URL, "unknown": "secret sentinel"},
        ),
        (
            "/api/youtube-manual-candidates?unknown=secret-query",
            {"subject_id": 1, "url": SHORT_URL},
        ),
    ),
)
def test_manual_candidate_rejects_unknown_body_and_query_fields(
    settings: Settings, path: str, body: dict[str, object]
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = client.post(path, json=body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "REQUEST_VALIDATION_FAILED"
    assert all(
        field["location"].split(".")[-1]
        in {"body", "query", "subject_id", "url", "unknown_field"}
        for field in payload["fields"]
    )
    _assert_private_absent(response, ("secret sentinel", "secret-query"))
    assert wake.request_count == 0


def test_manual_candidate_classifies_invalid_missing_and_inactive_inputs(
    settings: Settings,
):
    subject_id = _subject_id(settings)
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        invalid = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": "https://example.com/private"},
        )
        missing = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": 999999, "url": SHORT_URL},
        )
        conn = open_database(settings.database_path)
        try:
            conn.execute(
                "UPDATE analysis_subjects SET is_active=0 WHERE id=?", (subject_id,)
            )
            conn.commit()
        finally:
            conn.close()
        inactive = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        )

    assert invalid.status_code == 422
    assert invalid.json() == {"error": "INVALID_YOUTUBE_URL"}
    assert missing.status_code == 404
    assert missing.json() == {"error": "NOT_FOUND"}
    assert inactive.status_code == 422
    assert inactive.json() == {"error": "DISCOVERY_PROFILE_NOT_ACTIVE"}
    assert wake.request_count == 0


def test_manual_wake_failure_keeps_the_same_request_and_job(settings: Settings):
    subject_id = _subject_id(settings)
    failing = FakeWakeAdapter(error=RuntimeError(PRIVATE_WAKE_DETAILS))
    with _client(settings, failing) as client:
        response = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "YOUTUBE_SYNC_UNAVAILABLE"}
    _assert_private_absent(response, (PRIVATE_WAKE_DETAILS, SHORT_URL))
    assert _job_state(settings, 1) == (JobKind.YOUTUBE_SYNC.value, "queued")

    working = FakeWakeAdapter()
    with _client(settings, working) as client:
        retry = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": WATCH_URL},
        )
    assert retry.status_code == 202
    assert retry.json() == {
        "request_id": 1,
        "job_id": 1,
        "status": "queued",
        "reused": True,
    }


def test_get_sync_status_is_canonical_and_exposes_only_fixed_safe_fields(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        created = client.post("/api/youtube-syncs", json={})
        job_id = created.json()["job_id"]
        response = client.get(f"/api/youtube-syncs/{job_id}")

    expected_stages = (
        "youtube_seed_discovery",
        "youtube_search_discovery",
        "youtube_search_discovery",
        "youtube_seed_discovery",
        "youtube_search_discovery",
        "youtube_seed_discovery",
        "youtube_search_discovery",
    )
    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id,
        "status": "queued",
        "completed_units": 0,
        "total_units": 7,
        "resume_not_before_utc": None,
        "discovered_total": 0,
        "persisted_total": 0,
        "unavailable_total": 0,
        "units": [
            {
                "stage": stage,
                "status": "pending",
                "discovered_count": 0,
                "persisted_count": 0,
                "unavailable_count": 0,
                "error_code": None,
            }
            for stage in expected_stages
        ],
    }
    assert client.get(f"/api/youtube-syncs/{job_id}?private=yes").status_code == 422
    for path in ("0", "01", "+1", "1.0", "9999999999999999999"):
        assert client.get(f"/api/youtube-syncs/{path}").status_code in {404, 422}
    missing = client.get("/api/youtube-syncs/999999")
    assert missing.status_code == 404
    assert missing.json() == {"error": "JOB_NOT_FOUND"}


def test_get_sync_status_reports_defer_totals_and_only_safe_unit_errors(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        job_id = client.post("/api/youtube-syncs", json={}).json()["job_id"]
    service, conn, unit_key = _claim_running_unit(settings, job_id)
    resume_at = datetime.now(timezone.utc) + timedelta(hours=2)
    try:
        service.defer_current_unit(
            job_id,
            unit_key,
            "YOUTUBE_QUOTA_EXHAUSTED",
            resume_at,
        )
    finally:
        conn.close()
    with _client(settings, wake) as client:
        deferred = client.get(f"/api/youtube-syncs/{job_id}")
    assert deferred.status_code == 200
    assert deferred.json()["status"] == "retrying"
    assert deferred.json()["resume_not_before_utc"] == utc_iso(resume_at)
    assert deferred.json()["completed_units"] == 0
    assert deferred.json()["discovered_total"] == 0

    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE youtube_sync_manifests SET resume_not_before_utc=NULL "
            "WHERE job_id=?",
            (job_id,),
        )
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job_id,))
        conn.commit()
        claimed = YouTubeSyncService(conn).claim_next_runnable(
            datetime.now(timezone.utc) + timedelta(days=2)
        )
        assert claimed is not None
        failed_unit = conn.execute(
            "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
            (job_id,),
        ).fetchone()["unit_key"]
        JobStateService(conn).fail_unit(
            job_id, failed_unit, "YOUTUBE_SAFE_PROVIDER_ERROR"
        )
    finally:
        conn.close()
    with _client(settings, wake) as client:
        failed = client.get(f"/api/youtube-syncs/{job_id}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert [unit["error_code"] for unit in failed.json()["units"]].count(
        "YOUTUBE_SAFE_PROVIDER_ERROR"
    ) == 1


def test_get_sync_status_validates_manifest_checkpoint_and_error_provenance(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        job_id = client.post("/api/youtube-syncs", json={}).json()["job_id"]
    conn = open_database(settings.database_path)
    try:
        conn.execute(
            "UPDATE youtube_sync_checkpoints SET checkpoint_hash=? "
            "WHERE job_id=? AND unit_key=(SELECT unit_key FROM job_units "
            "WHERE job_id=? ORDER BY ordinal LIMIT 1)",
            ("0" * 64, job_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()
    with _client(settings, wake) as client:
        response = client.get(f"/api/youtube-syncs/{job_id}")
    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}


def test_completed_manual_status_aggregates_counts_without_metadata_or_url(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    subject_id = _subject_id(settings)
    with _client(settings, wake) as client:
        created = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        )
    job_id = created.json()["job_id"]
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    client_double = FakeYouTubeClient(
        video_responses=(
            (
                synthetic_video_item(
                    video_id=VIDEO_ID,
                    title=PRIVATE_TITLE,
                    description=PRIVATE_DESCRIPTION,
                ),
            ),
        )
    )
    conn = open_database(settings.database_path)
    try:
        service = YouTubeSyncService(
            conn, clock=lambda: now, youtube_client=client_double
        )
        claimed = service.claim_next_runnable(now)
        assert claimed is not None
        unit_key = conn.execute(
            "SELECT unit_key FROM job_units WHERE job_id=?", (job_id,)
        ).fetchone()["unit_key"]
        service.execute_manual_unit(job_id, unit_key)
    finally:
        conn.close()

    with _client(settings, wake) as client:
        response = client.get(f"/api/youtube-syncs/{job_id}")
        generic = client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id,
        "status": "succeeded",
        "completed_units": 1,
        "total_units": 1,
        "resume_not_before_utc": None,
        "discovered_total": 1,
        "persisted_total": 1,
        "unavailable_total": 0,
        "units": [
            {
                "stage": "youtube_manual_discovery",
                "status": "success",
                "discovered_count": 1,
                "persisted_count": 1,
                "unavailable_count": 0,
                "error_code": None,
            }
        ],
    }
    assert generic.status_code == 200
    assert generic.json()["kind"] == "youtube_sync"
    _assert_private_absent(
        response,
        (PRIVATE_TITLE, PRIVATE_DESCRIPTION, SHORT_URL, WATCH_URL, VIDEO_ID),
    )


def test_status_hides_stored_query_page_token_and_source_key(settings: Settings):
    app = create_app(settings)
    conn = open_database(settings.database_path)
    try:
        subject_id = conn.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name='大川智宏'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE analysis_subjects SET is_active=0 WHERE id<>?", (subject_id,)
        )
        conn.commit()
        DiscoveryProfileService(
            conn,
            clock=lambda: datetime(2026, 8, 19, tzinfo=timezone.utc),
        ).replace_version(
            ReplaceDiscoveryProfileVersion(
                subject_id=subject_id,
                seed_channel_ids=(),
                search_terms=(PRIVATE_QUERY,),
                reason="Synthetic collection profile update",
            )
        )
        service = YouTubeSyncService(conn)
        result = service.request_full_sync(
            datetime(2026, 8, 20, tzinfo=timezone.utc)
        )
        repository = DiscoveryRepository(conn)
        unit_key = conn.execute(
            "SELECT unit_key FROM job_units WHERE job_id=?", (result.job_id,)
        ).fetchone()["unit_key"]
        checkpoint = repository.get_youtube_sync_checkpoint(result.job_id, unit_key)
        window = repository.next_search_window(result.job_id, unit_key)
        assert window is not None
        with transaction(conn):
            repository.advance_search_window_page(
                job_id=result.job_id,
                unit_key=unit_key,
                window_id=window.id,
                next_page_token=PRIVATE_PAGE_TOKEN,
                encountered_video_ids=(),
                unavailable_video_ids=(),
            )
        source_key = checkpoint.source_key
    finally:
        conn.close()

    wake = FakeWakeAdapter()
    with _client_for_app(app, wake) as client:
        response = client.get(f"/api/youtube-syncs/{result.job_id}")

    assert response.status_code == 200
    assert response.json()["units"][0]["discovered_count"] == 0
    _assert_private_absent(
        response,
        (PRIVATE_QUERY, PRIVATE_PAGE_TOKEN, source_key),
    )
