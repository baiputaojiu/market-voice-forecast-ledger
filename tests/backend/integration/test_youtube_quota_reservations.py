from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

import market_voice_forecast_ledger.repositories.discovery as discovery_repository_module
from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import (
    EndpointClass,
    READ_UNIT_DAILY_LIMIT,
    SafeTransportFailure,
    SEARCH_CALL_DAILY_LIMIT,
    YouTubeClient,
    YouTubeProviderFailure,
)
from tests.backend.youtube_fakes import (
    FIXED_NOW,
    FakeCredentialStore,
    FakeYouTubeTransport,
    RecordingSleeper,
    fixed_clock,
)


UNIT_KEY = "youtube:profile:1:search"
OTHER_UNIT_KEY = "youtube:profile:2:search"
PRIVATE_SENTINEL = "private-provider-body-and-local-path-sentinel"


@pytest.fixture
def quota_db(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    conn = open_database(database_path)
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    with transaction(conn):
        job_id = _insert_job_with_unit(conn, UNIT_KEY)
    try:
        yield conn, database_path, job_id
    finally:
        conn.close()


def _insert_job_with_unit(
    conn: sqlite3.Connection,
    unit_key: str,
    *,
    job_kind: str = "youtube_sync",
    stage: str = "youtube_search_discovery",
) -> int:
    timestamp = "2026-08-18T02:03:04.000000Z"
    cursor = conn.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES (?, ?, 1, 'queued', ?, ?)
        """,
        (job_kind, f"synthetic-manifest-{unit_key}", timestamp, timestamp),
    )
    job_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO job_units(
            job_id, unit_key, stage, ordinal, declared_input_hash,
            dependency_keys_json, execution_contract_hash, status
        ) VALUES (?, ?, ?, 1, 'synthetic-input', '[]',
                  'synthetic-contract', 'pending')
        """,
        (job_id, unit_key, stage),
    )
    return job_id


def _callback(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    unit_key: str = UNIT_KEY,
    request_ordinal: int = 1,
):
    return DiscoveryRepository(conn).youtube_attempt_reservation(
        job_id=job_id,
        unit_key=unit_key,
        request_ordinal=request_ordinal,
        search_daily_limit=SEARCH_CALL_DAILY_LIMIT,
        read_daily_limit=READ_UNIT_DAILY_LIMIT,
    )


def _rows(conn: sqlite3.Connection):
    return tuple(
        conn.execute(
            "SELECT job_id, unit_key, request_ordinal, attempt_no, "
            "endpoint_class, attempted_at "
            "FROM youtube_quota_reservations ORDER BY id"
        )
    )


def _seed_reservations(
    conn: sqlite3.Connection,
    job_id: int,
    count: int,
    *,
    endpoint_classes: tuple[str, ...],
    attempted_at: datetime = FIXED_NOW,
    unit_key: str = UNIT_KEY,
) -> None:
    attempted_at_text = attempted_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with transaction(conn):
        conn.executemany(
            "INSERT INTO youtube_quota_reservations("
            "job_id, unit_key, request_ordinal, attempt_no, endpoint_class, "
            "attempted_at) VALUES (?, ?, ?, 1, ?, ?)",
            (
                (
                    job_id,
                    unit_key,
                    request_ordinal,
                    endpoint_classes[(request_ordinal - 1) % len(endpoint_classes)],
                    attempted_at_text,
                )
                for request_ordinal in range(1, count + 1)
            ),
        )


def _quota_error(callback, endpoint_class, attempt_no, attempted_at) -> str:
    try:
        callback(endpoint_class, attempt_no, attempted_at)
    except DomainError as cause:
        return cause.code
    return "accepted"


def test_search_bucket_accepts_call_100_and_rejects_call_101(quota_db):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        99,
        endpoint_classes=("search_list",),
    )

    _callback(conn, job_id, request_ordinal=100)(
        EndpointClass.SEARCH_LIST, 1, FIXED_NOW
    )
    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id, request_ordinal=101)(
            EndpointClass.SEARCH_LIST, 1, FIXED_NOW
        )

    assert caught.value.code == "YOUTUBE_QUOTA_EXHAUSTED"
    assert str(caught.value) == "YouTube daily quota is exhausted"
    assert len(_rows(conn)) == 100


def test_combined_read_bucket_accepts_unit_10000_and_rejects_unit_10001(
    quota_db,
):
    conn, _database_path, job_id = quota_db
    read_endpoints = ("channels_list", "playlist_items_list", "videos_list")
    _seed_reservations(
        conn,
        job_id,
        9_999,
        endpoint_classes=read_endpoints,
    )

    _callback(conn, job_id, request_ordinal=10_000)(
        EndpointClass.VIDEOS_LIST, 1, FIXED_NOW
    )
    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id, request_ordinal=10_001)(
            EndpointClass.CHANNELS_LIST, 1, FIXED_NOW
        )

    assert caught.value.code == "YOUTUBE_QUOTA_EXHAUSTED"
    assert len(_rows(conn)) == 10_000


def test_each_retry_attempt_charges_the_daily_bucket(quota_db):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        99,
        endpoint_classes=("search_list",),
    )
    callback = _callback(conn, job_id, request_ordinal=100)
    callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.SEARCH_LIST, 2, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_EXHAUSTED"
    assert [row[3] for row in _rows(conn)[-1:]] == [1]
    assert len(_rows(conn)) == 100


def test_quota_bucket_rolls_over_at_the_next_utc_day(quota_db):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        100,
        endpoint_classes=("search_list",),
    )

    _callback(conn, job_id, request_ordinal=101)(
        EndpointClass.SEARCH_LIST, 1, FIXED_NOW + timedelta(days=1)
    )

    assert len(_rows(conn)) == 101


def test_rolled_back_reservation_does_not_consume_the_last_daily_slot(quota_db):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        99,
        endpoint_classes=("search_list",),
    )

    class SyntheticRollback(Exception):
        pass

    with pytest.raises(SyntheticRollback):
        with transaction(conn):
            DiscoveryRepository(conn).reserve_youtube_quota_attempt(
                job_id=job_id,
                unit_key=UNIT_KEY,
                request_ordinal=100,
                attempt_no=1,
                endpoint_class="search_list",
                attempted_at=FIXED_NOW,
                search_daily_limit=SEARCH_CALL_DAILY_LIMIT,
                read_daily_limit=READ_UNIT_DAILY_LIMIT,
            )
            raise SyntheticRollback()

    _callback(conn, job_id, request_ordinal=100)(
        EndpointClass.SEARCH_LIST, 1, FIXED_NOW
    )
    assert len(_rows(conn)) == 100


def test_concurrent_reservations_cannot_overrun_the_last_daily_slot(quota_db):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        99,
        endpoint_classes=("search_list",),
    )
    barrier = Barrier(2)
    callbacks = (
        _callback(conn, job_id, request_ordinal=100),
        _callback(conn, job_id, request_ordinal=101),
    )

    def reserve(callback) -> str:
        barrier.wait()
        return _quota_error(
            callback,
            EndpointClass.SEARCH_LIST,
            1,
            FIXED_NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, callbacks))

    assert sorted(outcomes) == ["YOUTUBE_QUOTA_EXHAUSTED", "accepted"]
    assert len(_rows(conn)) == 100


def test_committed_crash_reservation_conservatively_consumes_daily_slot(
    quota_db,
):
    conn, _database_path, job_id = quota_db
    _seed_reservations(
        conn,
        job_id,
        99,
        endpoint_classes=("search_list",),
    )

    class SyntheticProcessCrash(BaseException):
        pass

    class CrashTransport:
        def get_json(self, _endpoint, _params, _api_key):
            raise SyntheticProcessCrash()

    client = YouTubeClient(
        transport=CrashTransport(),
        credential_store=FakeCredentialStore(),
        reserve_attempt=_callback(conn, job_id, request_ordinal=100),
        sleeper=RecordingSleeper(),
        clock=fixed_clock,
    )
    with pytest.raises(SyntheticProcessCrash):
        client.search_videos(
            "Synthetic analyst",
            "2023-08-17T23:59:59.000000Z",
            "2026-08-18T00:00:00.000000Z",
            None,
        )

    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id, request_ordinal=101)(
            EndpointClass.SEARCH_LIST, 1, FIXED_NOW
        )

    assert caught.value.code == "YOUTUBE_QUOTA_EXHAUSTED"
    assert len(_rows(conn)) == 100


def test_local_quota_exhaustion_defers_without_transport_or_cursor_progress(
    tmp_path,
):
    conn = open_database(tmp_path / "defer.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)

    class TransportMustNotRun:
        calls = 0

        def get_json(self, _endpoint, _params, _api_key):
            self.calls += 1
            raise AssertionError("transport must not run after local quota exhaustion")

    transport = TransportMustNotRun()
    service = YouTubeSyncService(
        conn,
        clock=fixed_clock,
        credential_store=FakeCredentialStore(),
        transport=transport,
        sleeper=RecordingSleeper(),
    )
    profile = DiscoveryRepository(conn).list_active_profile_versions()[0]
    request = service.request_manual_candidate(
        profile.subject_id,
        "https://youtu.be/quota000001",
        FIXED_NOW,
    )
    unit_key = conn.execute(
        "SELECT unit_key FROM job_units WHERE job_id=?",
        (request.job_id,),
    ).fetchone()["unit_key"]
    _seed_reservations(
        conn,
        request.job_id,
        10_000,
        endpoint_classes=("channels_list", "playlist_items_list", "videos_list"),
        unit_key=unit_key,
    )
    before_manual_rows = tuple(
        conn.execute(
            "SELECT * FROM manual_discovery_requests WHERE id=?",
            (request.request_id,),
        )
    )

    claimed = service.claim_next_runnable(FIXED_NOW)
    assert claimed is not None
    status = service.execute_claimed_job(claimed)

    unit = conn.execute(
        "SELECT status, error_code FROM job_units WHERE job_id=?",
        (request.job_id,),
    ).fetchone()
    assert status.value == "retrying"
    assert tuple(unit) == ("pending", None)
    assert tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT result_status, error_code FROM job_unit_attempts "
            "WHERE job_id=?",
            (request.job_id,),
        )
    ) == (("failed", "YOUTUBE_QUOTA_EXHAUSTED"),)
    assert service.get_sync_manifest(request.job_id).resume_not_before_utc == (
        FIXED_NOW + timedelta(days=1)
    )
    assert transport.calls == 0
    assert len(
        tuple(
            conn.execute(
                "SELECT id FROM youtube_quota_reservations WHERE job_id=?",
                (request.job_id,),
            )
        )
    ) == 10_000
    assert tuple(
        conn.execute(
            "SELECT * FROM manual_discovery_requests WHERE id=?",
            (request.request_id,),
        )
    ) == before_manual_rows


def test_callback_commits_one_minimal_reservation_in_its_own_short_transaction(
    quota_db,
):
    conn, database_path, job_id = quota_db
    reservation = _callback(conn, job_id)
    assert conn.in_transaction is False

    reservation(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)

    assert conn.in_transaction is False
    reopened = open_database(database_path)
    try:
        assert [tuple(row) for row in _rows(reopened)] == [(
            job_id,
            UNIT_KEY,
            1,
            1,
            "search_list",
            "2026-08-18T02:03:04.000000Z",
        )]
    finally:
        reopened.close()


def test_reservation_is_committed_before_transport_and_survives_call_crash(quota_db):
    conn, database_path, job_id = quota_db
    observed_before_call: list[tuple[object, ...]] = []

    class SyntheticProcessCrash(BaseException):
        pass

    class CrashAfterInspectingTransport:
        def get_json(self, _endpoint, _params, _api_key):
            reopened = open_database(database_path)
            try:
                observed_before_call.extend(tuple(row) for row in _rows(reopened))
            finally:
                reopened.close()
            raise SyntheticProcessCrash()

    client = YouTubeClient(
        transport=CrashAfterInspectingTransport(),
        credential_store=FakeCredentialStore(),
        reserve_attempt=_callback(conn, job_id),
        sleeper=RecordingSleeper(),
        clock=fixed_clock,
    )

    with pytest.raises(SyntheticProcessCrash):
        client.videos(("video000001",))

    assert len(observed_before_call) == 1
    assert observed_before_call[0][3:] == (
        1,
        "videos_list",
        "2026-08-18T02:03:04.000000Z",
    )
    reopened = open_database(database_path)
    try:
        assert len(_rows(reopened)) == 1
    finally:
        reopened.close()


def test_four_transport_attempts_commit_four_distinct_reservations(quota_db):
    conn, _database_path, job_id = quota_db
    failure = SafeTransportFailure(kind="network")
    transport = FakeYouTubeTransport(
        responses=(failure, failure, failure, failure)
    )
    client = YouTubeClient(
        transport=transport,
        credential_store=FakeCredentialStore(),
        reserve_attempt=_callback(conn, job_id),
        sleeper=RecordingSleeper(),
        clock=fixed_clock,
    )

    with pytest.raises(YouTubeProviderFailure) as caught:
        client.search_videos(
            "Synthetic analyst",
            "2023-08-17T23:59:59.000000Z",
            "2026-08-18T00:00:00.000000Z",
            None,
        )

    assert caught.value.category == "transient"
    assert [row[3] for row in _rows(conn)] == [1, 2, 3, 4]
    assert {row[4] for row in _rows(conn)} == {"search_list"}


@pytest.mark.parametrize(
    "endpoint_class",
    tuple(EndpointClass),
)
def test_every_endpoint_class_is_persisted_as_the_exact_safe_value(
    quota_db,
    endpoint_class,
):
    conn, _database_path, job_id = quota_db
    ordinal = tuple(EndpointClass).index(endpoint_class) + 1

    _callback(conn, job_id, request_ordinal=ordinal)(
        endpoint_class, 1, FIXED_NOW
    )

    assert _rows(conn)[-1][4] == endpoint_class.value


@pytest.mark.parametrize(
    ("job_id", "unit_key", "request_ordinal"),
    (
        (True, UNIT_KEY, 1),
        (0, UNIT_KEY, 1),
        (1, "", 1),
        (1, PRIVATE_SENTINEL + "\n", 1),
        (1, UNIT_KEY, True),
        (1, UNIT_KEY, 0),
    ),
)
def test_invalid_job_unit_and_request_identity_fail_before_opening_a_reservation(
    quota_db,
    job_id,
    unit_key,
    request_ordinal,
):
    conn, _database_path, real_job_id = quota_db
    if type(job_id) is int and job_id == 1:
        job_id = real_job_id

    with pytest.raises(DomainError) as caught:
        _callback(
            conn,
            job_id,
            unit_key=unit_key,
            request_ordinal=request_ordinal,
        )

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert PRIVATE_SENTINEL not in str(caught.value)
    assert _rows(conn) == ()


def test_missing_job_and_unit_mismatch_fail_closed_without_sqlite_details(quota_db):
    conn, _database_path, job_id = quota_db
    with transaction(conn):
        other_job_id = _insert_job_with_unit(conn, OTHER_UNIT_KEY)

    for invalid_job_id, invalid_unit_key in (
        (999_999, UNIT_KEY),
        (job_id, OTHER_UNIT_KEY),
        (other_job_id, UNIT_KEY),
    ):
        with pytest.raises(DomainError) as caught:
            _callback(
                conn,
                invalid_job_id,
                unit_key=invalid_unit_key,
            )
        assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
        assert "FOREIGN KEY" not in str(caught.value)
        assert invalid_unit_key not in str(caught.value)


@pytest.mark.parametrize(
    ("job_kind", "stage"),
    (
        ("video_pipeline", "video_metadata"),
        ("youtube_sync", "video_metadata"),
    ),
)
def test_non_youtube_job_or_unit_stage_is_rejected_without_writing(
    quota_db,
    job_kind,
    stage,
):
    conn, _database_path, _job_id = quota_db
    with transaction(conn):
        invalid_job_id = _insert_job_with_unit(
            conn,
            f"invalid:{job_kind}:{stage}",
            job_kind=job_kind,
            stage=stage,
        )

    with pytest.raises(DomainError) as caught:
        _callback(
            conn,
            invalid_job_id,
            unit_key=f"invalid:{job_kind}:{stage}",
        )

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert _rows(conn) == ()


@pytest.mark.parametrize(
    ("endpoint_class", "attempt_no", "attempted_at"),
    (
        ("videos_list", 1, FIXED_NOW),
        (EndpointClass.VIDEOS_LIST, True, FIXED_NOW),
        (EndpointClass.VIDEOS_LIST, 0, FIXED_NOW),
        (EndpointClass.VIDEOS_LIST, 1, datetime(2026, 8, 18, 2, 3, 4)),
        (
            EndpointClass.VIDEOS_LIST,
            1,
            datetime(2026, 8, 18, 2, 3, 4, tzinfo=timezone.utc).astimezone(),
        ),
    ),
)
def test_invalid_endpoint_attempt_or_time_fail_closed_without_writing(
    quota_db,
    endpoint_class,
    attempt_no,
    attempted_at,
):
    conn, _database_path, job_id = quota_db

    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id)(endpoint_class, attempt_no, attempted_at)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert _rows(conn) == ()


def test_noncontiguous_attempt_number_fails_closed(quota_db):
    conn, _database_path, job_id = quota_db

    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id)(EndpointClass.SEARCH_LIST, 2, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_SEQUENCE_INVALID"
    assert _rows(conn) == ()


def test_endpoint_class_cannot_change_within_one_logical_request(quota_db):
    conn, database_path, job_id = quota_db
    callback = _callback(conn, job_id)
    callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)
    before = [tuple(row) for row in _rows(conn)]

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.VIDEOS_LIST, 2, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert str(caught.value) == "YouTube quota reservation is invalid"
    assert "search_list" not in repr(caught.value)
    assert "videos_list" not in repr(caught.value)
    assert str(database_path) not in repr(caught.value)
    assert [tuple(row) for row in _rows(conn)] == before
    assert conn.in_transaction is False


def test_stored_mixed_endpoint_classes_fail_as_corruption_without_new_write(
    quota_db,
):
    conn, database_path, job_id = quota_db
    _callback(conn, job_id)(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)
    with transaction(conn):
        conn.execute(
            "INSERT INTO youtube_quota_reservations("
            "job_id, unit_key, request_ordinal, attempt_no, endpoint_class, "
            "attempted_at) VALUES (?, ?, 1, 2, 'videos_list', ?)",
            (job_id, UNIT_KEY, "2026-08-18T02:03:05.000000Z"),
        )
    before = [tuple(row) for row in _rows(conn)]

    with pytest.raises(DomainError) as caught:
        _callback(conn, job_id)(EndpointClass.SEARCH_LIST, 3, FIXED_NOW)

    assert caught.value.code == "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert str(caught.value) == "stored YouTube quota reservation is invalid"
    assert "search_list" not in repr(caught.value)
    assert "videos_list" not in repr(caught.value)
    assert str(database_path) not in repr(caught.value)
    assert [tuple(row) for row in _rows(conn)] == before
    assert conn.in_transaction is False


def test_attempt_five_is_rejected_by_the_four_attempt_storage_contract(quota_db):
    conn, _database_path, job_id = quota_db
    callback = _callback(conn, job_id)
    for attempt_no in range(1, 5):
        callback(EndpointClass.SEARCH_LIST, attempt_no, FIXED_NOW)

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.SEARCH_LIST, 5, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert [row[3] for row in _rows(conn)] == [1, 2, 3, 4]


def test_database_open_failure_is_reduced_without_native_or_path_text(
    quota_db,
    monkeypatch,
):
    conn, database_path, job_id = quota_db
    callback = _callback(conn, job_id)

    def fail_open(_path):
        raise sqlite3.OperationalError(f"native {PRIVATE_SENTINEL} {database_path}")

    monkeypatch.setattr(discovery_repository_module, "open_database", fail_open)

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_STORAGE_FAILED"
    assert PRIVATE_SENTINEL not in str(caught.value)
    assert str(database_path) not in str(caught.value)


def test_duplicate_reservation_is_rejected_and_original_row_is_unchanged(quota_db):
    conn, _database_path, job_id = quota_db
    callback = _callback(conn, job_id)
    callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)
    before = [tuple(row) for row in _rows(conn)]

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)

    assert caught.value.code == "YOUTUBE_QUOTA_RESERVATION_CONFLICT"
    assert [tuple(row) for row in _rows(conn)] == before


def test_corrupt_duplicate_row_fails_closed_without_disclosing_stored_text(quota_db):
    conn, _database_path, job_id = quota_db
    callback = _callback(conn, job_id)
    callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)
    conn.execute("DROP TRIGGER youtube_quota_reservations_no_update")
    conn.execute(
        "UPDATE youtube_quota_reservations SET attempted_at=?",
        (PRIVATE_SENTINEL,),
    )

    with pytest.raises(DomainError) as caught:
        callback(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)

    assert caught.value.code == "STORED_YOUTUBE_QUOTA_RESERVATION_INVALID"
    assert PRIVATE_SENTINEL not in str(caught.value)


def test_raw_update_delete_and_replace_remain_append_only(quota_db):
    conn, _database_path, job_id = quota_db
    _callback(conn, job_id)(EndpointClass.SEARCH_LIST, 1, FIXED_NOW)
    original_id = conn.execute(
        "SELECT id FROM youtube_quota_reservations"
    ).fetchone()["id"]

    statements = (
        (
            "UPDATE youtube_quota_reservations SET endpoint_class='videos_list' "
            "WHERE id=?",
            (original_id,),
        ),
        ("DELETE FROM youtube_quota_reservations WHERE id=?", (original_id,)),
        (
            "INSERT OR REPLACE INTO youtube_quota_reservations("
            "id, job_id, unit_key, request_ordinal, attempt_no, "
            "endpoint_class, attempted_at) VALUES (?, ?, ?, 1, 1, "
            "'videos_list', '2026-08-18T02:03:05.000000Z')",
            (original_id + 1, job_id, UNIT_KEY),
        ),
    )
    for sql, params in statements:
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
            conn.execute(sql, params)

    assert len(_rows(conn)) == 1


def test_reservation_schema_contains_only_approved_safe_fields(quota_db):
    conn, _database_path, _job_id = quota_db
    columns = tuple(
        row["name"]
        for row in conn.execute("PRAGMA table_info(youtube_quota_reservations)")
    )

    assert columns == (
        "id",
        "job_id",
        "unit_key",
        "request_ordinal",
        "attempt_no",
        "endpoint_class",
        "attempted_at",
    )
    assert not {
        "api_key",
        "request_url",
        "provider_body",
        "page_token",
        "title",
        "description",
    } & set(columns)


def test_direct_insert_primitive_requires_a_caller_transaction(quota_db):
    conn, _database_path, job_id = quota_db

    with pytest.raises(DomainError) as caught:
        DiscoveryRepository(conn).reserve_youtube_quota_attempt(
            job_id=job_id,
            unit_key=UNIT_KEY,
            request_ordinal=1,
            attempt_no=1,
            endpoint_class="search_list",
            attempted_at=FIXED_NOW,
            search_daily_limit=SEARCH_CALL_DAILY_LIMIT,
            read_daily_limit=READ_UNIT_DAILY_LIMIT,
        )

    assert caught.value.code == "DISCOVERY_TRANSACTION_REQUIRED"
    assert _rows(conn) == ()
