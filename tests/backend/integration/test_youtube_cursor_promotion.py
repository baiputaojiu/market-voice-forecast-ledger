import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    DiscoverySourceKind,
    youtube_search_source_key,
)
from market_voice_forecast_ledger.domain.enums import JobStage, JobStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from tests.backend.youtube_fakes import empty_full_sync_client


RUN_UPPER = datetime(2026, 8, 19, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cursor_hash(
    *, profile_id: int, source_kind: str, source_key: str, upper: datetime
) -> str:
    return _hash({
        "completed_upper_bound": utc_iso(upper),
        "profile_id": profile_id,
        "schema": "youtube-source-cursor.v1",
        "source_key": source_key,
        "source_kind": source_kind,
    })


def _seed_durable_cursor(db, profile, *, upper: datetime) -> None:
    source_key = youtube_search_source_key(profile.search_terms)
    with transaction(db):
        db.execute(
            "INSERT INTO youtube_source_cursors("
            "profile_id, source_kind, source_key, completed_upper_bound, "
            "cursor_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                profile.profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
                utc_iso(upper),
                _cursor_hash(
                    profile_id=profile.profile_id,
                    source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                    source_key=source_key,
                    upper=upper,
                ),
                utc_iso(upper),
            ),
        )


def _full_service(db):
    profiles = DiscoveryRepository(db).list_active_profile_versions()
    return (
        YouTubeSyncService(
            db,
            clock=lambda: RUN_UPPER,
            youtube_client=empty_full_sync_client(profiles),
        ),
        profiles,
    )


def _execute_units(db, service: YouTubeSyncService, job_id: int, count: int) -> None:
    for _ in range(count):
        claimed = service.claim_next_runnable(RUN_UPPER)
        assert claimed is not None and claimed.job_id == job_id
        running = db.execute(
            "SELECT unit_key, stage FROM job_units "
            "WHERE job_id=? AND status='running'",
            (job_id,),
        ).fetchone()
        assert running is not None
        if running["stage"] == JobStage.YOUTUBE_SEED_DISCOVERY.value:
            service.execute_seed_unit(job_id, running["unit_key"])
        elif running["stage"] == JobStage.YOUTUBE_SEARCH_DISCOVERY.value:
            service.execute_search_unit(job_id, running["unit_key"])
        else:
            raise AssertionError(f"unexpected full-sync stage: {running['stage']}")


def _durable_map(db):
    return tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_source_cursors "
            "ORDER BY profile_id, source_kind, source_key"
        )
    )


def _complete_full_job_units(db):
    service, profiles = _full_service(db)
    old_upper = RUN_UPPER - timedelta(days=1)
    _seed_durable_cursor(db, profiles[0], upper=old_upper)
    before = _durable_map(db)
    request = service.request_full_sync(RUN_UPPER)
    total_units = db.execute(
        "SELECT total_units FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0]
    _execute_units(db, service, request.job_id, total_units)
    return service, profiles, request.job_id, total_units, before


def test_no_durable_cursor_moves_until_every_fixed_unit_succeeds(db):
    service, profiles = _full_service(db)
    _seed_durable_cursor(db, profiles[0], upper=RUN_UPPER - timedelta(days=1))
    before = _durable_map(db)
    request = service.request_full_sync(RUN_UPPER)
    total_units = db.execute(
        "SELECT total_units FROM jobs WHERE id=?", (request.job_id,)
    ).fetchone()[0]
    _execute_units(db, service, request.job_id, total_units - 1)

    with pytest.raises(DomainError) as caught:
        service.finalize_full_job(request.job_id)

    assert caught.value.code == "ALL_UNITS_MUST_SUCCEED"
    assert _durable_map(db) == before
    assert JobRepository(db).get(request.job_id).status is JobStatus.RUNNING


def test_success_promotes_the_complete_exact_cursor_map_with_job_success(db):
    service, _, job_id, total_units, before = _complete_full_job_units(db)
    proposals = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=? "
            "ORDER BY profile_id, source_kind, source_key",
            (job_id,),
        )
    )
    assert len(proposals) == total_units
    assert _durable_map(db) == before

    service.finalize_full_job(job_id)

    durable = _durable_map(db)
    assert len(durable) == total_units
    assert JobRepository(db).get(job_id).status is JobStatus.SUCCEEDED
    assert {
        (row["profile_id"], row["source_kind"], row["source_key"])
        for row in durable
    } == {
        (row["profile_id"], row["source_kind"], row["source_key"])
        for row in proposals
    }
    for row in durable:
        assert row["completed_upper_bound"] == utc_iso(RUN_UPPER)
        assert row["updated_at"] == utc_iso(RUN_UPPER)
        assert row["cursor_hash"] == _cursor_hash(
            profile_id=row["profile_id"],
            source_kind=row["source_kind"],
            source_key=row["source_key"],
            upper=RUN_UPPER,
        )


@pytest.mark.parametrize(
    "corruption",
    (
        "missing_proposal",
        "proposal_hash",
        "proposal_bound",
        "proposal_owner",
        "extra_proposal",
        "unit_output",
    ),
)
def test_missing_or_corrupt_finalization_evidence_rolls_back_without_promotion(
    db, corruption
):
    service, profiles, job_id, _, before = _complete_full_job_units(db)
    proposal = db.execute(
        "SELECT * FROM youtube_sync_proposed_cursors WHERE job_id=? "
        "ORDER BY profile_id, source_kind, source_key LIMIT 1",
        (job_id,),
    ).fetchone()
    if corruption == "missing_proposal":
        db.execute(
            "DELETE FROM youtube_sync_proposed_cursors WHERE job_id=? "
            "AND profile_id=? AND source_kind=? AND source_key=?",
            (
                job_id,
                proposal["profile_id"],
                proposal["source_kind"],
                proposal["source_key"],
            ),
        )
    elif corruption == "proposal_hash":
        db.execute(
            "UPDATE youtube_sync_proposed_cursors SET cursor_hash=? "
            "WHERE job_id=? AND profile_id=? AND source_kind=? AND source_key=?",
            (
                "f" * 64,
                job_id,
                proposal["profile_id"],
                proposal["source_kind"],
                proposal["source_key"],
            ),
        )
    elif corruption == "proposal_bound":
        db.execute(
            "UPDATE youtube_sync_proposed_cursors SET completed_upper_bound=? "
            "WHERE job_id=? AND profile_id=? AND source_kind=? AND source_key=?",
            (
                utc_iso(RUN_UPPER - timedelta(seconds=1)),
                job_id,
                proposal["profile_id"],
                proposal["source_kind"],
                proposal["source_key"],
            ),
        )
    elif corruption == "proposal_owner":
        foreign_profile = next(
            profile
            for profile in profiles
            if profile.profile_id != proposal["profile_id"]
        )
        db.execute(
            "UPDATE youtube_sync_proposed_cursors SET profile_id=? "
            "WHERE job_id=? AND profile_id=? AND source_kind=? AND source_key=?",
            (
                foreign_profile.profile_id,
                job_id,
                proposal["profile_id"],
                proposal["source_kind"],
                proposal["source_key"],
            ),
        )
    elif corruption == "extra_proposal":
        source_key = "synthetic-extra-source"
        db.execute(
            "INSERT INTO youtube_sync_proposed_cursors("
            "job_id, profile_id, source_kind, source_key, "
            "completed_upper_bound, cursor_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                profiles[0].profile_id,
                DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                source_key,
                utc_iso(RUN_UPPER),
                _cursor_hash(
                    profile_id=profiles[0].profile_id,
                    source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH.value,
                    source_key=source_key,
                    upper=RUN_UPPER,
                ),
            ),
        )
    else:
        db.execute(
            "UPDATE job_units SET output_hash=? WHERE job_id=? AND ordinal=1",
            ("e" * 64, job_id),
        )

    with pytest.raises(DomainError):
        service.finalize_full_job(job_id)

    assert _durable_map(db) == before
    assert JobRepository(db).get(job_id).status is JobStatus.RUNNING
