import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    DiscoverySourceKind,
    youtube_search_source_key,
)
from market_voice_forecast_ledger.domain.enums import JobStage, JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import ChannelUploads, YouTubePage
from market_voice_forecast_ledger.youtube.metadata import normalize_video_item
from tests.backend.youtube_fakes import (
    CursorPromotionFailpoint,
    FakeYouTubeClient,
    empty_full_sync_client,
    synthetic_playlist_item,
    synthetic_video_item,
)


RUN_UPPER_BOUND = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
WITHIN_WINDOW = "2026-08-10T01:00:00Z"
AT_UPPER_BOUND = "2026-08-19T03:04:05Z"


class SyntheticSeedFault(RuntimeError):
    pass


class SyntheticCursorPromotionFault(RuntimeError):
    pass


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


def _video_id(index: int) -> str:
    return f"atom{index:07d}"


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_service(
    db,
    *,
    playlist_responses: tuple[object, ...],
    video_responses: tuple[object, ...],
    channel_responses: tuple[object, ...] | None = None,
):
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    client = FakeYouTubeClient(
        channel_responses=(
            (
                (ChannelUploads(channel_id, playlist_id),),
                (ChannelUploads(channel_id, playlist_id),),
            )
            if channel_responses is None
            else channel_responses
        ),
        playlist_responses=playlist_responses,
        video_responses=video_responses,
    )
    service = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=client,
    )
    return service, client, profile, playlist_id


def _create_and_claim(db, service: YouTubeSyncService):
    request = service.request_full_sync(RUN_UPPER_BOUND)
    claimed = service.claim_next_runnable(RUN_UPPER_BOUND)
    assert claimed is not None and claimed.job_id == request.job_id
    unit_key = db.execute(
        "SELECT unit_key FROM job_units "
        "WHERE job_id=? AND status='running'",
        (request.job_id,),
    ).fetchone()["unit_key"]
    return request.job_id, unit_key


def _page(playlist_id: str, video_ids: tuple[str, ...], token=None):
    return YouTubePage(
        tuple(
            synthetic_playlist_item(video_id, playlist_id)
            for video_id in video_ids
        ),
        token,
    )


def _items(video_ids: tuple[str, ...], *, changed_first=False):
    return tuple(
        synthetic_video_item(
            video_id=video_id,
            title=(
                "Changed seed metadata"
                if changed_first and ordinal == 0
                else f"Atomic upload {video_id}"
            ),
            snippet_published_at=WITHIN_WINDOW,
        )
        for ordinal, video_id in enumerate(video_ids)
    )


@pytest.mark.parametrize(
    ("stored_playlist", "channel_response", "expected_code"),
    (
        (
            "UU0123456789abcdefghijkl",
            "authoritative",
            "YOUTUBE_SEED_CHECKPOINT_INVALID",
        ),
        ("sealed", (), "YOUTUBE_DISCOVERY_INVALID"),
        ("sealed", "multiple", "YOUTUBE_DISCOVERY_INVALID"),
    ),
)
def test_stored_playlist_binding_requires_authoritative_channel_resolution(
    db, stored_playlist, channel_response, expected_code
):
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    sealed_playlist_id = "UU" + channel_id[2:]
    stored_playlist_id = (
        sealed_playlist_id if stored_playlist == "sealed" else stored_playlist
    )
    if channel_response == "authoritative":
        channel_responses = (
            (ChannelUploads(channel_id, sealed_playlist_id),),
        )
    elif channel_response == "multiple":
        channel_responses = (
            (
                ChannelUploads(channel_id, sealed_playlist_id),
                ChannelUploads(channel_id, stored_playlist_id),
            ),
        )
    else:
        channel_responses = (channel_response,)
    service, client, _, _ = _build_service(
        db,
        channel_responses=channel_responses,
        playlist_responses=(YouTubePage((), None),),
        video_responses=(),
    )
    job_id, unit_key = _create_and_claim(db, service)
    with transaction(db):
        DiscoveryRepository(db).bind_seed_uploads_playlist(
            job_id=job_id,
            unit_key=unit_key,
            source_key=channel_id,
            uploads_playlist_id=stored_playlist_id,
        )
    checkpoint_before = dict(
        db.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
    )

    with pytest.raises(DomainError) as caught:
        service.execute_seed_unit(job_id, unit_key)

    assert caught.value.code == expected_code
    assert client.channel_calls == [(channel_id,)]
    assert client.playlist_calls == []
    assert dict(
        db.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
    ) == checkpoint_before
    state = _state(db, job_id, unit_key)
    assert state["unit_status"] is UnitStatus.RUNNING
    assert state["videos"] == 0
    assert state["snapshots"] == 0
    assert state["observations"] == 0
    assert state["candidates"] == 0
    assert state["proposals"] == 0
    assert state["durable_cursors"] == 0


def test_repository_playlist_binding_requires_the_sealed_source_owner(db):
    service, _, profile, playlist_id = _build_service(
        db,
        playlist_responses=(),
        video_responses=(),
    )
    job_id, unit_key = _create_and_claim(db, service)
    checkpoint_before = dict(
        db.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
    )

    with transaction(db):
        with pytest.raises(DomainError) as caught:
            DiscoveryRepository(db).bind_seed_uploads_playlist(
                job_id=job_id,
                unit_key=unit_key,
                source_key="UC0123456789abcdefghijkl",
                uploads_playlist_id=playlist_id,
            )

    assert caught.value.code == "YOUTUBE_SEED_CHECKPOINT_INVALID"
    assert profile.seed_channel_ids[0] != "UC0123456789abcdefghijkl"
    assert dict(
        db.execute(
            "SELECT * FROM youtube_sync_checkpoints "
            "WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()
    ) == checkpoint_before


def _state(db, job_id: int, unit_key: str):
    checkpoint = db.execute(
        "SELECT * FROM youtube_sync_checkpoints "
        "WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    unit = JobStateService(db).unit(job_id, unit_key)
    return {
        "checkpoint": dict(checkpoint),
        "unit_status": unit.status,
        "unit_output": unit.output_hash,
        "videos": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
        "snapshots": db.execute(
            "SELECT COUNT(*) FROM video_metadata_snapshots"
        ).fetchone()[0],
        "observations": db.execute(
            "SELECT COUNT(*) FROM discovery_observations"
        ).fetchone()[0],
        "candidates": db.execute(
            "SELECT COUNT(*) FROM subject_video_candidates"
        ).fetchone()[0],
        "proposals": db.execute(
            "SELECT COUNT(*) FROM youtube_sync_proposed_cursors"
        ).fetchone()[0],
        "durable_cursors": db.execute(
            "SELECT COUNT(*) FROM youtube_source_cursors"
        ).fetchone()[0],
    }


def test_item_37_failure_rolls_back_the_whole_batch_and_pointer(db):
    video_ids = tuple(_video_id(index) for index in range(50))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    service, client, profile, playlist_id = _build_service(
        db,
        playlist_responses=(
            _page(playlist_id, video_ids),
            _page(playlist_id, video_ids),
        ),
        video_responses=(_items(video_ids, changed_first=True),) * 2,
    )
    job_id, unit_key = _create_and_claim(db, service)
    original = normalize_video_item(
        synthetic_video_item(
            video_id=video_ids[0],
            title="Original search metadata",
            snippet_published_at=WITHIN_WINDOW,
        ),
        fetched_at=RUN_UPPER_BOUND,
    )
    with transaction(db):
        DiscoveryRepository(db).persist_metadata_batch(
            job_id,
            profile.id,
            DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            "atomic-search-sentinel",
            (original,),
            RUN_UPPER_BOUND,
        )
    pointer_before = db.execute(
        "SELECT current_metadata_snapshot_id FROM videos "
        "WHERE youtube_video_id=?",
        (video_ids[0],),
    ).fetchone()[0]
    db.execute(
        "CREATE TEMP TRIGGER fail_seed_item_37 "
        "BEFORE INSERT ON video_metadata_snapshots "
        f"WHEN NEW.youtube_video_id='{video_ids[36]}' "
        "BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_ITEM_37'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="SYNTHETIC_ITEM_37"):
        service.execute_seed_unit(job_id, unit_key)

    after_failure = _state(db, job_id, unit_key)
    assert after_failure["checkpoint"]["page_count"] == 0
    assert after_failure["checkpoint"]["batch_ordinal"] == 0
    assert after_failure["unit_status"] is UnitStatus.RUNNING
    assert after_failure["observations"] == 1
    assert after_failure["videos"] == 1
    assert after_failure["snapshots"] == 1
    assert after_failure["candidates"] == 1
    assert after_failure["proposals"] == 0
    assert after_failure["durable_cursors"] == 0
    assert db.execute(
        "SELECT current_metadata_snapshot_id FROM videos "
        "WHERE youtube_video_id=?",
        (video_ids[0],),
    ).fetchone()[0] == pointer_before

    db.execute("DROP TRIGGER fail_seed_item_37")
    result = service.execute_seed_unit(job_id, unit_key)

    assert result.persisted_count == 50
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations "
        "WHERE source_kind='seed_uploads'",
    ).fetchone()[0] == 50
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 50
    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_candidates"
    ).fetchone()[0] == 50
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_source_cursors"
    ).fetchone()[0] == 0
    assert len(client.channel_calls) == 2
    assert client.playlist_calls == [(playlist_id, None), (playlist_id, None)]


def test_failure_after_persistence_before_checkpoint_rolls_back_and_replays(db):
    video_ids = tuple(_video_id(index + 100) for index in range(50))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    service, client, _, _ = _build_service(
        db,
        playlist_responses=(
            _page(playlist_id, video_ids),
            _page(playlist_id, video_ids),
        ),
        video_responses=(_items(video_ids), _items(video_ids)),
    )
    job_id, unit_key = _create_and_claim(db, service)
    original = service._discovery.persist_metadata_batch

    def fail_after_persistence(*args, **kwargs):
        original(*args, **kwargs)
        raise SyntheticSeedFault("after persistence before checkpoint")

    service._discovery.persist_metadata_batch = fail_after_persistence
    with pytest.raises(SyntheticSeedFault, match="before checkpoint"):
        service.execute_seed_unit(job_id, unit_key)

    failed = _state(db, job_id, unit_key)
    assert failed["videos"] == 0
    assert failed["snapshots"] == 0
    assert failed["observations"] == 0
    assert failed["candidates"] == 0
    assert failed["checkpoint"]["page_count"] == 0
    assert failed["checkpoint"]["uploads_playlist_id"] == playlist_id
    assert failed["proposals"] == 0
    assert failed["durable_cursors"] == 0

    service._discovery.persist_metadata_batch = original
    result = service.execute_seed_unit(job_id, unit_key)

    assert result.persisted_count == 50
    assert _state(db, job_id, unit_key)["observations"] == 50
    assert len(client.channel_calls) == 2
    assert client.playlist_calls == [(playlist_id, None), (playlist_id, None)]


@pytest.mark.parametrize(
    "fault_position",
    ("after_checkpoint_before_completion", "after_completion_before_finalization"),
)
def test_final_transaction_fault_replays_without_duplicate_observations(
    db, fault_position
):
    video_ids = tuple(_video_id(index + 200) for index in range(50))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    service, client, _, _ = _build_service(
        db,
        playlist_responses=(_page(playlist_id, video_ids),),
        video_responses=(_items(video_ids),),
    )
    job_id, unit_key = _create_and_claim(db, service)
    original = service._job_state.complete_unit_in_transaction

    def fail_at_completion(*args, **kwargs):
        if fault_position == "after_completion_before_finalization":
            original(*args, **kwargs)
        raise SyntheticSeedFault(fault_position)

    service._job_state.complete_unit_in_transaction = fail_at_completion
    with pytest.raises(SyntheticSeedFault, match=fault_position):
        service.execute_seed_unit(job_id, unit_key)

    failed = _state(db, job_id, unit_key)
    assert failed["videos"] == 50
    assert failed["snapshots"] == 50
    assert failed["observations"] == 50
    assert failed["candidates"] == 50
    assert failed["checkpoint"]["page_count"] == 1
    assert failed["checkpoint"]["batch_ordinal"] == 1
    assert failed["checkpoint"]["completed_at"] is None
    assert failed["unit_status"] is UnitStatus.RUNNING
    assert failed["unit_output"] is None
    assert failed["proposals"] == 0
    assert failed["durable_cursors"] == 0

    service._job_state.complete_unit_in_transaction = original
    result = service.execute_seed_unit(job_id, unit_key)

    completed = _state(db, job_id, unit_key)
    assert completed["unit_status"] is UnitStatus.SUCCESS
    assert completed["unit_output"] == result.output_hash
    assert completed["observations"] == 50
    assert completed["proposals"] == 1
    assert completed["durable_cursors"] == 0
    assert client.playlist_calls == [(playlist_id, None)]
    assert client.video_calls == [video_ids]


def test_private_page_token_is_checkpointed_only_and_resume_uses_it(db):
    first_ids = (_video_id(300), _video_id(301))
    second_ids = (_video_id(302),)
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    private_token = "private_page_token"
    service, _, _, _ = _build_service(
        db,
        playlist_responses=(
            _page(playlist_id, first_ids, private_token),
            SyntheticSeedFault("interrupt after committed page"),
        ),
        video_responses=(_items(first_ids),),
    )
    job_id, unit_key = _create_and_claim(db, service)

    with pytest.raises(SyntheticSeedFault, match="committed page"):
        service.execute_seed_unit(job_id, unit_key)

    checkpoint = _state(db, job_id, unit_key)["checkpoint"]
    assert checkpoint["next_page_token"] == private_token
    assert checkpoint["page_count"] == 1
    publicish_rows = tuple(
        row[0]
        for row in db.execute(
            "SELECT metadata_json FROM job_events WHERE job_id=?", (job_id,)
        )
    )
    assert all(private_token not in payload for payload in publicish_rows)

    resumed_client = FakeYouTubeClient(
        channel_responses=((ChannelUploads(channel_id, playlist_id),),),
        playlist_responses=(_page(playlist_id, second_ids),),
        video_responses=(_items(second_ids),),
    )
    resumed = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=resumed_client,
    )
    result = resumed.execute_seed_unit(job_id, unit_key)

    assert result.persisted_count == 3
    assert resumed_client.channel_calls == [(channel_id,)]
    assert resumed_client.playlist_calls == [(playlist_id, private_token)]
    assert _state(db, job_id, unit_key)["checkpoint"]["next_page_token"] is None
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 3


def test_resume_and_success_replay_preserve_canonical_cumulative_counts(
    db, db_path
):
    first_id = _video_id(500)
    unavailable_id = _video_id(501)
    outside_id = _video_id(502)
    final_id = _video_id(503)
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    channel_id = profile.seed_channel_ids[0]
    playlist_id = "UU" + channel_id[2:]
    private_token = "resume_cumulative_counts"
    first_page = _page(
        playlist_id,
        (first_id, unavailable_id, outside_id),
        private_token,
    )
    final_page = _page(
        playlist_id,
        (unavailable_id, outside_id, final_id),
    )
    first_response = (
        synthetic_video_item(
            video_id=first_id,
            snippet_published_at=WITHIN_WINDOW,
        ),
        synthetic_video_item(
            video_id=outside_id,
            snippet_published_at=AT_UPPER_BOUND,
        ),
    )
    final_response = (
        synthetic_video_item(
            video_id=final_id,
            snippet_published_at=WITHIN_WINDOW,
        ),
    )

    uninterrupted_db = open_database(
        db_path.with_name("uninterrupted-ledger.sqlite3")
    )
    apply_migrations(uninterrupted_db)
    bootstrap_reference_data(uninterrupted_db)
    try:
        uninterrupted_service, uninterrupted_client, _, _ = _build_service(
            uninterrupted_db,
            playlist_responses=(first_page, final_page),
            video_responses=(first_response, final_response),
        )
        uninterrupted_job, uninterrupted_unit = _create_and_claim(
            uninterrupted_db, uninterrupted_service
        )
        uninterrupted_result = uninterrupted_service.execute_seed_unit(
            uninterrupted_job, uninterrupted_unit
        )
        assert uninterrupted_client.video_calls == [
            (first_id, unavailable_id, outside_id),
            (final_id,),
        ]
    finally:
        uninterrupted_db.close()

    interrupted_service, _, _, _ = _build_service(
        db,
        playlist_responses=(
            first_page,
            SyntheticSeedFault("interrupt after cumulative page commit"),
        ),
        video_responses=(first_response,),
    )
    job_id, unit_key = _create_and_claim(db, interrupted_service)
    with pytest.raises(SyntheticSeedFault, match="cumulative page commit"):
        interrupted_service.execute_seed_unit(job_id, unit_key)

    resumed_client = FakeYouTubeClient(
        channel_responses=((ChannelUploads(channel_id, playlist_id),),),
        playlist_responses=(final_page,),
        video_responses=(final_response,),
    )
    resumed_service = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=resumed_client,
    )
    resumed_result = resumed_service.execute_seed_unit(job_id, unit_key)

    assert resumed_result == uninterrupted_result
    assert resumed_result.discovered_count == 4
    assert resumed_result.persisted_count == 2
    assert resumed_result.unavailable_count == 1
    assert resumed_client.video_calls == [(final_id,)]

    replay_client = FakeYouTubeClient()
    replay_result = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=replay_client,
    ).execute_seed_unit(job_id, unit_key)

    assert replay_result == resumed_result
    assert replay_client.channel_calls == []
    assert replay_client.playlist_calls == []
    assert replay_client.video_calls == []
    checkpoint = db.execute(
        "SELECT encountered_video_ids_json, unavailable_video_ids_json "
        "FROM youtube_sync_checkpoints WHERE job_id=? AND unit_key=?",
        (job_id, unit_key),
    ).fetchone()
    assert checkpoint["encountered_video_ids_json"] == (
        '["atom0000500","atom0000501","atom0000502","atom0000503"]'
    )
    assert checkpoint["unavailable_video_ids_json"] == '["atom0000501"]'
    private_values = (private_token, first_id, unavailable_id, outside_id, final_id)
    event_payloads = tuple(
        row["metadata_json"]
        for row in db.execute(
            "SELECT metadata_json FROM job_events WHERE job_id=?",
            (job_id,),
        )
    )
    assert all(
        value not in payload
        for value in private_values
        for payload in event_payloads
    )


@pytest.mark.parametrize(
    ("encountered_json", "unavailable_json"),
    (
        ('["atom0000600"]', "[]"),
        ('["atom0000601","atom0000600"]', "[]"),
        ('["atom0000600","atom0000600"]', "[]"),
        ('["not-a-video-id"]', "[]"),
        ("[]", '["atom0000600"]'),
        ('[ "atom0000600" ]', "[]"),
    ),
)
def test_checkpoint_progress_is_canonical_hashed_and_fail_closed(
    db, encountered_json, unavailable_json
):
    service, _, _, _ = _build_service(
        db,
        playlist_responses=(),
        video_responses=(),
    )
    job_id, unit_key = _create_and_claim(db, service)
    db.execute(
        "UPDATE youtube_sync_checkpoints SET encountered_video_ids_json=?, "
        "unavailable_video_ids_json=? WHERE job_id=? AND unit_key=?",
        (encountered_json, unavailable_json, job_id, unit_key),
    )

    with pytest.raises(DomainError) as caught:
        DiscoveryRepository(db).get_youtube_sync_manifest(job_id)

    assert caught.value.code == "STORED_YOUTUBE_SYNC_CHECKPOINT_INVALID"


def test_reexecution_after_success_recomputes_domain_artifact_without_network(db):
    video_ids = (_video_id(400), _video_id(401))
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    playlist_id = "UU" + profile.seed_channel_ids[0][2:]
    service, _, _, _ = _build_service(
        db,
        playlist_responses=(_page(playlist_id, video_ids),),
        video_responses=(_items(video_ids),),
    )
    job_id, unit_key = _create_and_claim(db, service)
    first = service.execute_seed_unit(job_id, unit_key)
    before = _state(db, job_id, unit_key)

    replay_client = FakeYouTubeClient()
    replay = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=replay_client,
    ).execute_seed_unit(job_id, unit_key)

    assert replay.output_hash == first.output_hash
    assert _state(db, job_id, unit_key) == before
    assert replay_client.channel_calls == []
    assert replay_client.playlist_calls == []
    assert replay_client.video_calls == []

    extra = normalize_video_item(
        synthetic_video_item(
            video_id=_video_id(499),
            snippet_published_at=WITHIN_WINDOW,
        ),
        fetched_at=RUN_UPPER_BOUND,
    )
    with transaction(db):
        DiscoveryRepository(db).persist_metadata_batch(
            job_id,
            profile.id,
            DiscoverySourceKind.SEED_UPLOADS,
            profile.seed_channel_ids[0],
            (extra,),
            RUN_UPPER_BOUND,
        )
    with pytest.raises(DomainError) as caught:
        DiscoveryRepository(db).verified_youtube_artifact_hashes(job_id)
    assert caught.value.code == "STORED_YOUTUBE_SYNC_ARTIFACT_INVALID"


def test_cursor_map_and_job_success_roll_back_together_after_reconnect(db, db_path):
    profiles = DiscoveryRepository(db).list_active_profile_versions()
    profile = profiles[0]
    source_key = youtube_search_source_key(profile.search_terms)
    old_upper = RUN_UPPER_BOUND.replace(day=18)
    old_cursor_hash = sha256_text(canonical_json({
        "completed_upper_bound": utc_iso(old_upper),
        "profile_id": profile.profile_id,
        "schema": "youtube-source-cursor.v1",
        "source_key": source_key,
        "source_kind": DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
    }))
    with transaction(db):
        db.execute(
            "INSERT INTO youtube_source_cursors("
            "profile_id, source_kind, source_key, completed_upper_bound, "
            "cursor_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                profile.profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
                utc_iso(old_upper),
                old_cursor_hash,
                utc_iso(old_upper),
            ),
        )
    failpoint = CursorPromotionFailpoint(
        SyntheticCursorPromotionFault("after durable cursor updates")
    )
    service = YouTubeSyncService(
        db,
        clock=lambda: RUN_UPPER_BOUND,
        youtube_client=empty_full_sync_client(profiles),
        failpoint=failpoint,
    )
    request = service.request_full_sync(RUN_UPPER_BOUND)
    total_units = db.execute(
        "SELECT total_units FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0]
    for _ in range(total_units):
        claimed = service.claim_next_runnable(RUN_UPPER_BOUND)
        assert claimed is not None and claimed.job_id == request.job_id
        running = db.execute(
            "SELECT unit_key, stage FROM job_units "
            "WHERE job_id=? AND status='running'",
            (request.job_id,),
        ).fetchone()
        if running["stage"] == JobStage.YOUTUBE_SEED_DISCOVERY.value:
            service.execute_seed_unit(request.job_id, running["unit_key"])
        else:
            assert running["stage"] == JobStage.YOUTUBE_SEARCH_DISCOVERY.value
            service.execute_search_unit(request.job_id, running["unit_key"])
    before = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_source_cursors "
            "ORDER BY profile_id, source_kind, source_key"
        )
    )

    with pytest.raises(SyntheticCursorPromotionFault, match="durable cursor"):
        service.finalize_full_job(request.job_id)

    assert failpoint.calls == 1
    reopened = open_database(db_path)
    try:
        after = tuple(
            dict(row)
            for row in reopened.execute(
                "SELECT * FROM youtube_source_cursors "
                "ORDER BY profile_id, source_kind, source_key"
            )
        )
        assert after == before
        assert JobRepository(reopened).get(request.job_id).status is JobStatus.RUNNING
    finally:
        reopened.close()
