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
from market_voice_forecast_ledger.domain.discovery import DiscoverySourceKind
from market_voice_forecast_ledger.domain.enums import JobKind
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.discovery_profiles import (
    DiscoveryProfileService,
    ReplaceDiscoveryProfileVersion,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.metadata import normalize_video_item
from tests.backend.youtube_fakes import FakeYouTubeClient, synthetic_video_item


VIDEO_ID = "abcdefghijk"
FOREIGN_VIDEO_ID = "lmnopqrstuv"
WATCH_URL = f"https://youtube.com/watch?v={VIDEO_ID}"
SHORT_URL = f"https://youtu.be/{VIDEO_ID}"
PRIVATE_QUERY = "private_query_sentinel"
PRIVATE_PAGE_TOKEN = "private_page_token_sentinel"
PRIVATE_TITLE = "private title sentinel"
PRIVATE_DESCRIPTION = "private description sentinel"
PRIVATE_CURRENT_TITLE = "private current title sentinel"
PRIVATE_CURRENT_DESCRIPTION = "private current description sentinel"
PRIVATE_FOREIGN_TITLE = "private foreign title sentinel"
PRIVATE_FOREIGN_DESCRIPTION = "private foreign description sentinel"
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


def _drop_table_triggers(conn, *table_names: str) -> None:
    placeholders = ",".join("?" for _ in table_names)
    names = tuple(
        row["name"]
        for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='trigger' "
            f"AND tbl_name IN ({placeholders}) ORDER BY name",
            table_names,
        )
    )
    for name in names:
        conn.execute(f'DROP TRIGGER "{name}"')


def _partial_seed_observation(
    settings: Settings,
    *,
    failed: bool,
    requested_at: datetime | None = None,
    title: str = PRIVATE_TITLE,
    description: str = PRIVATE_DESCRIPTION,
):
    if requested_at is None:
        requested_at = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
    conn = open_database(settings.database_path)
    try:
        service = YouTubeSyncService(conn, clock=lambda: requested_at)
        result = service.request_full_sync(requested_at)
        claimed = service.claim_next_runnable(requested_at)
        assert claimed is not None and claimed.job_id == result.job_id
        unit_key = conn.execute(
            "SELECT unit_key FROM job_units WHERE job_id=? AND status='running'",
            (result.job_id,),
        ).fetchone()["unit_key"]
        repository = DiscoveryRepository(conn)
        checkpoint = repository.get_youtube_sync_checkpoint(
            result.job_id, unit_key
        )
        manifest = service.get_sync_manifest(result.job_id)
        profiles = tuple(
            repository.get_profile_version(item.profile_version_id)
            for item in manifest.profiles
        )
        profile = next(
            item
            for item in profiles
            if checkpoint.source_key in item.seed_channel_ids
        )
        metadata = normalize_video_item(
            synthetic_video_item(
                video_id=VIDEO_ID,
                title=title,
                description=description,
                snippet_published_at="2026-08-18T01:02:03Z",
            ),
            fetched_at=requested_at,
        )
        with transaction(conn):
            repository.bind_seed_uploads_playlist(
                job_id=result.job_id,
                unit_key=unit_key,
                source_key=checkpoint.source_key,
                uploads_playlist_id="UU" + checkpoint.source_key[2:],
            )
            repository.persist_metadata_batch(
                result.job_id,
                profile.id,
                DiscoverySourceKind.SEED_UPLOADS,
                checkpoint.source_key,
                (metadata,),
                requested_at,
            )
            repository.advance_seed_checkpoint(
                job_id=result.job_id,
                unit_key=unit_key,
                next_page_token=PRIVATE_PAGE_TOKEN,
                encountered_video_ids=(VIDEO_ID,),
                unavailable_video_ids=(),
            )
        if failed:
            JobStateService(conn, clock=lambda: requested_at).fail_unit(
                result.job_id, unit_key, "YOUTUBE_PROVIDER_TRANSIENT"
            )
        row = conn.execute(
            "SELECT observation.id AS observation_id, observation.video_id, "
            "observation.metadata_snapshot_id, candidate.id AS candidate_id, "
            "candidate.current_presence_decision_id, checkpoint.source_key "
            "FROM discovery_observations AS observation "
            "JOIN subject_video_candidates AS candidate "
            "ON candidate.profile_id=observation.profile_id "
            "AND candidate.video_id=observation.video_id "
            "JOIN youtube_sync_checkpoints AS checkpoint "
            "ON checkpoint.job_id=observation.job_id "
            "AND checkpoint.unit_key=? WHERE observation.job_id=?",
            (unit_key, result.job_id),
        ).fetchone()
        assert row is not None
        identity = dict(row)
        identity["profile_version_id"] = profile.id
        identity["unit_key"] = unit_key
        return result.job_id, identity
    finally:
        conn.close()


def _historical_and_current_seed_observations(settings: Settings):
    historical_job_id, historical = _partial_seed_observation(
        settings,
        failed=False,
        title=PRIVATE_TITLE,
        description=PRIVATE_DESCRIPTION,
    )
    conn = open_database(settings.database_path)
    try:
        state = JobStateService(
            conn,
            clock=lambda: datetime(
                2026, 8, 19, 3, 4, 6, tzinfo=timezone.utc
            ),
        )
        assert state.request_stop(historical_job_id).value == "cancel_requested"
        state.fail_unit(
            historical_job_id,
            historical["unit_key"],
            "YOUTUBE_PROVIDER_TRANSIENT",
        )
        assert state.status(historical_job_id).value == "stopped"
    finally:
        conn.close()

    current_job_id, current = _partial_seed_observation(
        settings,
        failed=False,
        requested_at=datetime(2026, 8, 20, 3, 4, 5, tzinfo=timezone.utc),
        title=PRIVATE_CURRENT_TITLE,
        description=PRIVATE_CURRENT_DESCRIPTION,
    )
    assert current_job_id != historical_job_id
    assert current["metadata_snapshot_id"] != historical["metadata_snapshot_id"]
    return historical_job_id, historical, current_job_id, current


def _persist_foreign_seed_observation(
    settings: Settings, job_id: int, identity: dict[str, object]
) -> int:
    observed_at = datetime(2026, 8, 20, 3, 4, 6, tzinfo=timezone.utc)
    metadata = normalize_video_item(
        synthetic_video_item(
            video_id=FOREIGN_VIDEO_ID,
            title=PRIVATE_FOREIGN_TITLE,
            description=PRIVATE_FOREIGN_DESCRIPTION,
            snippet_published_at="2026-08-18T01:02:04Z",
        ),
        fetched_at=observed_at,
    )
    conn = open_database(settings.database_path)
    try:
        repository = DiscoveryRepository(conn)
        with transaction(conn):
            repository.persist_metadata_batch(
                job_id,
                identity["profile_version_id"],
                DiscoverySourceKind.SEED_UPLOADS,
                identity["source_key"],
                (metadata,),
                observed_at,
            )
            repository.advance_seed_checkpoint(
                job_id=job_id,
                unit_key=identity["unit_key"],
                next_page_token=PRIVATE_PAGE_TOKEN,
                encountered_video_ids=tuple(
                    sorted((VIDEO_ID, FOREIGN_VIDEO_ID))
                ),
                unavailable_video_ids=(),
            )
        row = conn.execute(
            "SELECT observation.metadata_snapshot_id "
            "FROM discovery_observations AS observation "
            "JOIN videos AS video ON video.id=observation.video_id "
            "WHERE observation.job_id=? AND video.youtube_video_id=?",
            (job_id, FOREIGN_VIDEO_ID),
        ).fetchone()
        assert row is not None
        return row["metadata_snapshot_id"]
    finally:
        conn.close()


def _job_storage_snapshot(settings: Settings, job_id: int):
    conn = open_database(settings.database_path)
    try:
        tables = (
            "jobs",
            "job_units",
            "job_unit_attempts",
            "job_events",
            "manual_discovery_requests",
            "youtube_sync_manifests",
            "youtube_sync_manifest_profiles",
            "youtube_sync_checkpoints",
            "youtube_search_windows",
            "youtube_source_cursors",
            "youtube_sync_proposed_cursors",
            "youtube_quota_reservations",
            "youtube_daily_sync_requests",
            "videos",
            "video_metadata_snapshots",
            "discovery_observations",
            "subject_video_candidates",
            "presence_decisions",
        )
        return tuple(
            (
                table,
                tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
            )
            for table in tables
        )
    finally:
        conn.close()


def _set_youtube_job_status(
    settings: Settings,
    job_id: int,
    status: str,
    *,
    running: bool,
) -> None:
    conn = open_database(settings.database_path)
    try:
        if running:
            now = datetime(2026, 8, 20, tzinfo=timezone.utc)
            claimed = YouTubeSyncService(
                conn, clock=lambda: now
            ).claim_next_runnable(now)
            assert claimed is not None and claimed.job_id == job_id
            state = JobStateService(conn, clock=lambda: now)
            if status == "pause_requested":
                assert state.request_pause(job_id).value == status
            else:
                assert status == "cancel_requested"
                assert state.request_stop(job_id).value == status
        elif status == "stopped":
            assert JobStateService(conn).request_stop(job_id).value == status
        else:
            assert status == "paused"
            _drop_table_triggers(conn, "jobs")
            conn.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
            conn.commit()
    finally:
        conn.close()


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
            job_id, failed_unit, "YOUTUBE_PROVIDER_REQUEST_FAILED"
        )
    finally:
        conn.close()
    with _client(settings, wake) as client:
        failed = client.get(f"/api/youtube-syncs/{job_id}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert [unit["error_code"] for unit in failed.json()["units"]].count(
        "YOUTUBE_PROVIDER_REQUEST_FAILED"
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
        reused = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": WATCH_URL},
        )

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
    assert reused.status_code == 202
    assert reused.json() == {
        "request_id": created.json()["request_id"],
        "job_id": job_id,
        "status": "succeeded",
        "reused": True,
    }
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


@pytest.mark.parametrize("failed", (False, True), ids=("running", "failed"))
@pytest.mark.parametrize(
    "mutation",
    (
        "observation_hash",
        "observation_profile",
        "observation_source",
        "observation_video",
        "snapshot_owner",
        "snapshot_hash",
        "current_snapshot_link",
        "candidate_owner",
        "candidate_video_owner",
        "candidate_first_observation",
        "presence_evidence_hash",
    ),
)
def test_partial_status_revalidates_every_persisted_discovery_binding(
    settings: Settings,
    failed: bool,
    mutation: str,
):
    job_id, identity = _partial_seed_observation(settings, failed=failed)
    conn = open_database(settings.database_path)
    private_mutation = f"private_{mutation}_sentinel"
    try:
        _drop_table_triggers(
            conn,
            "discovery_observations",
            "video_metadata_snapshots",
            "videos",
            "subject_video_candidates",
            "presence_decisions",
        )
        conn.execute("PRAGMA foreign_keys=OFF")
        if mutation == "observation_hash":
            conn.execute(
                "UPDATE discovery_observations SET observation_hash=? WHERE id=?",
                (private_mutation, identity["observation_id"]),
            )
        elif mutation == "observation_profile":
            conn.execute(
                "UPDATE discovery_observations SET profile_id=999999 WHERE id=?",
                (identity["observation_id"],),
            )
        elif mutation == "observation_source":
            conn.execute(
                "UPDATE discovery_observations SET source_key=? WHERE id=?",
                (private_mutation, identity["observation_id"]),
            )
        elif mutation == "observation_video":
            conn.execute(
                "UPDATE discovery_observations SET video_id=999999 WHERE id=?",
                (identity["observation_id"],),
            )
        elif mutation == "snapshot_owner":
            conn.execute(
                "UPDATE video_metadata_snapshots SET video_id=999999 WHERE id=?",
                (identity["metadata_snapshot_id"],),
            )
        elif mutation == "snapshot_hash":
            conn.execute(
                "UPDATE video_metadata_snapshots SET canonical_hash=? WHERE id=?",
                (private_mutation, identity["metadata_snapshot_id"]),
            )
        elif mutation == "current_snapshot_link":
            conn.execute(
                "UPDATE videos SET current_metadata_snapshot_id=NULL WHERE id=?",
                (identity["video_id"],),
            )
        elif mutation == "candidate_owner":
            other_profile_id = conn.execute(
                "SELECT id FROM discovery_profiles WHERE id<>(SELECT profile_id "
                "FROM subject_video_candidates WHERE id=?) ORDER BY id LIMIT 1",
                (identity["candidate_id"],),
            ).fetchone()["id"]
            conn.execute(
                "UPDATE subject_video_candidates SET profile_id=? WHERE id=?",
                (other_profile_id, identity["candidate_id"]),
            )
        elif mutation == "candidate_video_owner":
            conn.execute(
                "UPDATE subject_video_candidates SET video_id=999999 WHERE id=?",
                (identity["candidate_id"],),
            )
        elif mutation == "candidate_first_observation":
            conn.execute(
                "UPDATE subject_video_candidates SET first_observation_id=999999 "
                "WHERE id=?",
                (identity["candidate_id"],),
            )
        else:
            conn.execute(
                "UPDATE presence_decisions SET evidence_hash=? WHERE id=?",
                (private_mutation, identity["current_presence_decision_id"]),
            )
        conn.commit()
    finally:
        conn.close()

    before = _job_storage_snapshot(settings, job_id)
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = client.get(f"/api/youtube-syncs/{job_id}")
    after = _job_storage_snapshot(settings, job_id)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    _assert_private_absent(
        response,
        (
            private_mutation,
            PRIVATE_TITLE,
            PRIVATE_DESCRIPTION,
            PRIVATE_PAGE_TOKEN,
            identity["source_key"],
            VIDEO_ID,
        ),
    )
    assert after == before
    assert wake.request_count == 0


def test_status_keeps_historical_snapshot_readable_after_later_metadata(
    settings: Settings,
):
    historical_job_id, historical, current_job_id, current = (
        _historical_and_current_seed_observations(settings)
    )
    conn = open_database(settings.database_path)
    try:
        pointer = conn.execute(
            "SELECT current_metadata_snapshot_id FROM videos WHERE id=?",
            (historical["video_id"],),
        ).fetchone()["current_metadata_snapshot_id"]
    finally:
        conn.close()
    assert pointer == current["metadata_snapshot_id"]
    assert pointer != historical["metadata_snapshot_id"]

    before = _job_storage_snapshot(settings, historical_job_id)
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        historical_response = client.get(
            f"/api/youtube-syncs/{historical_job_id}"
        )
        current_response = client.get(f"/api/youtube-syncs/{current_job_id}")
    after = _job_storage_snapshot(settings, historical_job_id)

    assert historical_response.status_code == 200
    assert historical_response.json()["status"] == "stopped"
    assert historical_response.json()["persisted_total"] == 1
    assert current_response.status_code == 200
    assert current_response.json()["status"] == "running"
    assert current_response.json()["persisted_total"] == 1
    for response in (historical_response, current_response):
        _assert_private_absent(
            response,
            (
                PRIVATE_TITLE,
                PRIVATE_DESCRIPTION,
                PRIVATE_CURRENT_TITLE,
                PRIVATE_CURRENT_DESCRIPTION,
                PRIVATE_PAGE_TOKEN,
                historical["source_key"],
                VIDEO_ID,
            ),
        )
    assert after == before
    assert wake.request_count == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "current_pointer_null",
        "current_pointer_foreign",
        "current_snapshot_owner",
        "current_snapshot_hash",
    ),
)
def test_historical_status_revalidates_the_independent_current_snapshot(
    settings: Settings,
    mutation: str,
):
    historical_job_id, historical, current_job_id, current = (
        _historical_and_current_seed_observations(settings)
    )
    private_mutation = f"private_{mutation}_sentinel"
    foreign_snapshot_id = None
    if mutation == "current_pointer_foreign":
        foreign_snapshot_id = _persist_foreign_seed_observation(
            settings, current_job_id, current
        )

    conn = open_database(settings.database_path)
    try:
        _drop_table_triggers(conn, "videos", "video_metadata_snapshots")
        conn.execute("PRAGMA foreign_keys=OFF")
        if mutation == "current_pointer_null":
            conn.execute(
                "UPDATE videos SET current_metadata_snapshot_id=NULL WHERE id=?",
                (historical["video_id"],),
            )
        elif mutation == "current_pointer_foreign":
            conn.execute(
                "UPDATE videos SET current_metadata_snapshot_id=? WHERE id=?",
                (foreign_snapshot_id, historical["video_id"]),
            )
        elif mutation == "current_snapshot_owner":
            conn.execute(
                "UPDATE video_metadata_snapshots SET video_id=999999 WHERE id=?",
                (current["metadata_snapshot_id"],),
            )
        else:
            conn.execute(
                "UPDATE video_metadata_snapshots SET canonical_hash=? WHERE id=?",
                (private_mutation, current["metadata_snapshot_id"]),
            )
        conn.commit()
    finally:
        conn.close()

    before = _job_storage_snapshot(settings, historical_job_id)
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        response = client.get(f"/api/youtube-syncs/{historical_job_id}")
    after = _job_storage_snapshot(settings, historical_job_id)

    assert response.status_code == 500
    assert response.json() == {"error": "INTERNAL_ERROR"}
    _assert_private_absent(
        response,
        (
            private_mutation,
            PRIVATE_TITLE,
            PRIVATE_DESCRIPTION,
            PRIVATE_CURRENT_TITLE,
            PRIVATE_CURRENT_DESCRIPTION,
            PRIVATE_FOREIGN_TITLE,
            PRIVATE_FOREIGN_DESCRIPTION,
            PRIVATE_PAGE_TOKEN,
            historical["source_key"],
            VIDEO_ID,
            FOREIGN_VIDEO_ID,
        ),
    )
    assert after == before
    assert wake.request_count == 0


PUBLIC_YOUTUBE_UNIT_ERROR_CODES = (
    "YOUTUBE_CREDENTIAL_NOT_CONFIGURED",
    "YOUTUBE_CREDENTIAL_INVALID",
    "YOUTUBE_CREDENTIAL_STORAGE_FAILED",
    "YOUTUBE_DISCOVERY_INVALID",
    "YOUTUBE_INVALID_PAGE_TOKEN",
    "YOUTUBE_METADATA_INVALID",
    "YOUTUBE_PROVIDER_DEFERRED",
    "YOUTUBE_PROVIDER_REQUEST_FAILED",
    "YOUTUBE_PROVIDER_TRANSIENT",
    "YOUTUBE_QUOTA_EXHAUSTED",
    "YOUTUBE_RESPONSE_INVALID",
    "YOUTUBE_SEARCH_RESPONSE_INVALID",
    "YOUTUBE_SEARCH_WINDOW_SATURATED",
    "YOUTUBE_SEED_RESPONSE_INVALID",
    "YOUTUBE_SYNC_DEPENDENCY_MISSING",
    "YOUTUBE_SYNC_FAILED",
)


def test_status_exposes_only_finite_provenanced_unit_error_codes(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        job_id = client.post("/api/youtube-syncs", json={}).json()["job_id"]
    service, conn, unit_key = _claim_running_unit(settings, job_id)
    del service
    try:
        JobStateService(conn).fail_unit(
            job_id, unit_key, PUBLIC_YOUTUBE_UNIT_ERROR_CODES[0]
        )
        _drop_table_triggers(conn, "job_units")
        conn.commit()
    finally:
        conn.close()

    app = create_app(settings)
    with _client_for_app(app, wake) as client:
        for code in PUBLIC_YOUTUBE_UNIT_ERROR_CODES:
            conn = open_database(settings.database_path)
            try:
                conn.execute(
                    "UPDATE job_units SET error_code=? WHERE job_id=? "
                    "AND status='failed'",
                    (code, job_id),
                )
                conn.commit()
            finally:
                conn.close()
            response = client.get(f"/api/youtube-syncs/{job_id}")
            assert response.status_code == 200
            assert response.json()["units"][0]["error_code"] == code

        for private_code in (
            "private_retry_detail_sentinel",
            "private_provider_body_sentinel",
            "private_api_key_sentinel_000001",
            "private_local_path_C_drive_sentinel",
        ):
            conn = open_database(settings.database_path)
            try:
                conn.execute(
                    "UPDATE job_units SET error_code=? WHERE job_id=? "
                    "AND status='failed'",
                    (private_code, job_id),
                )
                conn.commit()
            finally:
                conn.close()
            before = _job_storage_snapshot(settings, job_id)
            response = client.get(f"/api/youtube-syncs/{job_id}")
            after = _job_storage_snapshot(settings, job_id)
            assert response.status_code == 500
            assert response.json() == {"error": "INTERNAL_ERROR"}
            _assert_private_absent(response, (private_code,))
            assert after == before


@pytest.mark.parametrize(
    ("status", "running"),
    (
        ("pause_requested", True),
        ("paused", False),
        ("cancel_requested", True),
        ("stopped", False),
    ),
)
def test_get_and_manual_reuse_serialize_every_reachable_control_status(
    settings: Settings,
    status: str,
    running: bool,
):
    subject_id = _subject_id(settings)
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        created = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": SHORT_URL},
        ).json()
    _set_youtube_job_status(
        settings, created["job_id"], status, running=running
    )

    with _client(settings, wake) as client:
        read = client.get(f"/api/youtube-syncs/{created['job_id']}")
        reused = client.post(
            "/api/youtube-manual-candidates",
            json={"subject_id": subject_id, "url": WATCH_URL},
        )

    assert read.status_code == 200
    assert read.json()["status"] == status
    assert reused.status_code == 202
    assert reused.json() == {
        "request_id": created["request_id"],
        "job_id": created["job_id"],
        "status": status,
        "reused": True,
    }


@pytest.mark.parametrize(
    ("status", "running"),
    (
        ("pause_requested", True),
        ("paused", False),
        ("cancel_requested", True),
        ("stopped", False),
    ),
)
def test_full_request_after_noncoalescible_status_creates_then_reuses_one_job(
    settings: Settings,
    status: str,
    running: bool,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        original_id = client.post("/api/youtube-syncs", json={}).json()["job_id"]
    _set_youtube_job_status(settings, original_id, status, running=running)

    with _client(settings, wake) as client:
        created = client.post("/api/youtube-syncs", json={})
        reused = client.post("/api/youtube-syncs", json={})

    assert created.status_code == 202
    assert created.json() == {
        "job_id": original_id + 1,
        "status": "queued",
        "reused": False,
    }
    assert reused.status_code == 202
    assert reused.json() == {
        "job_id": original_id + 1,
        "status": "queued",
        "reused": True,
    }


def test_get_sync_status_allows_only_an_optional_exact_empty_body(
    settings: Settings,
):
    wake = FakeWakeAdapter()
    with _client(settings, wake) as client:
        job_id = client.post("/api/youtube-syncs", json={}).json()["job_id"]
        bodyless = client.get(f"/api/youtube-syncs/{job_id}")
        empty = client.request(
            "GET", f"/api/youtube-syncs/{job_id}", json={}
        )
        invalid = (
            client.request(
                "GET",
                f"/api/youtube-syncs/{job_id}",
                json={"unknown": "private_get_body_sentinel"},
            ),
            client.request("GET", f"/api/youtube-syncs/{job_id}", json=[]),
            client.request(
                "GET",
                f"/api/youtube-syncs/{job_id}",
                content="null",
                headers={"content-type": "application/json"},
            ),
            client.request("GET", f"/api/youtube-syncs/{job_id}", json=True),
            client.request("GET", f"/api/youtube-syncs/{job_id}", json="value"),
            client.request(
                "GET",
                f"/api/youtube-syncs/{job_id}",
                content="{private_invalid_json_sentinel",
                headers={"content-type": "application/json"},
            ),
        )

    assert bodyless.status_code == 200
    assert empty.status_code == 200
    for response in invalid:
        assert response.status_code == 422
        assert response.json()["error"] == "REQUEST_VALIDATION_FAILED"
        _assert_private_absent(
            response,
            (
                "private_get_body_sentinel",
                "private_invalid_json_sentinel",
            ),
        )
