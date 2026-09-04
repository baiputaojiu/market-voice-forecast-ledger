import hashlib
import sqlite3
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import sha256_text, utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoverySourceKind,
    LiveState,
    PresenceOrigin,
    PresenceState,
    canonical_presence_decision_hash,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.voice_verification import (
    ReferenceClipCommand,
    build_presence_job_manifest,
)
from market_voice_forecast_ledger.repositories.discovery import (
    DiscoveryRepository,
)
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.voice_verification import (
    StoredCalibrationIdentity,
    VoiceVerificationRepository,
    canonical_reference_feature_contract_hash,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.voice_verification import (
    PresenceVerificationService,
)


NOW = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
MODEL_NAME = "pilot-speaker-model.onnx"
MODEL_VERSION = "1.0"
ADAPTER_VERSION = "voice-adapter-v1"
VAD_VERSION = "vad-v1"
SELECTION_VERSION = "presence-pilot-selection-v1"
CALIBRATION_HASH = sha256_text("presence-pilot-calibration")
THRESHOLD_VERSION = f"voice-calibration-{CALIBRATION_HASH}"


@dataclass(frozen=True, slots=True)
class CandidateSeed:
    candidate_id: int
    video_id: int
    profile_id: int
    profile_version_id: int
    source_kind: DiscoverySourceKind
    metadata: CanonicalVideoMetadata


@dataclass(frozen=True, slots=True)
class PilotSeed:
    candidates_by_profile: dict[int, tuple[CandidateSeed, ...]]
    reference_profile_by_subject: dict[int, int]


@pytest.fixture
def db(tmp_path: Path):
    conn = open_database(tmp_path / "presence-pilot.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def _youtube_video_id(ordinal: int) -> str:
    return f"v{ordinal:010d}"


def _channel_id(ordinal: int) -> str:
    return f"UC{ordinal:022d}"


def _candidate_sources(
    *,
    has_seed: bool,
    seed_shortage: bool,
    seedless_source_mode: str,
) -> tuple[
    DiscoverySourceKind, ...
]:
    if not has_seed:
        if seedless_source_mode == "manual_oldest":
            return (
                *(DiscoverySourceKind.CROSS_CHANNEL_SEARCH,) * 5,
                DiscoverySourceKind.MANUAL_URL,
            )
        if seedless_source_mode == "search_shortage":
            return (
                *(DiscoverySourceKind.CROSS_CHANNEL_SEARCH,) * 4,
                DiscoverySourceKind.MANUAL_URL,
                DiscoverySourceKind.MANUAL_URL,
            )
        return (DiscoverySourceKind.CROSS_CHANNEL_SEARCH,) * 6
    if seed_shortage:
        return (
            DiscoverySourceKind.SEED_UPLOADS,
            *(DiscoverySourceKind.CROSS_CHANNEL_SEARCH,) * 5,
        )
    return (
        DiscoverySourceKind.SEED_UPLOADS,
        DiscoverySourceKind.SEED_UPLOADS,
        *(DiscoverySourceKind.CROSS_CHANNEL_SEARCH,) * 4,
    )


def seed_pilot_environment(
    db: sqlite3.Connection,
    *,
    seed_shortage: bool = False,
    seedless_source_mode: str = "search_only",
) -> PilotSeed:
    discovery = DiscoveryRepository(db)
    profiles = discovery.list_active_profile_versions()
    assert len(profiles) == 4
    discovery_job_id = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
        """,
        (sha256_text("pilot-discovery-job"), utc_iso(NOW), utc_iso(NOW)),
    ).lastrowid
    candidates_by_profile: dict[int, tuple[CandidateSeed, ...]] = {}
    next_video = 1
    ages = (0, 0, 1, 2, 5, 5)
    with transaction(db):
        for profile in profiles:
            items: list[CandidateSeed] = []
            source_kinds = _candidate_sources(
                has_seed=bool(profile.seed_channel_ids),
                seed_shortage=seed_shortage,
                seedless_source_mode=seedless_source_mode,
            )
            profile_ages = ages
            if not profile.seed_channel_ids:
                if seedless_source_mode == "manual_oldest":
                    profile_ages = (0, 0, 1, 2, 5, 10)
                elif seedless_source_mode == "search_shortage":
                    profile_ages = (0, 1, 2, 5, 0, 10)
            for source_kind, age in zip(
                source_kinds, profile_ages, strict=True
            ):
                metadata = CanonicalVideoMetadata.build(
                    youtube_video_id=_youtube_video_id(next_video),
                    channel_id=_channel_id(next_video),
                    channel_title=f"Synthetic channel {next_video}",
                    title=f"Synthetic candidate {next_video}",
                    description="",
                    published_at=NOW - timedelta(days=age),
                    duration_seconds=600,
                    live_state=LiveState.NOT_LIVE,
                    actual_start_time=None,
                    schema_version="youtube-video-metadata.v1",
                    fetched_at=NOW,
                )
                result = discovery.persist_metadata_batch(
                    discovery_job_id,
                    profile.id,
                    source_kind,
                    f"{source_kind.value}-{profile.profile_id}",
                    (metadata,),
                    NOW + timedelta(microseconds=next_video),
                )
                candidate_id = result.candidate_ids[0]
                video_id = db.execute(
                    "SELECT video_id FROM subject_video_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()[0]
                items.append(
                    CandidateSeed(
                        candidate_id=candidate_id,
                        video_id=video_id,
                        profile_id=profile.profile_id,
                        profile_version_id=profile.id,
                        source_kind=source_kind,
                        metadata=metadata,
                    )
                )
                next_video += 1
            candidates_by_profile[profile.profile_id] = tuple(items)

        SpeakerRepository(db).add_threshold_config(
            SpeakerThresholdConfig(
                version=THRESHOLD_VERSION,
                model_name=MODEL_NAME,
                model_version=MODEL_VERSION,
                subject_rule=ScoreRule("gte", 0.75),
                interviewer_rule=ScoreRule("lte", 0.20),
            ),
            NOW,
            True,
        )
        voice = VoiceVerificationRepository(db)
        feature_rows: list[tuple[int, str, str, int, bytes, str]] = []
        reference_profile_by_subject: dict[int, int] = {}
        for profile in reversed(profiles):
            feature = struct.pack(
                "<4f",
                float(profile.subject_id),
                0.25,
                -0.5,
                0.75,
            )
            feature_hash = hashlib.sha256(feature).hexdigest()
            reference_profile_id = voice.add_reference_profile(
                subject_id=profile.subject_id,
                model_name=MODEL_NAME,
                model_version=MODEL_VERSION,
                adapter_version=ADAPTER_VERSION,
                feature_hash=feature_hash,
                threshold_config_version=THRESHOLD_VERSION,
                created_at=NOW,
                is_active=True,
            )
            reference_profile_by_subject[profile.subject_id] = (
                reference_profile_id
            )
            voice.add_reference_feature(
                reference_profile_id,
                encoding_version="speaker-embedding-v1",
                float_dtype="float32",
                dimension=4,
                embedding_blob=feature,
                created_at=NOW,
            )
            reference_video_id = candidates_by_profile[profile.profile_id][0].video_id
            clip_kinds = (
                "enrollment",
                "enrollment",
                "held_out_positive",
                "negative",
                "negative",
                "negative",
            )
            for ordinal, clip_kind in enumerate(clip_kinds, start=1):
                voice.add_reference_clip(
                    reference_profile_id,
                    ordinal,
                    clip_kind,
                    ReferenceClipCommand(
                        subject_id=profile.subject_id,
                        video_id=reference_video_id,
                        start_ms=ordinal * 10_000,
                        end_ms=ordinal * 10_000 + 5_000,
                        actor="local_user",
                        reason="clear reference speech",
                    ),
                    normalized_audio_sha256=sha256_text(
                        f"audio-{profile.subject_id}-{ordinal}"
                    ),
                    approved_at=NOW,
                )
            feature_rows.append(
                (
                    profile.subject_id,
                    "speaker-embedding-v1",
                    "float32",
                    4,
                    feature,
                    feature_hash,
                )
            )
        feature_contract_hash = canonical_reference_feature_contract_hash(
            tuple(sorted(feature_rows))
        )
        voice.add_calibration_identity(
            StoredCalibrationIdentity(
                calibration_hash=CALIBRATION_HASH,
                expected_prior_fingerprint=sha256_text("empty-calibration"),
                threshold_config_version=THRESHOLD_VERSION,
                model_sha256=sha256_text("pilot-model-artifact"),
                feature_contract_hash=feature_contract_hash,
                activated_at=NOW,
            )
        )
    return PilotSeed(
        candidates_by_profile=candidates_by_profile,
        reference_profile_by_subject=reference_profile_by_subject,
    )


def pilot_service(
    db: sqlite3.Connection,
    *,
    vad_contract_version: str = VAD_VERSION,
    selection_contract_version: str = SELECTION_VERSION,
) -> PresenceVerificationService:
    return PresenceVerificationService(
        db,
        clock=lambda: NOW,
        vad_contract_version=vad_contract_version,
        selection_contract_version=selection_contract_version,
    )


def _set_presence_state(
    db: sqlite3.Connection,
    candidate_id: int,
    state: PresenceState,
) -> int:
    created_at = NOW + timedelta(hours=1, microseconds=candidate_id)
    evidence_ref = f"synthetic-review-{candidate_id}"
    evidence_hash = sha256_text(evidence_ref)
    decision_hash = canonical_presence_decision_hash(
        candidate_id=candidate_id,
        state=state,
        decision_origin=PresenceOrigin.VOICE_VERIFICATION,
        evidence_ref=evidence_ref,
        evidence_hash=evidence_hash,
        created_at=created_at,
    )
    decision_id = db.execute(
        """
        INSERT INTO presence_decisions(
            candidate_id, state, decision_origin, evidence_ref,
            evidence_hash, decision_hash, created_at
        ) VALUES (?, ?, 'voice_verification', ?, ?, ?, ?)
        """,
        (
            candidate_id,
            state.value,
            evidence_ref,
            evidence_hash,
            decision_hash,
            utc_iso(created_at),
        ),
    ).lastrowid
    db.execute(
        "UPDATE subject_video_candidates SET current_presence_decision_id=? "
        "WHERE id=?",
        (decision_id, candidate_id),
    )
    return decision_id


def _video_pipeline_count(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_kind='video_pipeline'"
    ).fetchone()[0]


def _assert_no_pilot_writes(db: sqlite3.Connection) -> None:
    assert _video_pipeline_count(db) == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_binding_sets"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_bindings"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 0


def _pilot_write_counts(db: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "jobs",
            "job_units",
            "job_unit_attempts",
            "job_events",
            "video_pipeline_job_binding_sets",
            "video_pipeline_job_bindings",
            "voice_verification_manifests",
        )
    )


def test_pilot_selects_exact_five_per_active_profile_in_contract_order(db) -> None:
    seed = seed_pilot_environment(db)

    preview = pilot_service(db).preview_pilot()

    counts = Counter(item.profile_id for item in preview.candidates)
    assert len(preview.candidates) == 20
    assert counts == {profile_id: 5 for profile_id in seed.candidates_by_profile}
    assert len({item.candidate_id for item in preview.candidates}) == 20
    calibration = preview.calibration_snapshot
    assert calibration.calibration_hash == CALIBRATION_HASH
    assert calibration.threshold_config_version == THRESHOLD_VERSION
    assert calibration.subject_operator == "gte"
    assert calibration.subject_boundary == 0.75
    assert calibration.interviewer_operator == "lte"
    assert calibration.interviewer_boundary == 0.20
    assert calibration.model_name == MODEL_NAME
    assert calibration.model_version == MODEL_VERSION
    assert calibration.adapter_version == ADAPTER_VERSION
    assert calibration.activated_at == NOW
    assert len(calibration.references) == 4
    assert tuple(item.subject_id for item in calibration.references) == tuple(
        sorted(seed.reference_profile_by_subject)
    )
    assert all(len(item.bundle_hash) == 64 for item in calibration.references)
    assert len(calibration.snapshot_hash) == 64
    for profile_id, candidates in seed.candidates_by_profile.items():
        selected = tuple(
            item for item in preview.candidates if item.profile_id == profile_id
        )
        assert tuple(item.candidate_id for item in selected) == tuple(
            item.candidate_id for item in candidates[:5]
        )
        expected_sources = (
            ("cross_channel_search",) * 5
            if candidates[0].source_kind
            is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
            else (
                "seed_uploads",
                "seed_uploads",
                "cross_channel_search",
                "cross_channel_search",
                "cross_channel_search",
            )
        )
        assert tuple(item.source_kind.value for item in selected) == expected_sources


def test_default_pilot_creates_queued_jobs_with_corrected_vad_identity(db) -> None:
    seed_pilot_environment(db)
    service = PresenceVerificationService(db, clock=lambda: NOW)

    preview = service.preview_pilot()
    creation = service.create_pilot(preview.preview_hash)

    rows = db.execute(
        "SELECT manifest.vad_contract_version, job.status, job.total_units "
        "FROM voice_verification_manifests AS manifest "
        "JOIN jobs AS job ON job.id=manifest.job_id ORDER BY job.id"
    ).fetchall()
    assert len(creation.job_ids) == 20
    assert [tuple(row) for row in rows] == [("vad-v2", "queued", 7)] * 20


def test_pilot_backfills_seed_shortage_by_newest_then_keeps_oldest_slot(db) -> None:
    seed = seed_pilot_environment(db, seed_shortage=True)

    preview = pilot_service(db).preview_pilot()

    for profile_id, candidates in seed.candidates_by_profile.items():
        if candidates[0].source_kind is not DiscoverySourceKind.SEED_UPLOADS:
            continue
        selected = tuple(
            item for item in preview.candidates if item.profile_id == profile_id
        )
        assert tuple(item.candidate_id for item in selected) == tuple(
            item.candidate_id for item in candidates[:5]
        )
        assert tuple(item.source_kind.value for item in selected) == (
            "seed_uploads",
            "cross_channel_search",
            "cross_channel_search",
            "cross_channel_search",
            "cross_channel_search",
        )


@pytest.mark.parametrize(
    ("seedless_source_mode", "expected_sources"),
    (
        ("manual_oldest", ("cross_channel_search",) * 5),
        (
            "search_shortage",
            ("cross_channel_search",) * 4 + ("manual_url",),
        ),
    ),
)
def test_seedless_selection_prefers_search_before_source_backfill(
    db, seedless_source_mode: str, expected_sources: tuple[str, ...]
) -> None:
    seed = seed_pilot_environment(
        db, seedless_source_mode=seedless_source_mode
    )

    preview = pilot_service(db).preview_pilot()

    seedless = next(
        candidates
        for candidates in seed.candidates_by_profile.values()
        if not DiscoveryRepository(db)
        .get_profile_version(candidates[0].profile_version_id)
        .seed_channel_ids
    )
    selected = tuple(
        item
        for item in preview.candidates
        if item.profile_id == seedless[0].profile_id
    )
    assert tuple(item.candidate_id for item in selected) == tuple(
        item.candidate_id for item in seedless[:5]
    )
    assert tuple(item.source_kind.value for item in selected) == expected_sources


def test_preview_uses_canonical_first_observation_not_later_source(db) -> None:
    seed = seed_pilot_environment(db)
    service = pilot_service(db)
    before = service.preview_pilot()
    candidate = seed.candidates_by_profile[sorted(seed.candidates_by_profile)[0]][0]
    profile = DiscoveryRepository(db).get_profile_version(
        candidate.profile_version_id
    )
    later_source = (
        DiscoverySourceKind.CROSS_CHANNEL_SEARCH
        if candidate.source_kind is DiscoverySourceKind.SEED_UPLOADS
        else DiscoverySourceKind.MANUAL_URL
    )
    with transaction(db):
        DiscoveryRepository(db).persist_metadata_batch(
            db.execute(
                "SELECT job_id FROM discovery_observations "
                "WHERE id=(SELECT first_observation_id "
                "FROM subject_video_candidates WHERE id=?)",
                (candidate.candidate_id,),
            ).fetchone()[0],
            profile.id,
            later_source,
            f"later-{candidate.candidate_id}",
            (candidate.metadata,),
            NOW + timedelta(hours=2),
        )

    after = service.preview_pilot()

    assert after.preview_hash == before.preview_hash
    selected = next(
        item for item in after.candidates if item.candidate_id == candidate.candidate_id
    )
    assert selected.source_kind is candidate.source_kind


def test_preview_excludes_rejected_and_active_bound_candidates(db) -> None:
    seed = seed_pilot_environment(db)
    service = pilot_service(db)
    candidates = service.preview_pilot().candidates
    first = candidates[0]
    second = candidates[5]
    _set_presence_state(db, first.candidate_id, PresenceState.REJECTED)
    JobStateService(db, clock=lambda: NOW).create_video_pipeline(
        build_presence_job_manifest(second.manifest_snapshot),
        (second.candidate_id,),
    )

    preview = service.preview_pilot()

    selected_ids = {item.candidate_id for item in preview.candidates}
    assert len(preview.candidates) == 20
    assert first.candidate_id not in selected_ids
    assert second.candidate_id not in selected_ids


@pytest.mark.parametrize(
    "mutation", ("succeeded_with_pending", "stopped_with_running")
)
def test_create_fails_closed_on_corrupt_terminal_bound_job_state(
    db, mutation: str
) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    candidate = preview.candidates[0]
    manifest = build_presence_job_manifest(candidate.manifest_snapshot)
    jobs = JobStateService(db, clock=lambda: NOW)
    job_id = jobs.create_video_pipeline(
        manifest,
        (candidate.candidate_id,),
    )
    if mutation == "succeeded_with_pending":
        db.execute(
            "UPDATE jobs SET status='succeeded', updated_at=? WHERE id=?",
            (utc_iso(NOW + timedelta(minutes=1)), job_id),
        )
    else:
        jobs.begin_unit(job_id, manifest.units[0].unit_key)
        db.execute(
            "UPDATE jobs SET status='stopped', updated_at=? WHERE id=?",
            (utc_iso(NOW + timedelta(minutes=1)), job_id),
        )
    before = _pilot_write_counts(db)

    with pytest.raises(DomainError) as caught:
        service.create_pilot(preview.preview_hash)
    assert caught.value.code == "VIDEO_PIPELINE_BINDINGS_INVALID"
    assert _pilot_write_counts(db) == before


def test_preview_pairs_each_profile_with_its_same_subject_reference(db) -> None:
    seed = seed_pilot_environment(db)

    preview = pilot_service(db).preview_pilot()

    for item in preview.candidates:
        owners = db.execute(
            """
            SELECT profile.subject_id AS profile_subject,
                   reference.subject_id AS reference_subject
            FROM discovery_profiles AS profile
            JOIN voice_reference_profiles AS reference ON reference.id=?
            WHERE profile.id=?
            """,
            (item.manifest_snapshot.reference_profile_id, item.profile_id),
        ).fetchone()
        assert owners["profile_subject"] == owners["reference_subject"]
        assert (
            item.manifest_snapshot.reference_profile_id
            == seed.reference_profile_by_subject[owners["profile_subject"]]
        )


def test_pilot_creation_persists_exact_twenty_canonical_separate_jobs(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    decision_count = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    creation = service.create_pilot(preview.preview_hash)

    assert creation.preview_hash == preview.preview_hash
    assert creation.candidate_ids == tuple(
        item.candidate_id for item in preview.candidates
    )
    assert len(creation.job_ids) == len(creation.manifest_ids) == 20
    assert tuple(sorted(creation.job_ids)) == creation.job_ids
    assert _video_pipeline_count(db) == 20
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_binding_sets"
    ).fetchone()[0] == 20
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_bindings"
    ).fetchone()[0] == 20
    assert db.execute("SELECT COUNT(*) FROM job_units").fetchone()[0] == 140
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 20
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0] == decision_count
    repository = VoiceVerificationRepository(db)
    for item, job_id in zip(preview.candidates, creation.job_ids, strict=True):
        stored = repository.get_manifest_for_job(job_id)
        assert stored.snapshot == item.manifest_snapshot
        assert db.execute(
            "SELECT candidate_id FROM video_pipeline_job_bindings WHERE job_id=?",
            (job_id,),
        ).fetchone()[0] == item.candidate_id


def test_pilot_creation_rolls_back_every_row_on_twentieth_manifest_failure(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    db.execute(
        """
        CREATE TRIGGER inject_twentieth_voice_manifest_failure
        BEFORE INSERT ON voice_verification_manifests
        WHEN (SELECT COUNT(*) FROM voice_verification_manifests)=19
        BEGIN
            SELECT RAISE(ABORT, 'INJECTED_TWENTIETH_FAILURE');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_TWENTIETH_FAILURE"):
        service.create_pilot(preview.preview_hash)

    assert _video_pipeline_count(db) == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_binding_sets"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_bindings"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 0


def test_create_recomputes_preview_and_rejects_stale_decision_before_writes(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    _set_presence_state(
        db,
        preview.candidates[0].candidate_id,
        PresenceState.CONFIRMED,
    )

    with pytest.raises(DomainError) as caught:
        service.create_pilot(preview.preview_hash)
    assert caught.value.code == "PRESENCE_PILOT_CHANGED"

    assert _video_pipeline_count(db) == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 0


def test_create_rejects_profile_and_contract_drift_before_writes(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    profile = DiscoveryRepository(db).list_active_profile_versions()[0]
    with transaction(db):
        DiscoveryRepository(db).create_profile_version(
            profile.subject_id,
            seed_channel_ids=profile.seed_channel_ids,
            search_terms=(*profile.search_terms, "profile-drift"),
            created_at=NOW + timedelta(hours=3),
        )

    with pytest.raises(DomainError) as caught:
        service.create_pilot(preview.preview_hash)
    assert caught.value.code == "PRESENCE_PILOT_CHANGED"
    with pytest.raises(DomainError) as caught:
        pilot_service(
            db,
            vad_contract_version="vad-v2",
            selection_contract_version=SELECTION_VERSION,
        ).create_pilot(service.preview_pilot().preview_hash)
    assert caught.value.code == "PRESENCE_PILOT_CHANGED"

    assert _video_pipeline_count(db) == 0


def test_create_rejects_model_identity_drift_before_writes(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    db.execute("DROP TRIGGER voice_reference_calibrations_no_update")
    db.execute(
        "UPDATE voice_reference_calibrations SET model_sha256=?",
        (sha256_text("drifted-model-artifact"),),
    )

    with pytest.raises(DomainError) as caught:
        service.create_pilot(preview.preview_hash)
    assert caught.value.code == "PRESENCE_PILOT_CHANGED"
    assert _video_pipeline_count(db) == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    ("subject_boundary", "inactive_threshold", "mismatched_adapter"),
)
def test_create_fails_closed_on_active_calibration_state_drift(
    db, mutation: str
) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    if mutation in {"subject_boundary", "inactive_threshold"}:
        db.execute("DROP TRIGGER speaker_threshold_configs_limited_update")
        if mutation == "subject_boundary":
            db.execute(
                "UPDATE speaker_threshold_configs SET subject_boundary=0.74 "
                "WHERE version=?",
                (THRESHOLD_VERSION,),
            )
        else:
            db.execute(
                "UPDATE speaker_threshold_configs SET is_active=0 WHERE version=?",
                (THRESHOLD_VERSION,),
            )
    else:
        db.execute("DROP TRIGGER voice_reference_profiles_limited_update")
        reference_id = db.execute(
            "SELECT id FROM voice_reference_profiles "
            "WHERE is_active=1 ORDER BY subject_id LIMIT 1"
        ).fetchone()[0]
        db.execute(
            "UPDATE voice_reference_profiles SET adapter_version=? WHERE id=?",
            ("mismatched-adapter-v2", reference_id),
        )

    with pytest.raises(DomainError) as caught:
        service.create_pilot(preview.preview_hash)
    assert caught.value.code == (
        "PRESENCE_PILOT_CHANGED"
        if mutation == "subject_boundary"
        else "PRESENCE_PILOT_REFERENCE_INVALID"
    )
    _assert_no_pilot_writes(db)


def test_pilot_insufficiency_is_detected_before_any_write(db) -> None:
    seed = seed_pilot_environment(db)
    first_profile = seed.candidates_by_profile[
        sorted(seed.candidates_by_profile)[0]
    ]
    _set_presence_state(db, first_profile[0].candidate_id, PresenceState.CONFIRMED)
    _set_presence_state(db, first_profile[1].candidate_id, PresenceState.REJECTED)

    with pytest.raises(DomainError) as caught:
        pilot_service(db).create_pilot("a" * 64)
    assert caught.value.code == "PRESENCE_PILOT_INSUFFICIENT"

    assert _video_pipeline_count(db) == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 0


def test_preview_fails_closed_on_corrupt_first_source_and_binding(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    first = service.preview_pilot().candidates[0]
    db.execute("DROP TRIGGER discovery_observations_no_update")
    db.execute(
        "UPDATE discovery_observations SET source_kind='manual_url' WHERE id=?",
        (first.first_observation_id,),
    )
    with pytest.raises(DomainError) as caught:
        service.preview_pilot()
    assert caught.value.code == "STORED_DISCOVERY_OBSERVATION_INVALID"

    db.rollback()


def test_preview_fails_closed_on_corrupt_binding_inventory(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    first = service.preview_pilot().candidates[0]
    job_id = JobStateService(db, clock=lambda: NOW).create_video_pipeline(
        build_presence_job_manifest(first.manifest_snapshot),
        (first.candidate_id,),
    )
    db.execute("DROP TRIGGER video_pipeline_job_binding_sets_seal_once")
    db.execute(
        "UPDATE video_pipeline_job_binding_sets SET expected_binding_count=2 "
        "WHERE job_id=?",
        (job_id,),
    )

    with pytest.raises(DomainError) as caught:
        service.preview_pilot()
    assert caught.value.code == "VIDEO_PIPELINE_BINDINGS_INVALID"


def test_preview_fails_closed_on_foreign_voice_job_binding(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    candidates = service.preview_pilot().candidates
    original = candidates[0]
    foreign = candidates[5]
    job_id = JobStateService(db, clock=lambda: NOW).create_video_pipeline(
        build_presence_job_manifest(original.manifest_snapshot),
        (original.candidate_id,),
    )
    with transaction(db):
        VoiceVerificationRepository(db).add_manifest(
            job_id,
            original.manifest_snapshot,
            created_at=NOW,
        )
    db.execute("DROP TRIGGER video_pipeline_job_bindings_no_update")
    db.execute(
        "UPDATE video_pipeline_job_bindings SET candidate_id=? WHERE job_id=?",
        (foreign.candidate_id, job_id),
    )

    with pytest.raises(DomainError) as caught:
        service.preview_pilot()
    assert caught.value.code == "VOICE_MANIFEST_STORED_INVALID"


@pytest.mark.parametrize("mutation", ("timestamp", "decision_hash"))
def test_preview_fails_closed_on_corrupt_timestamp_or_hash(db, mutation: str) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    first = service.preview_pilot().candidates[0]
    if mutation == "timestamp":
        db.execute("DROP TRIGGER video_metadata_snapshots_no_update")
        db.execute(
            "UPDATE video_metadata_snapshots SET published_at='not-a-time' "
            "WHERE id=?",
            (first.metadata_snapshot_id,),
        )
        expected = "STORED_DISCOVERY_METADATA_INVALID"
    else:
        db.execute("DROP TRIGGER presence_decisions_no_update")
        db.execute(
            "UPDATE presence_decisions SET decision_hash=? WHERE id=?",
            ("0" * 64, first.manifest_snapshot.presence_decision_id),
        )
        expected = "STORED_PRESENCE_DECISION_INVALID"

    with pytest.raises(DomainError) as caught:
        service.preview_pilot()
    assert caught.value.code == expected


def test_preview_fails_closed_on_foreign_or_duplicate_active_reference(db) -> None:
    seed_pilot_environment(db)
    voice = VoiceVerificationRepository(db)
    active_ids = voice.list_active_reference_profile_ids()
    first_subject = db.execute(
        "SELECT subject_id FROM voice_reference_profiles WHERE id=?",
        (active_ids[0],),
    ).fetchone()[0]
    db.execute("DROP INDEX one_active_voice_reference_profile_per_subject")
    db.execute("DROP TRIGGER voice_reference_profiles_no_replace")
    voice.add_reference_profile(
        subject_id=first_subject,
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        adapter_version="duplicate-adapter-v1",
        feature_hash=sha256_text("duplicate-feature"),
        threshold_config_version=THRESHOLD_VERSION,
        created_at=NOW,
        is_active=True,
    )

    with pytest.raises(DomainError) as caught:
        pilot_service(db).preview_pilot()
    assert caught.value.code == "VOICE_REFERENCE_STORED_INVALID"


def test_preview_fails_closed_on_foreign_reference_binding(db) -> None:
    seed_pilot_environment(db)
    voice = VoiceVerificationRepository(db)
    active_ids = voice.list_active_reference_profile_ids()
    foreign_subject_id = db.execute(
        "SELECT subject_id FROM voice_reference_profiles WHERE id=?",
        (active_ids[1],),
    ).fetchone()[0]
    db.execute("DROP INDEX one_active_voice_reference_profile_per_subject")
    db.execute("DROP TRIGGER voice_reference_profiles_limited_update")
    db.execute(
        "UPDATE voice_reference_profiles SET subject_id=? WHERE id=?",
        (foreign_subject_id, active_ids[0]),
    )

    with pytest.raises(DomainError) as caught:
        pilot_service(db).preview_pilot()
    assert caught.value.code == "VOICE_REFERENCE_STORED_INVALID"


def test_preview_fails_closed_on_foreign_profile_binding(db) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    first = service.preview_pilot().candidates[0]
    foreign_profile_id = next(
        item.profile_id
        for item in service.preview_pilot().candidates
        if item.profile_id != first.profile_id
    )
    db.execute("DROP TRIGGER subject_video_candidates_limited_update")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "UPDATE subject_video_candidates SET profile_id=? WHERE id=?",
        (foreign_profile_id, first.candidate_id),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as caught:
        service.preview_pilot()
    assert caught.value.code == "STORED_DISCOVERY_CANDIDATE_INVALID"


def test_two_connections_racing_same_preview_create_only_one_pilot(db) -> None:
    seed_pilot_environment(db)
    preview_hash = pilot_service(db).preview_pilot().preview_hash
    database_path = Path(
        db.execute("PRAGMA database_list").fetchone()["file"]
    )
    barrier = Barrier(2)
    lock = Lock()
    outcomes: list[tuple[str, object]] = []

    def create_from_connection() -> None:
        conn = open_database(database_path)
        try:
            barrier.wait()
            creation = pilot_service(conn).create_pilot(preview_hash)
            outcome: tuple[str, object] = ("success", len(creation.job_ids))
        except DomainError as error:
            outcome = ("domain", error.code)
        except BaseException as error:  # pragma: no cover - diagnostic boundary
            outcome = ("unexpected", type(error).__name__)
        finally:
            conn.close()
        with lock:
            outcomes.append(outcome)

    threads = (
        Thread(target=create_from_connection),
        Thread(target=create_from_connection),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count(("success", 20)) == 1
    assert len(outcomes) == 2
    assert set(outcomes) <= {
        ("success", 20),
        ("domain", "PRESENCE_PILOT_CHANGED"),
        ("domain", "PRESENCE_PILOT_INSUFFICIENT"),
    }
    assert _video_pipeline_count(db) == 20
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_manifests"
    ).fetchone()[0] == 20
