import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.discovery import DiscoverySourceKind
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.youtube.client import QUOTA_CONTRACT_VERSION


FIXED_NOW = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)
LATER = FIXED_NOW + timedelta(minutes=15)
MANUAL_VIDEO_ID = "manual00001"


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _utc_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _search_source_key(search_terms: tuple[str, ...]) -> str:
    return _hash(
        {
            "ordered_terms": list(search_terms),
            "schema": "youtube-search-source.v1",
        }
    )


def _expected_profile_units(profile) -> tuple[tuple[str, str, str], ...]:
    seed_units = tuple(
        (
            f"youtube:profile:{profile.profile_id}:seed:{channel_id}",
            "seed_uploads",
            channel_id,
        )
        for channel_id in profile.seed_channel_ids
    )
    return seed_units + (
        (
            f"youtube:profile:{profile.profile_id}:search",
            "cross_channel_search",
            _search_source_key(profile.search_terms),
        ),
    )


def _discoverer_hash(unit_keys: tuple[str, ...]) -> str:
    return _hash(
        {
            "schema": "youtube-sync-discoverer-set.v1",
            "unit_keys": list(unit_keys),
        }
    )


def _profile_set_hash(profile_rows: tuple[dict[str, object], ...]) -> str:
    return _hash(
        {
            "profiles": list(profile_rows),
            "schema": "youtube-sync-profile-set.v1",
        }
    )


def _unit_input_hash(
    *,
    sync_kind: str,
    upper_bound: datetime,
    backfill_floor: datetime,
    profile_id: int,
    profile_version_id: int,
    config_hash: str,
    source_kind: str,
    source_key: str,
    manual_request_id: int | None,
    manual_video_id_hash: str | None,
) -> str:
    return _hash(
        {
            "backfill_floor": _utc_text(backfill_floor),
            "config_hash": config_hash,
            "manual_request_id": manual_request_id,
            "manual_video_id_hash": manual_video_id_hash,
            "profile_id": profile_id,
            "profile_version_id": profile_version_id,
            "quota_contract_version": QUOTA_CONTRACT_VERSION,
            "schema": "youtube-sync-unit-input.v1",
            "source_key": source_key,
            "source_kind": source_kind,
            "sync_kind": sync_kind,
            "upper_bound": _utc_text(upper_bound),
        }
    )


def _manual_video_hash(video_id: str) -> str:
    return _hash(
        {
            "schema": "youtube-manual-video.v1",
            "youtube_video_id": video_id,
        }
    )


def _bootstrap(db) -> tuple:
    bootstrap_reference_data(db)
    return DiscoveryRepository(db).list_active_profile_versions()


def _insert_manual_request(db, profile_id: int) -> int:
    with transaction(db):
        cursor = db.execute(
            "INSERT INTO manual_discovery_requests("
            "profile_id, youtube_video_id, requested_at) VALUES (?, ?, ?)",
            (profile_id, MANUAL_VIDEO_ID, _utc_text(FIXED_NOW)),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _row_dicts(rows) -> tuple[dict[str, object], ...]:
    return tuple(dict(row) for row in rows)


def test_youtube_job_kind_accepts_only_the_three_collection_stages():
    expected = (
        JobStage.YOUTUBE_SEED_DISCOVERY,
        JobStage.YOUTUBE_SEARCH_DISCOVERY,
        JobStage.YOUTUBE_MANUAL_DISCOVERY,
    )
    for ordinal, stage in enumerate(expected, start=1):
        manifest = JobManifest.build(
            JobKind.YOUTUBE_SYNC,
            (
                ManifestUnit(
                    f"youtube:test:{ordinal}",
                    stage,
                    1,
                    "declared-input",
                    (),
                    "youtube-contract-v1",
                ),
            ),
        )
        assert manifest.kind is JobKind.YOUTUBE_SYNC

    for wrong_stage in (
        JobStage.VIDEO_METADATA,
        JobStage.ANALYSIS_INPUT_EXTRACTION,
        JobStage.HEATMAP_UPDATE,
    ):
        with pytest.raises(DomainError) as caught:
            JobManifest.build(
                JobKind.YOUTUBE_SYNC,
                (
                    ManifestUnit(
                        "youtube:test:wrong",
                        wrong_stage,
                        1,
                        None,
                        (),
                        "youtube-contract-v1",
                    ),
                ),
            )
        assert caught.value.code == "INVALID_JOB_STAGE"


@pytest.mark.parametrize(
    "reserved_key",
    ("analysis-input:freeze", "heatmap:promote-current"),
)
def test_youtube_manifest_rejects_analysis_reserved_keys(reserved_key):
    with pytest.raises(DomainError) as caught:
        JobManifest.build(
            JobKind.YOUTUBE_SYNC,
            (
                ManifestUnit(
                    reserved_key,
                    JobStage.YOUTUBE_SEARCH_DISCOVERY,
                    1,
                    None,
                    (),
                    "youtube-contract-v1",
                ),
            ),
        )
    assert caught.value.code == "INVALID_YOUTUBE_MANIFEST"


def test_youtube_manifest_requires_dependencies_to_precede_the_unit():
    with pytest.raises(DomainError) as caught:
        JobManifest.build(
            JobKind.YOUTUBE_SYNC,
            (
                ManifestUnit(
                    "youtube:first",
                    JobStage.YOUTUBE_SEARCH_DISCOVERY,
                    1,
                    None,
                    ("youtube:later",),
                    "youtube-contract-v1",
                ),
                ManifestUnit(
                    "youtube:later",
                    JobStage.YOUTUBE_SEARCH_DISCOVERY,
                    2,
                    None,
                    (),
                    "youtube-contract-v1",
                ),
            ),
        )
    assert caught.value.code == "INVALID_UNIT_DEPENDENCY"


def test_full_manifest_has_exact_profile_discoverer_units_and_bindings(db):
    profiles = _bootstrap(db)
    result = YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_full_sync(
        FIXED_NOW
    )

    assert result.status is JobStatus.QUEUED
    assert result.reused is False
    generic = JobStateService(db).stored_manifest(result.job_id)
    specific = DiscoveryRepository(db).get_youtube_sync_manifest(result.job_id)

    expected_units: list[tuple[str, JobStage, int, str, tuple[str, ...], str]] = []
    expected_profile_rows: list[dict[str, object]] = []
    ordinal = 1
    for profile_ordinal, profile in enumerate(profiles, start=1):
        profile_units = _expected_profile_units(profile)
        unit_keys = tuple(unit_key for unit_key, _, _ in profile_units)
        expected_profile_rows.append(
            {
                "config_hash": profile.config_hash,
                "discoverer_set_hash": _discoverer_hash(unit_keys),
                "ordinal": profile_ordinal,
                "profile_id": profile.profile_id,
                "profile_version_id": profile.id,
            }
        )
        for unit_key, source_kind, source_key in profile_units:
            stage = {
                "seed_uploads": JobStage.YOUTUBE_SEED_DISCOVERY,
                "cross_channel_search": JobStage.YOUTUBE_SEARCH_DISCOVERY,
            }[source_kind]
            expected_units.append(
                (
                    unit_key,
                    stage,
                    ordinal,
                    _unit_input_hash(
                        sync_kind="full_discovery",
                        upper_bound=FIXED_NOW,
                        backfill_floor=datetime(
                            2023, 8, 19, 3, 4, 5, tzinfo=timezone.utc
                        ),
                        profile_id=profile.profile_id,
                        profile_version_id=profile.id,
                        config_hash=profile.config_hash,
                        source_kind=source_kind,
                        source_key=source_key,
                        manual_request_id=None,
                        manual_video_id_hash=None,
                    ),
                    (),
                    {
                        "seed_uploads": "youtube-seed-discovery-contract-v1",
                        "cross_channel_search": "youtube-search-discovery-contract-v1",
                    }[source_kind],
                )
            )
            ordinal += 1

    assert tuple(
        (
            unit.unit_key,
            unit.stage,
            unit.ordinal,
            unit.declared_input_hash,
            unit.dependency_keys,
            unit.execution_contract_hash,
        )
        for unit in generic.units
    ) == tuple(expected_units)
    assert generic.kind is JobKind.YOUTUBE_SYNC
    assert specific.sync_kind == "full_discovery"
    assert specific.upper_bound == FIXED_NOW
    assert specific.backfill_floor == datetime(
        2023, 8, 19, 3, 4, 5, tzinfo=timezone.utc
    )
    assert specific.quota_contract_version == QUOTA_CONTRACT_VERSION
    assert specific.profile_set_hash == _profile_set_hash(
        tuple(expected_profile_rows)
    )
    assert specific.manual_request_id is None
    assert specific.resume_not_before_utc is None
    assert specific.manifest_hash == generic.manifest_hash
    assert specific.created_at == FIXED_NOW
    assert tuple(
        {
            "ordinal": item.ordinal,
            "profile_id": item.profile_id,
            "profile_version_id": item.profile_version_id,
            "config_hash": item.config_hash,
            "discoverer_set_hash": item.discoverer_set_hash,
        }
        for item in specific.profiles
    ) == tuple(expected_profile_rows)

    stored = db.execute(
        "SELECT total_units, manifest_hash FROM jobs WHERE id=?",
        (result.job_id,),
    ).fetchone()
    assert stored["total_units"] == len(expected_units) == 7
    assert stored["manifest_hash"] == generic.manifest_hash
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_checkpoints WHERE job_id=?",
        (result.job_id,),
    ).fetchone()[0] == len(expected_units)


def test_job_unit_set_is_sealed_against_post_creation_addition(db):
    _bootstrap(db)
    result = YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_full_sync(
        FIXED_NOW
    )
    before = _row_dicts(
        db.execute(
            "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal",
            (result.job_id,),
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"):
        db.execute(
            "INSERT INTO job_units("
            "job_id, unit_key, stage, ordinal, declared_input_hash, "
            "dependency_keys_json, execution_contract_hash, status, attempt_count"
            ") VALUES (?, 'youtube:late', 'youtube_search_discovery', 99, "
            "NULL, '[]', 'youtube-contract-v1', 'pending', 0)",
            (result.job_id,),
        )

    assert _row_dicts(
        db.execute(
            "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal",
            (result.job_id,),
        )
    ) == before


def test_manual_manifest_has_one_bound_profile_request_checkpoint_and_unit(db):
    profiles = _bootstrap(db)
    profile = profiles[1]
    request_id = _insert_manual_request(db, profile.profile_id)
    result = YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_manual_sync(
        request_id, FIXED_NOW
    )

    generic = JobStateService(db).stored_manifest(result.job_id)
    specific = DiscoveryRepository(db).get_youtube_sync_manifest(result.job_id)
    source_key = f"manual-request:{request_id}"
    unit_key = f"youtube:manual-request:{request_id}"
    expected_profile = {
        "config_hash": profile.config_hash,
        "discoverer_set_hash": _discoverer_hash((unit_key,)),
        "ordinal": 1,
        "profile_id": profile.profile_id,
        "profile_version_id": profile.id,
    }

    assert result.reused is False
    assert result.status is JobStatus.QUEUED
    assert specific.sync_kind == "manual"
    assert specific.upper_bound == FIXED_NOW
    assert specific.backfill_floor == FIXED_NOW
    assert specific.manual_request_id == request_id
    assert specific.profile_set_hash == _profile_set_hash((expected_profile,))
    assert len(specific.profiles) == 1
    assert {
        "config_hash": specific.profiles[0].config_hash,
        "discoverer_set_hash": specific.profiles[0].discoverer_set_hash,
        "ordinal": specific.profiles[0].ordinal,
        "profile_id": specific.profiles[0].profile_id,
        "profile_version_id": specific.profiles[0].profile_version_id,
    } == expected_profile
    assert len(generic.units) == 1
    assert generic.units[0] == ManifestUnit(
        unit_key,
        JobStage.YOUTUBE_MANUAL_DISCOVERY,
        1,
        _unit_input_hash(
            sync_kind="manual",
            upper_bound=FIXED_NOW,
            backfill_floor=FIXED_NOW,
            profile_id=profile.profile_id,
            profile_version_id=profile.id,
            config_hash=profile.config_hash,
            source_kind="manual_url",
            source_key=source_key,
            manual_request_id=request_id,
            manual_video_id_hash=_manual_video_hash(MANUAL_VIDEO_ID),
        ),
        (),
        "youtube-manual-discovery-contract-v1",
    )
    checkpoint = db.execute(
        "SELECT * FROM youtube_sync_checkpoints WHERE job_id=?",
        (result.job_id,),
    ).fetchone()
    assert checkpoint is not None
    assert checkpoint["unit_key"] == unit_key
    assert checkpoint["source_kind"] == DiscoverySourceKind.MANUAL_URL.value
    assert checkpoint["source_key"] == source_key
    assert checkpoint["effective_lower_bound"] == _utc_text(FIXED_NOW)
    assert checkpoint["upper_bound"] == _utc_text(FIXED_NOW)
    assert checkpoint["uploads_playlist_id"] is None
    assert checkpoint["next_page_token"] is None
    assert checkpoint["page_count"] == 0
    assert checkpoint["batch_ordinal"] == 0
    assert checkpoint["completed_at"] is None


def test_manual_request_job_is_reused_but_never_coalesced_with_full_sync(db):
    profiles = _bootstrap(db)
    full = YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_full_sync(
        FIXED_NOW
    )
    request_id = _insert_manual_request(db, profiles[0].profile_id)
    service = YouTubeSyncService(db, clock=lambda: FIXED_NOW)
    first = service.request_manual_sync(request_id, FIXED_NOW)
    second = service.request_manual_sync(request_id, LATER)
    full_again = service.request_full_sync(LATER)

    assert first.job_id != full.job_id
    assert second.job_id == first.job_id
    assert second.reused is True
    assert full_again.job_id == full.job_id
    assert full_again.reused is True
    assert db.execute(
        "SELECT COUNT(*) FROM youtube_sync_manifests WHERE manual_request_id=?",
        (request_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "bad_requested_at",
    (
        datetime(2026, 8, 19, 3, 4, 5),
        datetime(2026, 8, 19, 12, 4, 5, tzinfo=timezone(timedelta(hours=9))),
        "2026-08-19T03:04:05Z",
        True,
    ),
)
def test_sync_request_rejects_non_exact_utc_before_any_write(db, bad_requested_at):
    _bootstrap(db)
    before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    with pytest.raises(DomainError) as caught:
        YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_full_sync(
            bad_requested_at
        )

    assert caught.value.code == "YOUTUBE_SYNC_REQUEST_INVALID"
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before


def test_failure_between_job_insert_and_sync_manifest_seal_rolls_back_every_row(
    db, monkeypatch
):
    _bootstrap(db)
    observed_job_ids: list[int] = []

    class SyntheticSealFailure(RuntimeError):
        pass

    def fail_after_job_insert(repository, *, job_id, **kwargs):
        assert repository._conn.in_transaction
        assert repository._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0] == 1
        observed_job_ids.append(job_id)
        raise SyntheticSealFailure("synthetic manifest seal failure")

    monkeypatch.setattr(
        DiscoveryRepository,
        "create_youtube_sync_manifest",
        fail_after_job_insert,
    )

    with pytest.raises(SyntheticSealFailure):
        YouTubeSyncService(db, clock=lambda: FIXED_NOW).request_full_sync(
            FIXED_NOW
        )

    assert observed_job_ids
    for table in (
        "jobs",
        "job_units",
        "job_events",
        "youtube_sync_manifests",
        "youtube_sync_manifest_profiles",
        "youtube_sync_checkpoints",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_youtube_sync_creation_primitives_require_caller_transaction(db):
    profiles = _bootstrap(db)
    profile = profiles[0]
    repository = DiscoveryRepository(db)

    with pytest.raises(DomainError) as caught:
        repository.create_youtube_sync_manifest(
            job_id=1,
            sync_kind="full_discovery",
            upper_bound=FIXED_NOW,
            backfill_floor=datetime(2023, 8, 19, 3, 4, 5, tzinfo=timezone.utc),
            quota_contract_version=QUOTA_CONTRACT_VERSION,
            profiles=(profile,),
            manual_request_id=None,
            created_at=FIXED_NOW,
        )

    assert caught.value.code == "DISCOVERY_TRANSACTION_REQUIRED"
