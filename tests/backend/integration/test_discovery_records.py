import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoverySourceKind,
    LiveState,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository


OBSERVED_AT = datetime(2026, 8, 18, 2, 3, 4, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _profile_version(db, name="木野内栄治"):
    return DiscoveryRepository(db).get_current_profile_version_by_subject_name(name)


def _job(db, ordinal: int = 1) -> int:
    cursor = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
        """,
        (
            f"synthetic-youtube-sync-{ordinal}",
            _utc_text(OBSERVED_AT),
            _utc_text(OBSERVED_AT),
        ),
    )
    return cursor.lastrowid


def _metadata(
    *,
    video_id: str = "video000001",
    title: str = "Synthetic market discussion",
    fetched_at: datetime = OBSERVED_AT,
) -> CanonicalVideoMetadata:
    return CanonicalVideoMetadata.build(
        youtube_video_id=video_id,
        channel_id="UCabcdefghijklmnopqrstuv",
        channel_title="Synthetic Channel",
        title=title,
        description="Synthetic description without provider payload.",
        published_at=datetime(2026, 8, 17, 1, 2, 3, tzinfo=timezone.utc),
        duration_seconds=321,
        live_state=LiveState.NOT_LIVE,
        actual_start_time=None,
        schema_version="youtube-video-metadata.v1",
        fetched_at=fetched_at,
    )


def _persist(
    db,
    *,
    job_id: int,
    profile_version_id: int,
    source_kind: DiscoverySourceKind,
    source_key: str,
    items: tuple[CanonicalVideoMetadata, ...],
    observed_at: datetime = OBSERVED_AT,
):
    with transaction(db):
        return DiscoveryRepository(db).persist_metadata_batch(
            job_id=job_id,
            profile_version_id=profile_version_id,
            source_kind=source_kind,
            source_key=source_key,
            items=items,
            observed_at=observed_at,
        )


def _counts(db):
    return tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "videos",
            "video_metadata_snapshots",
            "discovery_observations",
            "subject_video_candidates",
            "presence_decisions",
        )
    )


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_same_video_merges_identity_but_preserves_multiple_observations(db):
    profile = _profile_version(db)
    metadata = _metadata()
    first = _persist(
        db,
        job_id=_job(db, 1),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.SEED_UPLOADS,
        source_key="UCJ1DVBLVpe4FvBZZ94kreaQ",
        items=(metadata,),
    )
    second = _persist(
        db,
        job_id=_job(db, 2),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="a" * 64,
        items=(metadata,),
    )

    assert first.snapshot_ids == second.snapshot_ids
    assert first.candidate_ids == second.candidate_ids
    assert first.observation_ids != second.observation_ids
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_observations"
    ).fetchone()[0] == 2
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT state, decision_origin FROM presence_decisions "
            "WHERE candidate_id=? ORDER BY id",
            (first.candidate_ids[0],),
        )
    ) == (("presence_unverified", "collection_initial"),)


def test_observation_and_initial_presence_use_exact_canonical_hashes(db):
    profile = _profile_version(db)
    job_id = _job(db, 3)
    metadata = _metadata(video_id="video000002")
    result = _persist(
        db,
        job_id=job_id,
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="b" * 64,
        items=(metadata,),
    )
    observation = db.execute(
        "SELECT * FROM discovery_observations WHERE id=?",
        (result.observation_ids[0],),
    ).fetchone()
    idempotency_key = _hash(
        {
            "schema": "youtube-discovery-observation-key.v1",
            "job_id": job_id,
            "profile_id": profile.profile_id,
            "source_kind": "cross_channel_search",
            "source_key": "b" * 64,
            "youtube_video_id": metadata.youtube_video_id,
        }
    )
    observation_hash = _hash(
        {
            "schema": "youtube-discovery-observation.v1",
            "idempotency_key": idempotency_key,
            "metadata_snapshot_id": result.snapshot_ids[0],
            "metadata_snapshot_hash": metadata.canonical_hash,
            "observed_at": _utc_text(OBSERVED_AT),
        }
    )
    assert observation["idempotency_key"] == idempotency_key
    assert observation["observation_hash"] == observation_hash
    decision = db.execute(
        "SELECT * FROM presence_decisions WHERE candidate_id=?",
        (result.candidate_ids[0],),
    ).fetchone()
    assert decision["evidence_ref"] == f"observation:{observation['id']}"
    assert decision["evidence_hash"] == observation_hash
    assert decision["decision_hash"] == _hash(
        {
            "candidate_id": result.candidate_ids[0],
            "created_at": _utc_text(OBSERVED_AT),
            "decision_origin": "collection_initial",
            "evidence_hash": observation_hash,
            "evidence_ref": f"observation:{observation['id']}",
            "schema": "youtube-presence-decision.v1",
            "state": "presence_unverified",
        }
    )


def test_same_hash_reuses_snapshot_and_changed_metadata_appends_snapshot(db):
    profile = _profile_version(db)
    original = _metadata(video_id="video000003")
    first = _persist(
        db,
        job_id=_job(db, 4),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.SEED_UPLOADS,
        source_key="UCJ1DVBLVpe4FvBZZ94kreaQ",
        items=(original,),
    )
    same_content = replace(
        original, fetched_at=OBSERVED_AT + timedelta(hours=1)
    )
    second = _persist(
        db,
        job_id=_job(db, 5),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="c" * 64,
        items=(same_content,),
    )
    changed = _metadata(
        video_id="video000003",
        title="Corrected synthetic title",
        fetched_at=OBSERVED_AT + timedelta(hours=2),
    )
    third = _persist(
        db,
        job_id=_job(db, 6),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="d" * 64,
        items=(changed,),
    )

    assert first.snapshot_ids == second.snapshot_ids
    assert third.snapshot_ids != first.snapshot_ids
    assert db.execute(
        "SELECT COUNT(*) FROM video_metadata_snapshots"
    ).fetchone()[0] == 2
    video = db.execute(
        "SELECT current_metadata_snapshot_id FROM videos "
        "WHERE youtube_video_id='video000003'"
    ).fetchone()
    assert video["current_metadata_snapshot_id"] == third.snapshot_ids[0]
    assert first.candidate_ids == second.candidate_ids == third.candidate_ids
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions WHERE candidate_id=?",
        (first.candidate_ids[0],),
    ).fetchone()[0] == 1


def test_same_video_across_profiles_has_one_video_and_distinct_candidates(db):
    first_profile = _profile_version(db, "木野内栄治")
    second_profile = _profile_version(db, "大川智宏")
    metadata = _metadata(video_id="video000004")
    job_id = _job(db, 7)

    first = _persist(
        db,
        job_id=job_id,
        profile_version_id=first_profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="e" * 64,
        items=(metadata,),
    )
    second = _persist(
        db,
        job_id=job_id,
        profile_version_id=second_profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="f" * 64,
        items=(metadata,),
    )

    assert first.snapshot_ids == second.snapshot_ids
    assert first.observation_ids != second.observation_ids
    assert first.candidate_ids != second.candidate_ids
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM subject_video_candidates"
    ).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0] == 2


def test_different_video_ids_never_merge_by_other_normalized_content(db):
    profile = _profile_version(db)
    first = _metadata(video_id="video000005")
    second = _metadata(video_id="video000006")

    result = _persist(
        db,
        job_id=_job(db, 8),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="1" * 64,
        items=(first, second),
    )

    assert len(set(result.snapshot_ids)) == 2
    assert len(set(result.candidate_ids)) == 2
    assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2
    assert first.canonical_hash != second.canonical_hash


def test_exact_same_job_observation_is_idempotent_and_validated(db):
    profile = _profile_version(db)
    job_id = _job(db, 9)
    metadata = _metadata(video_id="video000007")
    kwargs = {
        "job_id": job_id,
        "profile_version_id": profile.id,
        "source_kind": DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        "source_key": "2" * 64,
        "items": (metadata,),
    }

    first = _persist(db, **kwargs)
    before = _counts(db)
    second = _persist(db, **kwargs)

    assert second == first
    assert _counts(db) == before


@pytest.mark.parametrize("mismatch", ("time", "metadata"))
def test_duplicate_observation_key_rejects_mismatched_stored_content(db, mismatch):
    profile = _profile_version(db)
    job_id = _job(db, 10)
    original = _metadata(video_id="video000008")
    _persist(
        db,
        job_id=job_id,
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="3" * 64,
        items=(original,),
    )
    before = _counts(db)
    metadata = (
        _metadata(video_id="video000008", title="Conflicting title")
        if mismatch == "metadata"
        else original
    )
    observed_at = (
        OBSERVED_AT + timedelta(seconds=1)
        if mismatch == "time"
        else OBSERVED_AT
    )

    with pytest.raises(DomainError) as caught:
        _persist(
            db,
            job_id=job_id,
            profile_version_id=profile.id,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key="3" * 64,
            items=(metadata,),
            observed_at=observed_at,
        )

    assert caught.value.code == "DISCOVERY_OBSERVATION_CONFLICT"
    assert _counts(db) == before


def test_conflicting_duplicate_video_in_batch_is_rejected_before_first_insert(db):
    profile = _profile_version(db)
    first = _metadata(video_id="video000009")
    second = _metadata(video_id="video000009", title="Conflicting batch title")
    before = _counts(db)

    with transaction(db):
        with pytest.raises(DomainError) as caught:
            DiscoveryRepository(db).persist_metadata_batch(
                job_id=_job(db, 11),
                profile_version_id=profile.id,
                source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                source_key="4" * 64,
                items=(first, second),
                observed_at=OBSERVED_AT,
            )
        assert caught.value.code == "DISCOVERY_METADATA_BATCH_CONFLICT"
        assert _counts(db) == before


@pytest.mark.parametrize(
    "mutate",
    (
        lambda item: replace(item, youtube_video_id="short"),
        lambda item: replace(item, channel_id="UCshort"),
        lambda item: replace(item, channel_title=7),
        lambda item: replace(item, published_at=item.published_at.replace(tzinfo=None)),
        lambda item: replace(
            item,
            published_at=item.published_at.astimezone(
                timezone(timedelta(hours=9))
            ),
        ),
        lambda item: replace(item, duration_seconds=True),
        lambda item: replace(item, duration_seconds=-1),
        lambda item: replace(item, live_state="not_live"),
        lambda item: replace(
            item, actual_start_time=datetime(2026, 8, 17, 1, 2, 3)
        ),
        lambda item: replace(item, schema_version="youtube-video-metadata.v2"),
        lambda item: replace(item, canonical_hash="0" * 64),
        lambda item: replace(
            item,
            fetched_at=item.fetched_at.astimezone(timezone(timedelta(hours=9))),
        ),
    ),
)
def test_entire_batch_is_validated_before_first_insert(db, mutate):
    profile = _profile_version(db)
    valid = _metadata(video_id="video000010")
    invalid = mutate(_metadata(video_id="video000011"))
    before = _counts(db)

    with transaction(db):
        with pytest.raises(DomainError) as caught:
            DiscoveryRepository(db).persist_metadata_batch(
                job_id=_job(db, 12),
                profile_version_id=profile.id,
                source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                source_key="5" * 64,
                items=(valid, invalid),
                observed_at=OBSERVED_AT,
            )
        assert caught.value.code == "DISCOVERY_METADATA_INVALID"
        assert _counts(db) == before


def test_metadata_batch_requires_exact_safe_inputs_and_at_most_fifty_items(db):
    profile = _profile_version(db)
    repository = DiscoveryRepository(db)
    valid = _metadata(video_id="video000012")
    job_id = _job(db, 13)
    invalid_calls = (
        {"job_id": True},
        {"profile_version_id": True},
        {"source_kind": "cross_channel_search"},
        {"source_key": "https://youtube.com/watch?v=video000012"},
        {"items": [valid]},
        {"observed_at": OBSERVED_AT.replace(tzinfo=None)},
        {
            "items": tuple(
                _metadata(video_id=f"bulk{index:07d}") for index in range(51)
            )
        },
    )
    base = {
        "job_id": job_id,
        "profile_version_id": profile.id,
        "source_kind": DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        "source_key": "6" * 64,
        "items": (valid,),
        "observed_at": OBSERVED_AT,
    }
    before = _counts(db)

    with transaction(db):
        for overrides in invalid_calls:
            values = {**base, **overrides}
            with pytest.raises(DomainError) as caught:
                repository.persist_metadata_batch(**values)
            assert caught.value.code == "DISCOVERY_METADATA_BATCH_INVALID"
            assert _counts(db) == before


def test_metadata_batch_requires_and_never_commits_the_caller_transaction(db):
    profile = _profile_version(db)
    repository = DiscoveryRepository(db)
    job_id = _job(db, 14)
    kwargs = {
        "job_id": job_id,
        "profile_version_id": profile.id,
        "source_kind": DiscoverySourceKind.SEED_UPLOADS,
        "source_key": "UCJ1DVBLVpe4FvBZZ94kreaQ",
        "items": (_metadata(video_id="video000013"),),
        "observed_at": OBSERVED_AT,
    }

    with pytest.raises(DomainError) as caught:
        repository.persist_metadata_batch(**kwargs)
    assert caught.value.code == "DISCOVERY_TRANSACTION_REQUIRED"

    db.execute("BEGIN IMMEDIATE")
    result = repository.persist_metadata_batch(**kwargs)
    assert db.in_transaction is True
    assert result.candidate_ids
    db.rollback()
    assert _counts(db) == tuple((table, 0) for table, _ in _counts(db))


def test_duplicate_reread_detects_corrupt_snapshot_without_mutation(db):
    profile = _profile_version(db)
    job_id = _job(db, 15)
    metadata = _metadata(video_id="video000014")
    kwargs = {
        "job_id": job_id,
        "profile_version_id": profile.id,
        "source_kind": DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        "source_key": "7" * 64,
        "items": (metadata,),
    }
    result = _persist(db, **kwargs)
    db.execute("DROP TRIGGER video_metadata_snapshots_no_update")
    db.execute(
        "UPDATE video_metadata_snapshots SET title='corrupt stored title' WHERE id=?",
        (result.snapshot_ids[0],),
    )
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _persist(db, **kwargs)

    assert caught.value.code == "STORED_DISCOVERY_METADATA_INVALID"
    assert _counts(db) == before


def test_existing_candidate_rejects_a_current_decision_owned_by_another_candidate(db):
    profile = _profile_version(db)
    job_id = _job(db, 16)
    first_metadata = _metadata(video_id="video000015")
    second_metadata = _metadata(video_id="video000016")
    first = _persist(
        db,
        job_id=job_id,
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="8" * 64,
        items=(first_metadata,),
    )
    second = _persist(
        db,
        job_id=job_id,
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="8" * 64,
        items=(second_metadata,),
    )
    foreign_decision_id = db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (second.candidate_ids[0],),
    ).fetchone()[0]
    db.execute("DROP TRIGGER subject_video_candidates_current_presence_owner_update")
    db.execute(
        "UPDATE subject_video_candidates SET current_presence_decision_id=? WHERE id=?",
        (foreign_decision_id, first.candidate_ids[0]),
    )

    with pytest.raises(DomainError) as caught:
        _persist(
            db,
            job_id=job_id,
            profile_version_id=profile.id,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key="8" * 64,
            items=(first_metadata,),
        )

    assert caught.value.code == "STORED_DISCOVERY_CANDIDATE_INVALID"


def test_changed_metadata_rejects_a_corrupt_current_snapshot_before_pointer_move(db):
    profile = _profile_version(db)
    original = _metadata(video_id="video000017")
    first = _persist(
        db,
        job_id=_job(db, 17),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="9" * 64,
        items=(original,),
    )
    db.execute("DROP TRIGGER video_metadata_snapshots_no_update")
    db.execute(
        "UPDATE video_metadata_snapshots SET title='corrupt current title' WHERE id=?",
        (first.snapshot_ids[0],),
    )
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _persist(
            db,
            job_id=_job(db, 18),
            profile_version_id=profile.id,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key="a1" * 32,
            items=(
                _metadata(
                    video_id="video000017",
                    title="New valid title",
                    fetched_at=OBSERVED_AT + timedelta(minutes=1),
                ),
            ),
        )

    assert caught.value.code == "STORED_DISCOVERY_METADATA_INVALID"
    assert _counts(db) == before
    assert db.execute(
        "SELECT current_metadata_snapshot_id FROM videos "
        "WHERE youtube_video_id='video000017'"
    ).fetchone()[0] == first.snapshot_ids[0]


def test_rediscovery_rejects_a_corrupt_first_observation_and_rolls_back(db):
    profile = _profile_version(db)
    metadata = _metadata(video_id="video000018")
    first = _persist(
        db,
        job_id=_job(db, 19),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="b1" * 32,
        items=(metadata,),
    )
    db.execute("DROP TRIGGER discovery_observations_no_update")
    db.execute(
        "UPDATE discovery_observations SET observation_hash=? WHERE id=?",
        ("0" * 64, first.observation_ids[0]),
    )
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _persist(
            db,
            job_id=_job(db, 20),
            profile_version_id=profile.id,
            source_kind=DiscoverySourceKind.SEED_UPLOADS,
            source_key="UCJ1DVBLVpe4FvBZZ94kreaQ",
            items=(metadata,),
        )

    assert caught.value.code == "STORED_DISCOVERY_OBSERVATION_INVALID"
    assert _counts(db) == before


def test_rediscovery_rejects_more_than_one_collection_initial_decision(db):
    profile = _profile_version(db)
    metadata = _metadata(video_id="video000019")
    first = _persist(
        db,
        job_id=_job(db, 21),
        profile_version_id=profile.id,
        source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
        source_key="c1" * 32,
        items=(metadata,),
    )
    created_at = OBSERVED_AT + timedelta(seconds=1)
    observation_hash = db.execute(
        "SELECT observation_hash FROM discovery_observations WHERE id=?",
        (first.observation_ids[0],),
    ).fetchone()[0]
    evidence_ref = f"observation:{first.observation_ids[0]}"
    duplicate_hash = _hash(
        {
            "candidate_id": first.candidate_ids[0],
            "created_at": _utc_text(created_at),
            "decision_origin": "collection_initial",
            "evidence_hash": observation_hash,
            "evidence_ref": evidence_ref,
            "schema": "youtube-presence-decision.v1",
            "state": "presence_unverified",
        }
    )
    db.execute(
        """
        INSERT INTO presence_decisions(
            candidate_id, state, decision_origin, evidence_ref,
            evidence_hash, decision_hash, created_at
        ) VALUES (?, 'presence_unverified', 'collection_initial', ?, ?, ?, ?)
        """,
        (
            first.candidate_ids[0],
            evidence_ref,
            observation_hash,
            duplicate_hash,
            _utc_text(created_at),
        ),
    )
    before = _counts(db)

    with pytest.raises(DomainError) as caught:
        _persist(
            db,
            job_id=_job(db, 22),
            profile_version_id=profile.id,
            source_kind=DiscoverySourceKind.SEED_UPLOADS,
            source_key="UCJ1DVBLVpe4FvBZZ94kreaQ",
            items=(metadata,),
        )

    assert caught.value.code == "STORED_DISCOVERY_CANDIDATE_INVALID"
    assert _counts(db) == before
