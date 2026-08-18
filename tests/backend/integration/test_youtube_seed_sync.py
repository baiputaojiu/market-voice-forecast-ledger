import hashlib
import json
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.discovery import DiscoverySourceKind
from market_voice_forecast_ledger.domain.enums import UnitStatus
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import ChannelUploads, YouTubePage
from market_voice_forecast_ledger.youtube.metadata import normalize_video_item
from tests.backend.youtube_fakes import (
    FakeYouTubeClient,
    synthetic_playlist_item,
    synthetic_video_item,
)


RUN_UPPER_BOUND = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
WITHIN_WINDOW = "2026-08-10T01:00:00Z"
BEFORE_WINDOW = "2022-08-10T01:00:00Z"
AT_UPPER_BOUND = "2026-08-19T03:04:05Z"


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _video_id(index: int) -> str:
    return f"seed{index:07d}"


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _seed_fixture(
    db,
    *,
    playlist_pages: tuple[YouTubePage, ...],
    video_responses: tuple[object, ...],
):
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    client = FakeYouTubeClient(
        channel_responses=((ChannelUploads(channel_id, playlist_id),),),
        playlist_responses=playlist_pages,
        video_responses=video_responses,
    )
    service = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=client,
    )
    request = service.request_full_sync(RUN_UPPER_BOUND)
    claimed = service.claim_next_runnable(RUN_UPPER_BOUND)
    assert claimed is not None and claimed.job_id == request.job_id
    running = tuple(
        db.execute(
            "SELECT unit_key FROM job_units "
            "WHERE job_id=? AND status='running'",
            (request.job_id,),
        )
    )
    assert len(running) == 1
    return service, client, profile, request.job_id, running[0]["unit_key"]


def _seed_observation_ids(db, job_id: int, profile_id: int, channel_id: str):
    return tuple(
        row["id"]
        for row in db.execute(
            "SELECT id FROM discovery_observations "
            "WHERE job_id=? AND profile_id=? AND source_kind='seed_uploads' "
            "AND source_key=? ORDER BY id",
            (job_id, profile_id, channel_id),
        )
    )


def test_seed_unit_persists_73_ids_in_exact_batches_without_name_filter(db):
    video_ids = tuple(_video_id(index) for index in range(73))
    unavailable_id = video_ids[36]
    prior_search_id = video_ids[10]
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    pages = (
        YouTubePage(
            tuple(
                synthetic_playlist_item(video_id, playlist_id)
                for video_id in video_ids[:50]
            ),
            "page_two_private",
        ),
        YouTubePage(
            tuple(
                synthetic_playlist_item(video_id, playlist_id)
                for video_id in video_ids[50:]
            ),
            None,
        ),
    )
    first_items = tuple(
        synthetic_video_item(
            video_id=video_id,
            title=(
                "A title with no person name"
                if video_id == video_ids[5]
                else f"Synthetic upload {video_id}"
            ),
            snippet_published_at=WITHIN_WINDOW,
        )
        for video_id in video_ids[:50]
        if video_id != unavailable_id
    )
    second_items = tuple(
        synthetic_video_item(
            video_id=video_id,
            title=f"Synthetic upload {video_id}",
            snippet_published_at=WITHIN_WINDOW,
        )
        for video_id in video_ids[50:]
    )
    service, client, profile, job_id, unit_key = _seed_fixture(
        db,
        playlist_pages=pages,
        video_responses=(first_items, second_items),
    )

    prior_metadata = normalize_video_item(
        synthetic_video_item(
            video_id=prior_search_id,
            title=f"Synthetic upload {prior_search_id}",
            snippet_published_at=WITHIN_WINDOW,
        ),
        fetched_at=RUN_UPPER_BOUND,
    )
    with transaction(db):
        prior = DiscoveryRepository(db).persist_metadata_batch(
            job_id,
            profile.id,
            DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            "previous-search-observation",
            (prior_metadata,),
            RUN_UPPER_BOUND,
        )

    result = service.execute_seed_unit(job_id, unit_key)

    assert result.discovered_count == 73
    assert result.persisted_count == 72
    assert result.unavailable_count == 1
    assert tuple(len(call) for call in client.video_calls) == (50, 23)
    assert client.video_calls[0] == video_ids[:50]
    assert client.video_calls[1] == video_ids[50:]
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 72
    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_candidates"
    ).fetchone()[0] == 72
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 73

    prior_video = db.execute(
        "SELECT id FROM videos WHERE youtube_video_id=?", (prior_search_id,)
    ).fetchone()
    candidate_rows = tuple(
        db.execute(
            "SELECT id FROM subject_video_candidates "
            "WHERE profile_id=? AND video_id=?",
            (profile.profile_id, prior_video["id"]),
        )
    )
    observation_rows = tuple(
        db.execute(
            "SELECT id, source_kind FROM discovery_observations "
            "WHERE profile_id=? AND video_id=? ORDER BY id",
            (profile.profile_id, prior_video["id"]),
        )
    )
    assert len(candidate_rows) == 1
    assert tuple(row["source_kind"] for row in observation_rows) == (
        "cross_channel_search",
        "seed_uploads",
    )
    assert prior.observation_ids == (observation_rows[0]["id"],)

    no_name = db.execute(
        "SELECT title FROM video_metadata_snapshots WHERE youtube_video_id=?",
        (video_ids[5],),
    ).fetchone()
    assert no_name["title"] == "A title with no person name"
    assert db.execute(
        "SELECT COUNT(*) FROM videos WHERE youtube_video_id=?",
        (unavailable_id,),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_metadata_snapshots WHERE youtube_video_id=?",
        (unavailable_id,),
    ).fetchone()[0] == 0

    observation_ids = _seed_observation_ids(
        db, job_id, profile.profile_id, channel_id
    )
    expected_output = _hash(
        {
            "completed_upper_bound": _utc_text(RUN_UPPER_BOUND),
            "persisted_observation_ids": list(observation_ids),
            "profile_version_id": profile.id,
            "schema": "youtube-seed-unit-output.v1",
            "source_key": channel_id,
        }
    )
    assert result.output_hash == expected_output
    unit = JobStateService(db).unit(job_id, unit_key)
    assert unit.status is UnitStatus.SUCCESS
    assert unit.output_hash == expected_output
    proposal = db.execute(
        "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=?",
        (job_id,),
    ).fetchone()
    assert dict(proposal) == {
        "job_id": job_id,
        "profile_id": profile.profile_id,
        "source_kind": "seed_uploads",
        "source_key": channel_id,
        "completed_upper_bound": _utc_text(RUN_UPPER_BOUND),
        "cursor_hash": _hash(
            {
                "completed_upper_bound": _utc_text(RUN_UPPER_BOUND),
                "profile_id": profile.profile_id,
                "schema": "youtube-source-cursor.v1",
                "source_key": channel_id,
                "source_kind": "seed_uploads",
            }
        ),
    }
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_source_cursors"
    ).fetchone()[0] == 0


def test_seed_unit_keeps_only_the_sealed_half_open_time_window(db):
    video_ids = (_video_id(100), _video_id(101), _video_id(102))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    page = YouTubePage(
        tuple(synthetic_playlist_item(item, playlist_id) for item in video_ids),
        None,
    )
    items = (
        synthetic_video_item(
            video_id=video_ids[0], snippet_published_at=BEFORE_WINDOW
        ),
        synthetic_video_item(
            video_id=video_ids[1], snippet_published_at=WITHIN_WINDOW
        ),
        synthetic_video_item(
            video_id=video_ids[2], snippet_published_at=AT_UPPER_BOUND
        ),
    )
    service, _, _, job_id, unit_key = _seed_fixture(
        db, playlist_pages=(page,), video_responses=(items,)
    )

    result = service.execute_seed_unit(job_id, unit_key)

    stored = tuple(
        row["youtube_video_id"]
        for row in db.execute("SELECT youtube_video_id FROM videos ORDER BY id")
    )
    assert stored == (video_ids[1],)
    assert result.discovered_count == 3
    assert result.persisted_count == 1
    assert result.unavailable_count == 0


def test_seed_unit_stops_when_a_page_is_wholly_older_than_the_lower_bound(db):
    video_ids = (_video_id(110), _video_id(111))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    page = YouTubePage(
        tuple(synthetic_playlist_item(item, playlist_id) for item in video_ids),
        "must_not_be_requested",
    )
    items = tuple(
        synthetic_video_item(
            video_id=item, snippet_published_at=BEFORE_WINDOW
        )
        for item in video_ids
    )
    service, client, _, job_id, unit_key = _seed_fixture(
        db, playlist_pages=(page,), video_responses=(items,)
    )

    result = service.execute_seed_unit(job_id, unit_key)

    assert result.discovered_count == 2
    assert result.persisted_count == 0
    assert result.unavailable_count == 0
    assert client.playlist_calls == [(playlist_id, None)]
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_unavailable_page_identity_prevents_wholly_old_early_stop(db):
    old_id = _video_id(115)
    unavailable_id = _video_id(116)
    eligible_id = _video_id(117)
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    pages = (
        YouTubePage(
            (
                synthetic_playlist_item(old_id, playlist_id),
                synthetic_playlist_item(unavailable_id, playlist_id),
            ),
            "page_after_ambiguous_old",
        ),
        YouTubePage(
            (synthetic_playlist_item(eligible_id, playlist_id),),
            None,
        ),
    )
    responses = (
        (
            synthetic_video_item(
                video_id=old_id,
                snippet_published_at=BEFORE_WINDOW,
            ),
        ),
        (
            synthetic_video_item(
                video_id=eligible_id,
                snippet_published_at=WITHIN_WINDOW,
            ),
        ),
    )
    service, client, _, job_id, unit_key = _seed_fixture(
        db,
        playlist_pages=pages,
        video_responses=responses,
    )

    result = service.execute_seed_unit(job_id, unit_key)

    assert client.playlist_calls == [
        (playlist_id, None),
        (playlist_id, "page_after_ambiguous_old"),
    ]
    assert client.video_calls == [
        (old_id, unavailable_id),
        (eligible_id,),
    ]
    assert result.discovered_count == 3
    assert result.persisted_count == 1
    assert result.unavailable_count == 1
    assert tuple(
        row["youtube_video_id"]
        for row in db.execute("SELECT youtube_video_id FROM videos ORDER BY id")
    ) == (eligible_id,)


def test_seed_unit_deduplicates_across_pages_but_preserves_distinct_ids(db):
    first_id, repeated_id, last_id = (
        _video_id(120),
        _video_id(121),
        _video_id(122),
    )
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    pages = (
        YouTubePage(
            (
                synthetic_playlist_item(first_id, playlist_id),
                synthetic_playlist_item(repeated_id, playlist_id),
            ),
            "dedupe_page_two",
        ),
        YouTubePage(
            (
                synthetic_playlist_item(repeated_id, playlist_id),
                synthetic_playlist_item(last_id, playlist_id),
            ),
            None,
        ),
    )
    responses = (
        tuple(
            synthetic_video_item(
                video_id=item, snippet_published_at=WITHIN_WINDOW
            )
            for item in (first_id, repeated_id)
        ),
        (
            synthetic_video_item(
                video_id=last_id, snippet_published_at=WITHIN_WINDOW
            ),
        ),
    )
    service, client, _, job_id, unit_key = _seed_fixture(
        db, playlist_pages=pages, video_responses=responses
    )

    result = service.execute_seed_unit(job_id, unit_key)

    assert client.video_calls == [
        (first_id, repeated_id),
        (last_id,),
    ]
    assert result.discovered_count == 3
    assert result.persisted_count == 3
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 3
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 3
