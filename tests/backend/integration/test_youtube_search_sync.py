import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger import bootstrap
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain import discovery as discovery_domain
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.discovery import DiscoverySourceKind
from market_voice_forecast_ledger.domain.enums import JobStage, JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.discovery_profiles import (
    DiscoveryProfileService,
    ReplaceDiscoveryProfileVersion,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import (
    YouTubePage,
    YouTubeProviderFailure,
)
from tests.backend.youtube_fakes import (
    FakeYouTubeClient,
    synthetic_search_item,
    synthetic_video_item,
)


RUN_UPPER = datetime(2026, 1, 11, tzinfo=timezone.utc)
RUN_FLOOR = datetime(2023, 1, 11, tzinfo=timezone.utc)
SPLIT_BOUNDARY = datetime(2024, 7, 12, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    bootstrap.bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _video_id(index: int) -> str:
    return f"srch{index:07d}"


def _search_profile(db, *, terms: tuple[str, ...] | None = None):
    profiles = DiscoveryRepository(db).list_active_profile_versions()
    if terms is None:
        return next(profile for profile in profiles if not profile.seed_channel_ids)
    return next(profile for profile in profiles if profile.search_terms == terms)


def _unit_key(profile_id: int) -> str:
    return f"youtube:profile:{profile_id}:search"


def _start_search_unit(db, service: YouTubeSyncService, profile, upper=RUN_UPPER):
    request = service.request_full_sync(upper)
    unit_key = _unit_key(profile.profile_id)
    JobStateService(db, clock=lambda: upper).begin_unit(request.job_id, unit_key)
    assert JobStateService(db).unit(request.job_id, unit_key).status is UnitStatus.RUNNING
    return request.job_id, unit_key


def _insert_cursor(
    db,
    *,
    profile_id: int,
    source_kind: DiscoverySourceKind,
    source_key: str,
    completed_upper_bound: datetime,
    valid_hash: bool = True,
) -> None:
    cursor_hash = _canonical_hash({
        "completed_upper_bound": utc_iso(completed_upper_bound),
        "profile_id": profile_id,
        "schema": "youtube-source-cursor.v1",
        "source_key": source_key,
        "source_kind": source_kind.value,
    })
    with transaction(db):
        db.execute(
            "INSERT INTO youtube_source_cursors("
            "profile_id, source_kind, source_key, completed_upper_bound, "
            "cursor_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                profile_id,
                source_kind.value,
                source_key,
                utc_iso(completed_upper_bound),
                cursor_hash if valid_hash else "corrupt-cursor-hash",
                utc_iso(completed_upper_bound),
            ),
        )


def _search_item_page(video_ids: tuple[str, ...], token=None) -> YouTubePage:
    return YouTubePage(
        tuple(synthetic_search_item(video_id) for video_id in video_ids),
        token,
    )


def _checkpoint_ten_search_pages(db, job_id: int, unit_key: str):
    repository = DiscoveryRepository(db)
    window = repository.next_search_window(job_id, unit_key)
    assert window is not None
    for page_number in range(1, 11):
        with transaction(db):
            _, window = repository.advance_search_window_page(
                job_id=job_id,
                unit_key=unit_key,
                window_id=window.id,
                next_page_token=f"durable_token_{page_number}",
                encountered_video_ids=(),
                unavailable_video_ids=(),
            )
    assert window.page_count == 10
    assert window.next_page_token == "durable_token_10"
    return window


def test_source_key_and_three_calendar_year_floor_are_exact_and_leap_safe():
    terms = ("千竈鉄平", "千竃鉄平")
    expected_key = _canonical_hash({
        "ordered_terms": ["千竈鉄平", "千竃鉄平"],
        "schema": "youtube-search-source.v1",
    })

    assert discovery_domain.source_key_for_search_terms(terms) == expected_key
    assert discovery_domain.initial_backfill_floor(
        datetime(2028, 2, 29, 6, 7, 8, 900, tzinfo=timezone.utc)
    ) == datetime(2025, 2, 28, 6, 7, 8, 900, tzinfo=timezone.utc)
    assert discovery_domain.initial_backfill_floor(
        datetime(2027, 3, 1, tzinfo=timezone.utc)
    ) == datetime(2024, 3, 1, tzinfo=timezone.utc)


def test_source_cursor_identity_survives_version_change_but_new_sources_backfill(db):
    repository = DiscoveryRepository(db)
    original = _search_profile(db)
    old_upper = datetime(2025, 12, 31, tzinfo=timezone.utc)
    source_key = discovery_domain.youtube_search_source_key(original.search_terms)
    _insert_cursor(
        db,
        profile_id=original.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=old_upper,
    )

    new_seed = "UC0123456789abcdefghijkl"
    changed = DiscoveryProfileService(
        db, clock=lambda: datetime(2026, 1, 10, tzinfo=timezone.utc)
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=(new_seed,),
            search_terms=original.search_terms,
            reason="add a synthetic discovery seed",
        )
    )
    job_id = YouTubeSyncService(db).request_full_sync(RUN_UPPER).job_id

    search_checkpoint = repository.get_youtube_sync_checkpoint(
        job_id, _unit_key(changed.profile_id)
    )
    seed_checkpoint = repository.get_youtube_sync_checkpoint(
        job_id, f"youtube:profile:{changed.profile_id}:seed:{new_seed}"
    )
    root = repository.next_search_window(job_id, _unit_key(changed.profile_id))
    assert changed.id != original.id
    assert search_checkpoint.effective_lower_bound == old_upper
    assert root is not None
    assert (root.lower_bound, root.upper_bound) == (old_upper, RUN_UPPER)
    assert seed_checkpoint.effective_lower_bound == RUN_FLOOR

    changed_terms = original.search_terms + ("Synthetic New Spelling",)
    newer = DiscoveryProfileService(
        db, clock=lambda: datetime(2026, 1, 10, 1, tzinfo=timezone.utc)
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=(new_seed,),
            search_terms=changed_terms,
            reason="change the ordered synthetic search set",
        )
    )
    new_job_id = YouTubeSyncService(db).request_full_sync(
        RUN_UPPER + timedelta(hours=1)
    ).job_id
    new_checkpoint = repository.get_youtube_sync_checkpoint(
        new_job_id, _unit_key(newer.profile_id)
    )
    assert new_checkpoint.source_key != source_key
    assert new_checkpoint.effective_lower_bound == datetime(
        2023, 1, 11, 1, tzinfo=timezone.utc
    )


def test_long_idle_existing_cursor_precedes_initial_floor_across_profile_versions(db):
    repository = DiscoveryRepository(db)
    original = _search_profile(db)
    long_idle_cursor = datetime(2022, 1, 1, tzinfo=timezone.utc)
    upper = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source_key = discovery_domain.youtube_search_source_key(
        original.search_terms
    )
    _insert_cursor(
        db,
        profile_id=original.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=long_idle_cursor,
    )
    unchanged = DiscoveryProfileService(
        db, clock=lambda: upper - timedelta(days=1)
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=("UC0123456789abcdefghijkl",),
            search_terms=original.search_terms,
            reason="retain the long-idle synthetic search source",
        )
    )

    job_id = YouTubeSyncService(db).request_full_sync(upper).job_id

    checkpoint = repository.get_youtube_sync_checkpoint(
        job_id, _unit_key(unchanged.profile_id)
    )
    root = repository.next_search_window(
        job_id, _unit_key(unchanged.profile_id)
    )
    assert unchanged.id != original.id
    assert checkpoint.effective_lower_bound == long_idle_cursor
    assert root is not None
    assert (root.lower_bound, root.upper_bound) == (long_idle_cursor, upper)

    changed_terms = unchanged.search_terms + ("Long Idle New Term",)
    changed = DiscoveryProfileService(
        db, clock=lambda: upper - timedelta(hours=12)
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=unchanged.subject_id,
            seed_channel_ids=unchanged.seed_channel_ids,
            search_terms=changed_terms,
            reason="create a new synthetic search source",
        )
    )
    changed_upper = upper + timedelta(hours=1)
    changed_job_id = YouTubeSyncService(db).request_full_sync(
        changed_upper
    ).job_id
    changed_checkpoint = repository.get_youtube_sync_checkpoint(
        changed_job_id, _unit_key(changed.profile_id)
    )
    assert changed_checkpoint.source_key != source_key
    assert changed_checkpoint.effective_lower_bound == datetime(
        2023, 1, 1, 1, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "corruption",
    ("after_upper_bound", "malformed_bound", "hash", "owner_hash"),
)
def test_existing_cursor_still_fails_closed_on_invalid_bound_hash_or_owner(
    db, corruption
):
    profile = _search_profile(db)
    upper = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    cursor_bound = (
        upper + timedelta(seconds=1)
        if corruption == "after_upper_bound"
        else datetime(2022, 1, 1, tzinfo=timezone.utc)
    )
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=cursor_bound,
    )
    if corruption == "malformed_bound":
        db.execute(
            "UPDATE youtube_source_cursors SET completed_upper_bound=? "
            "WHERE profile_id=? AND source_kind=? AND source_key=?",
            (
                "2022-01-01T00:00:00Z",
                profile.profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
            ),
        )
    elif corruption == "hash":
        db.execute(
            "UPDATE youtube_source_cursors SET cursor_hash=? "
            "WHERE profile_id=? AND source_kind=? AND source_key=?",
            (
                "f" * 64,
                profile.profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
            ),
        )
    elif corruption == "owner_hash":
        foreign_profile = next(
            item
            for item in DiscoveryRepository(db).list_active_profile_versions()
            if item.profile_id != profile.profile_id
        )
        owner_hash = _canonical_hash({
            "completed_upper_bound": utc_iso(cursor_bound),
            "profile_id": foreign_profile.profile_id,
            "schema": "youtube-source-cursor.v1",
            "source_key": source_key,
            "source_kind": DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
        })
        db.execute(
            "UPDATE youtube_source_cursors SET cursor_hash=? "
            "WHERE profile_id=? AND source_kind=? AND source_key=?",
            (
                owner_hash,
                profile.profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
            ),
        )

    with pytest.raises(DomainError) as caught:
        YouTubeSyncService(db).request_full_sync(upper)

    assert caught.value.code == "STORED_YOUTUBE_SOURCE_CURSOR_INVALID"


def test_cursor_equal_to_upper_completes_empty_increment_without_provider_calls(db):
    profile = _search_profile(db)
    upper = datetime(2026, 1, 1, tzinfo=timezone.utc)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=upper,
    )
    with transaction(db):
        db.execute(
            "UPDATE discovery_profiles SET is_active=0 WHERE id<>?",
            (profile.profile_id,),
        )
    durable_before = tuple(
        dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")
    )
    client = FakeYouTubeClient()
    service = YouTubeSyncService(db, clock=lambda: upper, youtube_client=client)

    request = service.request_full_sync(upper)
    unit_key = _unit_key(profile.profile_id)
    JobStateService(db, clock=lambda: upper).begin_unit(request.job_id, unit_key)
    result = service.execute_search_unit(request.job_id, unit_key)

    expected_output_hash = _canonical_hash({
        "completed_upper_bound": utc_iso(upper),
        "persisted_observation_ids": [],
        "profile_version_id": profile.id,
        "schema": "youtube-search-unit-output.v1",
        "source_key": source_key,
    })
    assert (
        result.discovered_count,
        result.persisted_count,
        result.unavailable_count,
        result.output_hash,
    ) == (0, 0, 0, expected_output_hash)
    assert JobStateService(db).unit(
        request.job_id, unit_key
    ).status is UnitStatus.SUCCESS
    root = db.execute(
        "SELECT * FROM youtube_search_windows WHERE job_id=? AND unit_key=?",
        (request.job_id, unit_key),
    ).fetchone()
    assert root["lower_bound"] == root["upper_bound"] == utc_iso(upper)
    assert root["page_count"] == 0
    assert root["completed_at"] is not None
    proposal = db.execute(
        "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (request.job_id,),
    ).fetchone()
    assert proposal is not None
    assert (
        proposal["profile_id"],
        proposal["source_kind"],
        proposal["source_key"],
        proposal["completed_upper_bound"],
    ) == (
        profile.profile_id,
        DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
        source_key,
        utc_iso(upper),
    )
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (request.job_id,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations WHERE job_id=?",
        (request.job_id,),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_quota_reservations WHERE job_id=?",
        (request.job_id,),
    ).fetchone()[0] == 0
    assert (
        client.channel_calls,
        client.playlist_calls,
        client.search_calls,
        client.video_calls,
    ) == ([], [], [], [])
    assert tuple(
        dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")
    ) == durable_before

    replay_client = FakeYouTubeClient()
    replay = YouTubeSyncService(
        db, clock=lambda: upper, youtube_client=replay_client
    ).execute_search_unit(request.job_id, unit_key)
    assert replay == result
    assert (
        replay_client.channel_calls,
        replay_client.playlist_calls,
        replay_client.search_calls,
        replay_client.video_calls,
    ) == ([], [], [], [])

    service.finalize_full_job(request.job_id)

    assert JobStateService(db).status(request.job_id) is JobStatus.SUCCEEDED
    assert tuple(
        dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")
    ) == durable_before


def test_manual_manifest_never_reads_or_writes_source_cursors(db):
    profile = _search_profile(db)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=datetime(2025, 1, 1, tzinfo=timezone.utc),
        valid_hash=False,
    )
    with transaction(db):
        request_id = db.execute(
            "INSERT INTO manual_discovery_requests("
            "profile_id, youtube_video_id, requested_at) VALUES (?, ?, ?)",
            (profile.profile_id, "manual00001", utc_iso(RUN_UPPER)),
        ).lastrowid
    before = tuple(db.execute("SELECT * FROM youtube_source_cursors"))

    result = YouTubeSyncService(db).request_manual_sync(request_id, RUN_UPPER)

    checkpoint = DiscoveryRepository(db).get_youtube_sync_checkpoint(
        result.job_id, f"youtube:manual-request:{request_id}"
    )
    assert checkpoint.effective_lower_bound == checkpoint.upper_bound == RUN_UPPER
    assert tuple(db.execute("SELECT * FROM youtube_source_cursors")) == before
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_search_windows WHERE job_id=?",
        (result.job_id,),
    ).fetchone()[0] == 0


def test_search_uses_only_ordered_terms_provider_overlap_and_local_half_open_bounds(db):
    profile = _search_profile(db, terms=("千竈鉄平", "千竃鉄平"))
    below_id, lower_id, inside_id, upper_id = tuple(_video_id(i) for i in range(4))
    client = FakeYouTubeClient(
        search_responses=(
            _search_item_page((below_id, lower_id, inside_id, upper_id)),
        ),
        video_responses=((
            synthetic_video_item(
                video_id=below_id,
                snippet_published_at="2023-01-10T23:59:59Z",
            ),
            synthetic_video_item(
                video_id=lower_id,
                snippet_published_at="2023-01-11T00:00:00Z",
            ),
            synthetic_video_item(
                video_id=inside_id,
                snippet_published_at="2026-01-10T23:59:59Z",
            ),
            synthetic_video_item(
                video_id=upper_id,
                snippet_published_at="2026-01-11T00:00:00Z",
            ),
        ),),
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)

    result = service.execute_search_unit(job_id, unit_key)

    assert result.discovered_count == 4
    assert result.persisted_count == 2
    assert client.search_calls == [(
        "千竈鉄平|千竃鉄平",
        "2023-01-10T23:59:59.000000Z",
        "2026-01-11T00:00:00.000000Z",
        None,
    )]
    observed = tuple(
        row[0]
        for row in db.execute(
            "SELECT video.youtube_video_id FROM discovery_observations AS observation "
            "JOIN videos AS video ON video.id=observation.video_id "
            "WHERE observation.job_id=? ORDER BY video.youtube_video_id",
            (job_id,),
        )
    )
    assert observed == tuple(sorted((lower_id, inside_id)))
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (job_id,),
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM youtube_source_cursors").fetchone()[0] == 0


def test_ten_page_window_splits_at_day_boundary_newer_first_without_new_job_units(db):
    profile = _search_profile(db)
    root_ids = tuple(_video_id(index + 100) for index in range(10))
    older_id, newer_id = _video_id(110), _video_id(111)
    root_pages = tuple(
        _search_item_page((video_id,), f"root_token_{index + 1}")
        for index, video_id in enumerate(root_ids)
    )
    client = FakeYouTubeClient(
        search_responses=root_pages + (
            _search_item_page((older_id, newer_id)),
            _search_item_page((older_id, newer_id)),
        ),
        video_responses=tuple(
            (
                synthetic_video_item(
                    video_id=video_id,
                    snippet_published_at="2025-01-01T00:00:00Z",
                ),
            )
            for video_id in root_ids
        ) + (
            (
                synthetic_video_item(
                    video_id=older_id,
                    snippet_published_at="2024-07-11T23:59:59Z",
                ),
                synthetic_video_item(
                    video_id=newer_id,
                    snippet_published_at="2024-07-12T00:00:00Z",
                ),
            ),
            (
                synthetic_video_item(
                    video_id=older_id,
                    snippet_published_at="2024-07-11T23:59:59Z",
                ),
            ),
        ),
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)
    unit_count_before = db.execute(
        "SELECT COUNT(*) FROM job_units WHERE job_id=?", (job_id,)
    ).fetchone()[0]

    result = service.execute_search_unit(job_id, unit_key)

    windows = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_search_windows "
            "WHERE job_id=? AND unit_key=? ORDER BY ordinal",
            (job_id, unit_key),
        )
    )
    assert len(windows) == 3
    parent, newer, older = windows
    assert parent["page_count"] == 10
    assert parent["completed_at"] is not None
    assert newer["split_parent_id"] == older["split_parent_id"] == parent["id"]
    assert newer["lower_bound"] == older["upper_bound"] == utc_iso(SPLIT_BOUNDARY)
    assert older["lower_bound"] == utc_iso(RUN_FLOOR)
    assert newer["upper_bound"] == utc_iso(RUN_UPPER)
    assert client.search_calls[10][1] == "2024-07-11T23:59:59.000000Z"
    assert client.search_calls[11][1] == "2023-01-10T23:59:59.000000Z"
    assert client.search_calls[10][3] is None
    assert client.search_calls[11][3] is None
    assert result.discovered_count == 12
    assert result.persisted_count == 12
    assert db.execute(
        "SELECT COUNT(*) FROM job_units WHERE job_id=?", (job_id,)
    ).fetchone()[0] == unit_count_before
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations WHERE job_id=?",
        (job_id,),
    ).fetchone()[0] == 12


@pytest.mark.parametrize(
    ("lower", "upper", "expected"),
    (
        (
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 4, tzinfo=timezone.utc),
            datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 3, 23, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
            datetime(2026, 1, 3, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 3, 1, tzinfo=timezone.utc),
            None,
        ),
    ),
)
def test_split_boundary_checks_midnight_before_then_after_midpoint(
    lower, upper, expected
):
    window = discovery_domain.SearchWindow(
        id=1,
        job_id=1,
        unit_key="youtube:profile:1:search",
        ordinal=1,
        lower_bound=lower,
        upper_bound=upper,
        next_page_token="saturated_token",
        page_count=10,
        split_parent_id=None,
        completed_at=None,
        window_hash="a" * 64,
    )

    assert YouTubeSyncService._search_split_boundary(window) == expected


def test_asymmetric_window_uses_adjacent_midnight_and_preserves_full_cover(db):
    profile = _search_profile(db)
    lower = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    upper = datetime(2026, 1, 4, tzinfo=timezone.utc)
    boundary = datetime(2026, 1, 3, tzinfo=timezone.utc)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=lower,
    )
    root_pages = tuple(
        YouTubePage((), f"asymmetric_token_{index}") for index in range(10)
    )
    client = FakeYouTubeClient(
        search_responses=root_pages + (
            YouTubePage((), None),
            YouTubePage((), None),
        )
    )
    service = YouTubeSyncService(db, clock=lambda: upper, youtube_client=client)
    job_id, unit_key = _start_search_unit(db, service, profile, upper)
    unit_count = db.execute(
        "SELECT COUNT(*) FROM job_units WHERE job_id=?", (job_id,)
    ).fetchone()[0]

    result = service.execute_search_unit(job_id, unit_key)

    windows = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_search_windows WHERE job_id=? "
            "AND unit_key=? ORDER BY ordinal",
            (job_id, unit_key),
        )
    )
    assert len(windows) == 3
    parent, newer, older = windows
    assert parent["page_count"] == 10
    assert newer["split_parent_id"] == older["split_parent_id"] == parent["id"]
    assert older["lower_bound"] == utc_iso(lower)
    assert newer["lower_bound"] == older["upper_bound"] == utc_iso(boundary)
    assert newer["upper_bound"] == utc_iso(upper)
    assert client.search_calls[10][1] == "2026-01-02T23:59:59.000000Z"
    assert client.search_calls[11][1] == "2026-01-01T00:59:59.000000Z"
    assert result.discovered_count == result.persisted_count == 0
    assert db.execute(
        "SELECT COUNT(*) FROM job_units WHERE job_id=?", (job_id,)
    ).fetchone()[0] == unit_count


def test_invalid_token_restarts_only_current_window_and_replay_is_idempotent(db):
    profile = _search_profile(db)
    first_id, second_id = _video_id(200), _video_id(201)
    client = FakeYouTubeClient(
        search_responses=(
            _search_item_page((first_id,), "stale_private_token"),
            YouTubeProviderFailure(
                "YOUTUBE_PAGE_TOKEN_INVALID", "invalid_page_token"
            ),
            _search_item_page((first_id,), "fresh_private_token"),
            _search_item_page((second_id,)),
        ),
        video_responses=(
            (
                synthetic_video_item(
                    video_id=first_id,
                    snippet_published_at="2025-01-01T00:00:00Z",
                ),
            ),
            (
                synthetic_video_item(
                    video_id=second_id,
                    snippet_published_at="2025-01-01T00:00:00Z",
                ),
            ),
        ),
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)

    result = service.execute_search_unit(job_id, unit_key)

    assert tuple(call[3] for call in client.search_calls) == (
        None,
        "stale_private_token",
        None,
        "fresh_private_token",
    )
    window = db.execute(
        "SELECT * FROM youtube_search_windows WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    checkpoint = db.execute(
        "SELECT * FROM youtube_sync_checkpoints WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    assert window["page_count"] == 2
    assert window["next_page_token"] is None
    assert checkpoint["batch_ordinal"] == 3
    assert result.discovered_count == result.persisted_count == 2
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations WHERE job_id=?",
        (job_id,),
    ).fetchone()[0] == 2


def test_one_day_page_ten_checkpoint_resumes_from_durable_token(db):
    profile = _search_profile(db)
    lower = RUN_UPPER - timedelta(days=1)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=lower,
    )
    before = tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors"))
    client = FakeYouTubeClient(search_responses=(YouTubePage((), None),))
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)
    _checkpoint_ten_search_pages(db, job_id, unit_key)
    resumed_service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )

    result = resumed_service.execute_search_unit(job_id, unit_key)

    assert tuple(call[3] for call in client.search_calls) == ("durable_token_10",)
    assert result.discovered_count == result.persisted_count == 0
    window = db.execute(
        "SELECT page_count, next_page_token, completed_at "
        "FROM youtube_search_windows WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    assert tuple(window) == (11, None, utc_iso(RUN_UPPER))
    assert JobStateService(db).unit(job_id, unit_key).status is UnitStatus.SUCCESS
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (job_id,),
    ).fetchone()[0] == 1
    assert tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")) == before


def test_preexisting_splittable_page_ten_splits_before_provider_call(db):
    profile = _search_profile(db)
    client = FakeYouTubeClient(
        search_responses=(YouTubePage((), None), YouTubePage((), None))
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)
    _checkpoint_ten_search_pages(db, job_id, unit_key)

    result = service.execute_search_unit(job_id, unit_key)

    windows = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_search_windows WHERE job_id=? "
            "AND unit_key=? ORDER BY ordinal",
            (job_id, unit_key),
        )
    )
    assert len(windows) == 3
    parent, newer, older = windows
    assert parent["page_count"] == 10
    assert newer["split_parent_id"] == older["split_parent_id"] == parent["id"]
    assert client.search_calls == [
        (
            "|".join(profile.search_terms),
            "2024-07-11T23:59:59.000000Z",
            utc_iso(RUN_UPPER),
            None,
        ),
        (
            "|".join(profile.search_terms),
            "2023-01-10T23:59:59.000000Z",
            utc_iso(SPLIT_BOUNDARY),
            None,
        ),
    ]
    assert result.discovered_count == result.persisted_count == 0


def test_one_day_leaf_continues_page_eleven_before_proposing_cursor(db):
    profile = _search_profile(db)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    lower = RUN_UPPER - timedelta(days=1)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=lower,
    )
    before = tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors"))
    client = FakeYouTubeClient(
        search_responses=tuple(
            YouTubePage((), f"saturated_token_{index}")
            for index in range(1, 11)
        )
        + (YouTubePage((), None),)
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)

    result = service.execute_search_unit(job_id, unit_key)

    assert result.discovered_count == result.persisted_count == 0
    assert JobStateService(db).unit(job_id, unit_key).status is UnitStatus.SUCCESS
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (job_id,),
    ).fetchone()[0] == 1
    assert tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")) == before
    assert tuple(call[3] for call in client.search_calls) == (
        None,
        "saturated_token_1",
        "saturated_token_2",
        "saturated_token_3",
        "saturated_token_4",
        "saturated_token_5",
        "saturated_token_6",
        "saturated_token_7",
        "saturated_token_8",
        "saturated_token_9",
        "saturated_token_10",
    )


def test_invalid_page_eleven_token_restarts_one_day_leaf_without_cursor_promotion(db):
    profile = _search_profile(db)
    lower = RUN_UPPER - timedelta(days=1)
    source_key = discovery_domain.youtube_search_source_key(profile.search_terms)
    _insert_cursor(
        db,
        profile_id=profile.profile_id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key=source_key,
        completed_upper_bound=lower,
    )
    before = tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors"))
    client = FakeYouTubeClient(
        search_responses=(
            YouTubeProviderFailure(
                "YOUTUBE_PAGE_TOKEN_INVALID", "invalid_page_token"
            ),
            YouTubePage((), None),
        )
    )
    service = YouTubeSyncService(
        db, clock=lambda: RUN_UPPER, youtube_client=client
    )
    job_id, unit_key = _start_search_unit(db, service, profile)
    _checkpoint_ten_search_pages(db, job_id, unit_key)

    result = service.execute_search_unit(job_id, unit_key)

    assert tuple(call[3] for call in client.search_calls) == (
        "durable_token_10",
        None,
    )
    assert result.discovered_count == result.persisted_count == 0
    window = db.execute(
        "SELECT page_count, next_page_token, completed_at "
        "FROM youtube_search_windows WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    assert tuple(window) == (1, None, utc_iso(RUN_UPPER))
    checkpoint = db.execute(
        "SELECT batch_ordinal FROM youtube_sync_checkpoints "
        "WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    assert checkpoint["batch_ordinal"] == 11
    assert tuple(dict(row) for row in db.execute("SELECT * FROM youtube_source_cursors")) == before
