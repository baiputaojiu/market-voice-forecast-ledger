import hashlib
import sqlite3
import struct
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.discovery import (
    CanonicalVideoMetadata,
    DiscoverySourceKind,
    LiveState,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.voice_verification import ReferenceClipCommand
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.retention import RetentionRepository
from market_voice_forecast_ledger.repositories.voice_verification import (
    StoredCalibrationIdentity,
    VoiceVerificationRepository,
)
from market_voice_forecast_ledger.services.retention import AudioDeletionResult
from market_voice_forecast_ledger.services.voice_reference import (
    ApprovedReferenceClip,
    IsolatedReferenceMedia,
    IsolatedReferenceScorer,
    PreparedReferenceAudio,
    ReferenceFeatureData,
    ReferenceMediaPlan,
    VoiceReferenceService,
)
from market_voice_forecast_ledger.voice import media as voice_media
from market_voice_forecast_ledger.voice.media import AcquiredMedia, NormalizedAudio
from market_voice_forecast_ledger.voice.protocol import (
    ReferenceDryRunRequest,
    ReferenceDryRunResponse,
    ReferenceEnrollmentRequest,
    ReferenceEnrollmentResponse,
    ReferenceScoreRequest,
    ReferenceScoreResponse,
)
from tests.backend.voice_fakes import fake_runtime_attestation


NOW = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)
FEATURE_BYTES = struct.pack("<4f", 0.25, -0.5, 0.75, 1.0)
FEATURE_HASH = hashlib.sha256(FEATURE_BYTES).hexdigest()


@dataclass(frozen=True)
class ReferenceSeed:
    subject_id: int
    profile_id: int
    video_id: int
    candidate_id: int
    decision_id: int
    decision_hash: str
    reference_profile_id: int


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "voice-reference.sqlite3")
    apply_migrations(conn)
    bootstrap_reference_data(conn)
    try:
        yield conn
    finally:
        conn.close()


def seed_reference_profile(db: sqlite3.Connection) -> ReferenceSeed:
    profile = db.execute(
        "SELECT id, subject_id FROM discovery_profiles ORDER BY id LIMIT 1"
    ).fetchone()
    discovery_job_id = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
        """,
        ("discovery-fixture", utc_iso(NOW), utc_iso(NOW)),
    ).lastrowid
    metadata = CanonicalVideoMetadata.build(
        youtube_video_id="abcdefghijk",
        channel_id="UCabcdefghijklmnopqrstuv",
        channel_title="Synthetic channel",
        title="Synthetic reference source",
        description="",
        published_at=NOW,
        duration_seconds=600,
        live_state=LiveState.NOT_LIVE,
        actual_start_time=None,
        schema_version="youtube-video-metadata.v1",
        fetched_at=NOW,
    )
    with transaction(db):
        candidate = DiscoveryRepository(db).create_initial_candidate(
            profile_id=profile["id"],
            job_id=discovery_job_id,
            metadata=metadata,
            source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
            source_key="synthetic-search",
            observation_hash="1" * 64,
            idempotency_key="2" * 64,
            observed_at=NOW,
        )
        SpeakerRepository(db).add_threshold_config(
            SpeakerThresholdConfig(
                version="voice-threshold-v1",
                model_name="speaker-model",
                model_version="1.0",
                subject_rule=ScoreRule("gte", 0.7),
                interviewer_rule=ScoreRule("lte", 0.2),
            ),
            NOW,
            True,
        )
        reference_profile_id = db.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (?, 'speaker-model', '1.0', 'adapter-v1', ?,
                      'voice-threshold-v1', ?, 1)
            """,
            (profile["subject_id"], FEATURE_HASH, utc_iso(NOW)),
        ).lastrowid
    decision = db.execute(
        "SELECT decision_hash FROM presence_decisions WHERE id=?",
        (candidate.current_presence_decision_id,),
    ).fetchone()
    return ReferenceSeed(
        subject_id=profile["subject_id"],
        profile_id=profile["id"],
        video_id=candidate.video_id,
        candidate_id=candidate.id,
        decision_id=candidate.current_presence_decision_id,
        decision_hash=decision["decision_hash"],
        reference_profile_id=reference_profile_id,
    )


def expected_clip_hash(
    seed: ReferenceSeed,
    *,
    ordinal: int,
    clip_kind: str,
    start_ms: int,
    end_ms: int,
    audio_hash: str,
    reason: str,
    approved_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "approval_actor": "local_user",
                "approval_reason": reason,
                "approved_at": utc_iso(approved_at),
                "clip_kind": clip_kind,
                "end_ms": end_ms,
                "normalized_audio_sha256": audio_hash,
                "ordinal": ordinal,
                "reference_profile_id": seed.reference_profile_id,
                "schema": "voice-reference-clip.v1",
                "start_ms": start_ms,
                "subject_id": seed.subject_id,
                "video_id": seed.video_id,
            }
        )
    )


def add_valid_clip(
    db: sqlite3.Connection, seed: ReferenceSeed, *, ordinal: int = 1
) -> int:
    return VoiceVerificationRepository(db).add_reference_clip(
        seed.reference_profile_id,
        ordinal,
        "enrollment",
        ReferenceClipCommand(
            subject_id=seed.subject_id,
            video_id=seed.video_id,
            start_ms=1_000,
            end_ms=18_000,
            actor="local_user",
            reason="clear solo speech",
        ),
        normalized_audio_sha256="a" * 64,
        approved_at=NOW,
    )


def test_reference_bundle_recomputes_clip_and_feature_hashes(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)

    feature_id = repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    clip_id = add_valid_clip(db, seed)

    bundle = repository.get_reference_bundle(seed.reference_profile_id)
    assert bundle.feature.id == feature_id
    assert bundle.feature.embedding_blob == FEATURE_BYTES
    assert bundle.feature.feature_sha256 == FEATURE_HASH
    assert tuple(clip.id for clip in bundle.clips) == (clip_id,)
    assert bundle.clips[0].clip_hash == expected_clip_hash(
        seed,
        ordinal=1,
        clip_kind="enrollment",
        start_ms=1_000,
        end_ms=18_000,
        audio_hash="a" * 64,
        reason="clear solo speech",
        approved_at=NOW,
    )


def test_reference_repository_rejects_out_of_sqlite_int64_before_db(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    operations = (
        lambda: repository.get_reference_bundle(2**63),
        lambda: repository.add_reference_feature(
            2**63,
            encoding_version="speaker-embedding-v1",
            float_dtype="float32",
            dimension=4,
            embedding_blob=FEATURE_BYTES,
            created_at=NOW,
        ),
        lambda: repository.add_reference_clip(
            seed.reference_profile_id,
            1,
            "enrollment",
            ReferenceClipCommand(
                subject_id=seed.subject_id,
                video_id=seed.video_id,
                start_ms=2**63,
                end_ms=2**63 + 3_000,
                actor="local_user",
                reason="clear speech",
            ),
            normalized_audio_sha256="a" * 64,
            approved_at=NOW,
        ),
    )

    for operation in operations:
        with pytest.raises(DomainError) as caught:
            operation()
        assert caught.value.code in {
            "VOICE_REFERENCE_INVALID",
            "VOICE_REFERENCE_STORED_INVALID",
        }
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_reference_repository_persists_calibration_identity_and_profile_owner(
    db,
) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    identity = StoredCalibrationIdentity(
        calibration_hash="a" * 64,
        expected_prior_fingerprint="b" * 64,
        threshold_config_version="voice-threshold-v1",
        model_sha256="c" * 64,
        feature_contract_hash="d" * 64,
        activated_at=NOW,
    )
    other_subject_id = db.execute(
        "SELECT id FROM analysis_subjects WHERE id!=? ORDER BY id LIMIT 1",
        (seed.subject_id,),
    ).fetchone()[0]
    with transaction(db):
        profile_id = repository.add_reference_profile(
            subject_id=other_subject_id,
            model_name="model.onnx",
            model_version="model-v1",
            adapter_version="adapter-v1",
            feature_hash="e" * 64,
            threshold_config_version="voice-threshold-v1",
            created_at=NOW,
            is_active=True,
        )
        repository.add_calibration_identity(identity)

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        repository.list_active_reference_profile_ids()
    assert db.execute(
        "SELECT subject_id FROM voice_reference_profiles WHERE id=?",
        (profile_id,),
    ).fetchone()[0] == other_subject_id
    assert repository.get_calibration_identity(identity.calibration_hash) == identity


def test_reference_write_prevalidates_before_insert(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)

    with pytest.raises(DomainError, match="VOICE_REFERENCE_INVALID"):
        repository.add_reference_clip(
            seed.reference_profile_id,
            1,
            "enrollment",
            ReferenceClipCommand(
                subject_id=seed.subject_id,
                video_id=seed.video_id,
                start_ms=2_000,
                end_ms=1_000,
                actor="local_user",
                reason="clear solo speech",
            ),
            normalized_audio_sha256="a" * 64,
            approved_at=NOW,
        )

    assert db.execute("SELECT COUNT(*) FROM voice_reference_clips").fetchone()[0] == 0


def test_reference_read_rejects_corrupt_blob_without_healing(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    add_valid_clip(db, seed)
    db.execute("DROP TRIGGER voice_reference_features_no_update")
    db.execute(
        "UPDATE voice_reference_features SET embedding_blob=? "
        "WHERE reference_profile_id=?",
        (sqlite3.Binary(b"tampered-private-feature"), seed.reference_profile_id),
    )

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        repository.get_reference_bundle(seed.reference_profile_id)

    assert db.execute(
        "SELECT embedding_blob FROM voice_reference_features "
        "WHERE reference_profile_id=?",
        (seed.reference_profile_id,),
    ).fetchone()[0] == b"tampered-private-feature"


def test_reference_read_rejects_non_integer_storage_type(db) -> None:
    seed = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    repository.add_reference_feature(
        seed.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    add_valid_clip(db, seed)
    db.execute("DROP TRIGGER voice_reference_clips_no_update")
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute(
        "UPDATE voice_reference_clips SET ordinal=1.5 WHERE reference_profile_id=?",
        (seed.reference_profile_id,),
    )

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        repository.get_reference_bundle(seed.reference_profile_id)


def test_reference_clip_append_rejects_corrupt_existing_clip_before_insert(
    db,
) -> None:
    seed = seed_reference_profile(db)
    add_valid_clip(db, seed)
    db.execute("DROP TRIGGER voice_reference_clips_no_update")
    db.execute(
        "UPDATE voice_reference_clips SET clip_hash=? "
        "WHERE reference_profile_id=? AND ordinal=1",
        ("b" * 64, seed.reference_profile_id),
    )

    with pytest.raises(DomainError, match="VOICE_REFERENCE_STORED_INVALID"):
        add_valid_clip(db, seed, ordinal=2)

    rows = db.execute(
        "SELECT ordinal, clip_hash FROM voice_reference_clips "
        "WHERE reference_profile_id=? ORDER BY ordinal",
        (seed.reference_profile_id,),
    ).fetchall()
    assert tuple((row["ordinal"], row["clip_hash"]) for row in rows) == (
        (1, "b" * 64),
    )


@dataclass(frozen=True)
class CalibrationSeed:
    subject_ids: tuple[int, ...]
    profile_ids: tuple[int, ...]
    video_ids: tuple[int, ...]


class FakeReferenceMedia:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, int, int]] = []

    def plan(self, model, approval: ApprovedReferenceClip) -> ReferenceMediaPlan:
        job = (
            self.root
            / hashlib.sha256(model.model_name.encode("ascii")).hexdigest()[:12]
            / f"{approval.subject_id}-{approval.ordinal}-{approval.approval_hash[:12]}"
        ).resolve()
        job.mkdir(parents=True, exist_ok=False)
        return ReferenceMediaPlan(
            model_sha256=model.model_sha256,
            approval_hash=approval.approval_hash,
            video_id=approval.video_id,
            source_path=job / "source.media",
            source_part_path=job / "source.media.part",
            normalized_path=job / "normalized.wav",
        )

    def prepare(
        self,
        model,
        approval: ApprovedReferenceClip,
        plan: ReferenceMediaPlan,
    ) -> PreparedReferenceAudio:
        call_no = len(self.calls) + 1
        self.calls.append(
            (model.model_name, approval.subject_id, approval.ordinal)
        )
        plan.source_path.write_bytes(b"source")
        plan.source_part_path.write_bytes(b"part")
        plan.normalized_path.write_bytes(b"normalized")
        return PreparedReferenceAudio(
            local_path=plan.normalized_path,
            audio_duration_ms=approval.end_ms,
            normalized_audio_sha256=hashlib.sha256(
                f"{model.model_name}:{approval.approval_hash}".encode("ascii")
            ).hexdigest(),
        )


class FakeReferenceScorer:
    def __init__(
        self,
        db: sqlite3.Connection,
        scores: dict[str, tuple[float, float]],
        *,
        fail_model: str | None = None,
        elapsed_ms: dict[str, int] | None = None,
    ) -> None:
        self.db = db
        self.scores = scores
        self.fail_model = fail_model
        self.elapsed_ms = elapsed_ms or {}
        self.enrollment_calls: list[tuple[str, int, tuple[int, ...]]] = []
        self.score_calls: list[tuple[str, int, int]] = []
        self.dry_run_calls: list[tuple[str, int]] = []

    def derive_enrollment_feature(
        self,
        model,
        subject_id: int,
        clips: tuple[tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...],
    ) -> ReferenceFeatureData:
        if model.model_name == self.fail_model:
            raise RuntimeError("C:/private/model-native-failure")
        assert self.db.execute(
            "SELECT COUNT(*) FROM local_artifacts"
        ).fetchone()[0] >= 72
        self.enrollment_calls.append(
            (model.model_name, subject_id, tuple(item[0].ordinal for item in clips))
        )
        marker = 1.0 if "campplus" in model.model_name else 2.0
        dimension = 192 if "campplus" in model.model_name else 256
        body = struct.pack(
            f"<{dimension}f",
            marker,
            float(subject_id),
            *([0.0] * (dimension - 2)),
        )
        return ReferenceFeatureData(
            subject_id=subject_id,
            encoding_version="sherpa-speaker-embedding-v1",
            float_dtype="float32-le",
            dimension=dimension,
            embedding_blob=body,
            feature_sha256=hashlib.sha256(body).hexdigest(),
        )

    def score(
        self,
        model,
        subject_id: int,
        feature: ReferenceFeatureData,
        approval: ApprovedReferenceClip,
        audio: PreparedReferenceAudio,
    ) -> float:
        del feature, audio
        if model.model_name == self.fail_model:
            raise RuntimeError("C:/private/model-native-failure")
        self.score_calls.append(
            (model.model_name, subject_id, approval.ordinal)
        )
        positive, negative = self.scores[model.model_name]
        return positive if approval.clip_kind == "held_out_positive" else negative

    def dry_run(
        self,
        model,
        features: tuple[ReferenceFeatureData, ...],
        *,
        candidate_count: int,
    ) -> int:
        assert len(features) == 4
        self.dry_run_calls.append((model.model_name, candidate_count))
        return self.elapsed_ms[model.model_name]


class FakeRetention:
    def __init__(
        self,
        *,
        db: sqlite3.Connection | None = None,
        fail_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.db = db
        self.fail_ids = fail_ids
        self.calls: list[int] = []

    def delete_audio(self, artifact_id: int) -> AudioDeletionResult:
        self.calls.append(artifact_id)
        failed = artifact_id in self.fail_ids
        if not failed and self.db is not None:
            row = self.db.execute(
                "SELECT local_path FROM local_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if row is not None:
                Path(row["local_path"]).unlink(missing_ok=True)
        return AudioDeletionResult(
            artifact_id=artifact_id,
            deleted=not failed,
            already_absent=False,
            retryable=failed,
            error_code="AUDIO_DELETE_OS_ERROR" if failed else None,
            retry_count=int(failed),
            deleted_at=None if failed else NOW,
        )


def seed_calibration_candidates(db: sqlite3.Connection) -> CalibrationSeed:
    profiles = db.execute(
        """
        SELECT profile.id, profile.subject_id
        FROM discovery_profiles AS profile
        JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
        WHERE profile.is_active=1 AND subject.is_active=1
        ORDER BY profile.subject_id
        """
    ).fetchall()
    assert len(profiles) == 4
    job_id = db.execute(
        """
        INSERT INTO jobs(
            job_kind, manifest_hash, total_units, status, created_at, updated_at
        ) VALUES ('youtube_sync', 'reference-approval-source', 1,
                  'succeeded', ?, ?)
        """,
        (utc_iso(NOW), utc_iso(NOW)),
    ).lastrowid
    video_ids: list[int] = []
    with transaction(db):
        for ordinal, profile in enumerate(profiles, start=1):
            metadata = CanonicalVideoMetadata.build(
                youtube_video_id=f"{ordinal:011d}",
                channel_id=f"UC{ordinal:022d}",
                channel_title=f"Synthetic channel {ordinal}",
                title=f"Synthetic person {ordinal}",
                description="",
                published_at=NOW,
                duration_seconds=600,
                live_state=LiveState.NOT_LIVE,
                actual_start_time=None,
                schema_version="youtube-video-metadata.v1",
                fetched_at=NOW,
            )
            candidate = DiscoveryRepository(db).create_initial_candidate(
                profile_id=profile["id"],
                job_id=job_id,
                metadata=metadata,
                source_kind=DiscoverySourceKind.CROSS_CHANNEL_SEARCH,
                source_key=f"reference-{ordinal}",
                observation_hash=f"{ordinal:x}" * 64,
                idempotency_key=f"{ordinal + 4:x}" * 64,
                observed_at=NOW,
            )
            video_ids.append(candidate.video_id)
    return CalibrationSeed(
        subject_ids=tuple(row["subject_id"] for row in profiles),
        profile_ids=tuple(row["id"] for row in profiles),
        video_ids=tuple(video_ids),
    )


def approve_complete_reference_set(
    service: VoiceReferenceService,
    seed: CalibrationSeed,
) -> None:
    for index, subject_id in enumerate(seed.subject_ids):
        own_video = seed.video_ids[index]
        commands = (
            ReferenceClipCommand(
                subject_id, own_video, 0, 15_000,
                "local_user", "clear enrollment speech one",
            ),
            ReferenceClipCommand(
                subject_id, own_video, 15_000, 30_000,
                "local_user", "clear enrollment speech two",
            ),
            ReferenceClipCommand(
                subject_id, own_video, 30_000, 40_000,
                "local_user", "clear held out speech",
            ),
        )
        for command in commands:
            service.approve_clip(command)
        negative_videos = tuple(
            seed.video_ids[item]
            for item in range(4)
            if item != index
        )
        for ordinal, video_id in enumerate(negative_videos, start=1):
            service.approve_clip(
                ReferenceClipCommand(
                    subject_id,
                    video_id,
                    ordinal * 10_000,
                    (ordinal + 1) * 10_000,
                    "local_user",
                    f"confirmed negative speaker {ordinal}",
                )
            )


def calibration_models(tmp_path: Path):
    campplus, _ = fake_runtime_attestation(tmp_path / "campplus")
    resnet, _ = fake_runtime_attestation(tmp_path / "resnet")
    return (
        replace(
            campplus,
            model_name="3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            model_version="sherpa-onnx-1.13.4",
        ),
        replace(
            resnet,
            model_name="wespeaker_zh_cnceleb_resnet34.onnx",
            model_version="sherpa-onnx-1.13.4",
        ),
    )


def task5_service(
    db: sqlite3.Connection,
    tmp_path: Path,
    *,
    scores: dict[str, tuple[float, float]] | None = None,
    elapsed_ms: dict[str, int] | None = None,
    retention: FakeRetention | None = None,
    fail_model: str | None = None,
):
    models = calibration_models(tmp_path)
    if scores is None:
        scores = {
            models[0].model_name: (0.80, 0.30),
            models[1].model_name: (0.75, 0.10),
        }
    if elapsed_ms is None:
        elapsed_ms = {
            models[0].model_name: 900,
            models[1].model_name: 1_100,
        }
    media = FakeReferenceMedia(tmp_path / "private-audio")
    scorer = FakeReferenceScorer(
        db,
        scores,
        fail_model=fail_model,
        elapsed_ms=elapsed_ms,
    )
    effective_retention = retention or FakeRetention(db=db)
    if effective_retention.db is None:
        effective_retention.db = db
    service = VoiceReferenceService(
        db,
        media=media,
        scorer=scorer,
        retention=effective_retention,
        clock=lambda: NOW,
    )
    return service, models, media, scorer, effective_retention, None


def test_isolated_reference_scorer_binds_every_input_and_uses_child_cpu(
    tmp_path: Path,
) -> None:
    model = calibration_models(tmp_path / "model")[0]
    audio_path = (tmp_path / "private" / "normalized.wav").resolve()
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"fake-wav")
    audio_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    approved_at = NOW
    approvals = tuple(
        ApprovedReferenceClip(
            subject_id=1,
            video_id=ordinal,
            start_ms=0,
            end_ms=15_000,
            ordinal=ordinal,
            clip_kind="enrollment" if ordinal < 3 else "held_out_positive",
            actor="operator",
            reason="approved-reference",
            approved_at=approved_at,
            approval_hash=str(ordinal) * 64,
        )
        for ordinal in (1, 2, 3)
    )
    audio = PreparedReferenceAudio(
        local_path=audio_path,
        audio_duration_ms=15_000,
        normalized_audio_sha256=audio_hash,
    )
    feature_body = struct.pack("<192f", *([0.25] * 192))

    class RecordingProcess:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def execute(self, request):
            self.requests.append(request)
            common = {
                "adapter_contract_version": request.adapter_contract_version,
                "input_hash": request.input_hash,
                "model_name": request.model_name,
                "model_version": request.model_version,
                "output_hash": "f" * 64,
            }
            if isinstance(request, ReferenceEnrollmentRequest):
                return ReferenceEnrollmentResponse(
                    **common,
                    operation="reference_enrollment",
                    cpu_time_ms=7,
                    dimension=192,
                    encoding_version="sherpa-speaker-embedding-v1",
                    feature_b64=__import__("base64").b64encode(feature_body).decode(),
                    feature_length=len(feature_body),
                    feature_sha256=hashlib.sha256(feature_body).hexdigest(),
                    float_dtype="float32-le",
                )
            if isinstance(request, ReferenceScoreRequest):
                return ReferenceScoreResponse(
                    **common,
                    operation="reference_score",
                    cpu_time_ms=11,
                    raw_score=0.72,
                )
            assert isinstance(request, ReferenceDryRunRequest)
            return ReferenceDryRunResponse(
                **common,
                operation="reference_dry_run",
                cpu_time_ms=321,
                candidate_count=20,
            )

    process = RecordingProcess()
    scorer = IsolatedReferenceScorer(lambda candidate: process)

    feature = scorer.derive_enrollment_feature(
        model,
        1,
        ((approvals[0], audio), (approvals[1], audio)),
    )
    score = scorer.score(model, 1, feature, approvals[2], audio)
    cpu_ms = scorer.dry_run(
        model,
        tuple(replace(feature, subject_id=subject_id) for subject_id in range(1, 5)),
        candidate_count=20,
    )

    assert score == 0.72
    assert cpu_ms == 321
    enrollment = process.requests[0]
    assert isinstance(enrollment, ReferenceEnrollmentRequest)
    assert enrollment.model_sha256 == model.model_sha256
    assert enrollment.audios[0].approval_hash == approvals[0].approval_hash
    assert enrollment.audios[0].audio_sha256 == audio_hash
    assert (enrollment.audios[0].start_ms, enrollment.audios[0].end_ms) == (0, 15_000)
    score_request = process.requests[1]
    assert isinstance(score_request, ReferenceScoreRequest)
    assert score_request.feature.feature_sha256 == feature.feature_sha256
    dry_run = process.requests[2]
    assert isinstance(dry_run, ReferenceDryRunRequest)
    assert len(dry_run.audios) == 20
    assert dry_run.candidate_count == 20


def test_isolated_reference_media_resolves_video_and_uses_registered_targets(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "private-media").resolve()
    root.mkdir()
    model = calibration_models(tmp_path / "model")[0]
    approval = ApprovedReferenceClip(
        subject_id=1,
        video_id=17,
        start_ms=0,
        end_ms=3_000,
        ordinal=1,
        clip_kind="enrollment",
        actor="operator",
        reason="approved-reference",
        approved_at=NOW,
        approval_hash="a" * 64,
    )
    calls: list[tuple[object, ...]] = []

    class Resolver:
        def youtube_video_id(self, video_id: int) -> str:
            calls.append(("resolve", video_id))
            return "abcdefghijk"

    class Acquirer:
        def acquire_registered(self, video_id, target_dir, *, source_path, part_path):
            calls.append(("acquire", video_id, target_dir, source_path, part_path))
            source_path.write_bytes(b"source")
            return AcquiredMedia(
                path=source_path,
                sha256=hashlib.sha256(b"source").hexdigest(),
                video_id=video_id,
            )

    class Normalizer:
        def normalize_registered(self, source, target):
            calls.append(("normalize", source, target))
            body = __import__(
                "tests.backend.voice_fakes", fromlist=["_synthetic_pcm_wav"]
            )._synthetic_pcm_wav(duration_ms=4_000)
            target.write_bytes(body)
            return NormalizedAudio(
                path=target,
                sha256=hashlib.sha256(body).hexdigest(),
                source_sha256=hashlib.sha256(b"source").hexdigest(),
            )

    media = IsolatedReferenceMedia(Resolver(), Acquirer(), Normalizer(), root)
    plan = media.plan(model, approval)
    prepared = media.prepare(model, approval, plan)

    assert plan.artifact_paths == (
        plan.source_path,
        plan.source_part_path,
        plan.normalized_path,
    )
    assert prepared.local_path == plan.normalized_path
    assert prepared.audio_duration_ms == 4_000
    assert calls[0] == ("resolve", 17)
    assert calls[1][0] == "acquire"
    assert calls[2] == ("normalize", plan.source_path, plan.normalized_path)


def test_isolated_reference_media_retry_uses_atomic_fresh_private_directory(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "private-media").resolve()
    root.mkdir()
    model = calibration_models(tmp_path / "model")[0]
    approval = ApprovedReferenceClip(
        subject_id=1,
        video_id=17,
        start_ms=0,
        end_ms=3_000,
        ordinal=1,
        clip_kind="enrollment",
        actor="operator",
        reason="approved-reference",
        approved_at=NOW,
        approval_hash="a" * 64,
    )

    class Resolver:
        def youtube_video_id(self, video_id: int) -> str:
            del video_id
            return "abcdefghijk"

    class Acquirer:
        def acquire_registered(self, *args, **kwargs):
            raise AssertionError("producer is not used while planning")

    class Normalizer:
        def normalize_registered(self, *args, **kwargs):
            raise AssertionError("producer is not used while planning")

    first = IsolatedReferenceMedia(Resolver(), Acquirer(), Normalizer(), root)
    retry = IsolatedReferenceMedia(Resolver(), Acquirer(), Normalizer(), root)

    first_plan = first.plan(model, approval)
    retry_plan = retry.plan(model, approval)

    assert first_plan.source_path.parent != retry_plan.source_path.parent
    assert first_plan.source_path.parent.parent == root
    assert retry_plan.source_path.parent.parent == root
    assert first_plan.source_path.parent.is_dir()
    assert retry_plan.source_path.parent.is_dir()


@pytest.mark.parametrize(
    "mutation",
    (
        {"subject_id": True},
        {"subject_id": 2**63},
        {"subject_id": 999},
        {"video_id": True},
        {"video_id": 2**63},
        {"video_id": 999},
        {"start_ms": -1},
        {"start_ms": 2**63, "end_ms": 2**63 + 3_000},
        {"start_ms": 10_000, "end_ms": 10_000},
        {"end_ms": 2_999},
        {"end_ms": 120_001},
        {"actor": "system"},
        {"reason": "C:/private/reference.wav"},
    ),
)
def test_approve_clip_rejects_invalid_identity_range_actor_and_reason(
    db,
    tmp_path,
    mutation,
) -> None:
    seed = seed_calibration_candidates(db)
    service, *_ = task5_service(db, tmp_path)
    values = {
        "subject_id": seed.subject_ids[0],
        "video_id": seed.video_ids[0],
        "start_ms": 0,
        "end_ms": 3_000,
        "actor": "local_user",
        "reason": "clear solo speech",
    }
    values.update(mutation)

    with pytest.raises(DomainError) as caught:
        service.approve_clip(ReferenceClipCommand(**values))

    assert caught.value.code == "VOICE_REFERENCE_INVALID"
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0


def test_list_candidates_rejects_id_outside_sqlite_int64_without_db_error(
    db,
    tmp_path,
) -> None:
    service, *_ = task5_service(db, tmp_path)

    with pytest.raises(DomainError) as caught:
        service.list_candidates(2**63)

    assert caught.value.code == "VOICE_REFERENCE_INVALID"
    assert caught.value.__cause__ is None


def test_approve_clip_assigns_exact_slots_and_writes_public_audit_only(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, *_ = task5_service(db, tmp_path)

    approve_complete_reference_set(service, seed)

    approvals = service.list_candidates(seed.subject_ids[0])
    assert tuple((item.ordinal, item.clip_kind) for item in approvals) == (
        (1, "enrollment"),
        (2, "enrollment"),
        (3, "held_out_positive"),
        (4, "negative"),
        (5, "negative"),
        (6, "negative"),
    )
    row = db.execute(
        """
        SELECT entity_type, entity_id, operation, actor_kind, reason_code,
               before_json, after_json
        FROM audit_events
        WHERE entity_type='voice_reference_approval'
          AND entity_id=?
        ORDER BY id LIMIT 1
        """,
        (str(seed.subject_ids[0]),),
    ).fetchone()
    assert tuple(row) == (
        "voice_reference_approval",
        str(seed.subject_ids[0]),
        "approve",
        "user",
        "VOICE_REFERENCE_CLIP_APPROVED",
        None,
        row["after_json"],
    )
    assert set(__import__("json").loads(row["after_json"])) == {
        "approval_hash",
        "end_ms",
        "ordinal",
        "role",
        "start_ms",
        "subject_id",
        "video_id",
    }
    public_audit = "\n".join(
        value or ""
        for row in db.execute(
            "SELECT before_json, after_json FROM audit_events"
        )
        for value in row
    )
    assert "audio_sha256" not in public_audit
    assert "embedding" not in public_audit
    assert "model" not in public_audit
    assert str(tmp_path) not in public_audit


def test_approve_clip_rejects_overlap_wrong_negative_and_seventh_slot(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, *_ = task5_service(db, tmp_path)
    subject_id = seed.subject_ids[0]
    own_video = seed.video_ids[0]
    service.approve_clip(
        ReferenceClipCommand(
            subject_id, own_video, 0, 15_000,
            "local_user", "first enrollment",
        )
    )
    with pytest.raises(DomainError) as overlap:
        service.approve_clip(
            ReferenceClipCommand(
                subject_id, own_video, 10_000, 25_000,
                "local_user", "overlapping enrollment",
            )
        )
    assert overlap.value.code == "VOICE_REFERENCE_INVALID"
    service.approve_clip(
        ReferenceClipCommand(
            subject_id, own_video, 15_000, 30_000,
            "local_user", "second enrollment",
        )
    )
    service.approve_clip(
        ReferenceClipCommand(
            subject_id, own_video, 30_000, 40_000,
            "local_user", "held out speech",
        )
    )
    with pytest.raises(DomainError) as wrong_negative:
        service.approve_clip(
            ReferenceClipCommand(
                subject_id, own_video, 40_000, 50_000,
                "local_user", "not a negative person",
            )
        )
    assert wrong_negative.value.code == "VOICE_REFERENCE_INVALID"
    for ordinal, video_id in enumerate(seed.video_ids[1:], start=1):
        service.approve_clip(
            ReferenceClipCommand(
                subject_id,
                video_id,
                ordinal * 10_000,
                (ordinal + 1) * 10_000,
                "local_user",
                f"negative speaker {ordinal}",
            )
        )
    count_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with pytest.raises(DomainError) as seventh:
        service.approve_clip(
            ReferenceClipCommand(
                subject_id, seed.video_ids[1], 50_000, 60_000,
                "local_user", "seventh approval",
            )
        )
    assert seventh.value.code == "VOICE_REFERENCE_INVALID"
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == count_before


def test_list_candidates_rejects_corrupt_approval_without_healing(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, *_ = task5_service(db, tmp_path)
    service.approve_clip(
        ReferenceClipCommand(
            seed.subject_ids[0], seed.video_ids[0], 0, 15_000,
            "local_user", "clear enrollment",
        )
    )
    db.execute("DROP TRIGGER audit_events_no_update")
    original = db.execute(
        "SELECT after_json FROM audit_events ORDER BY id LIMIT 1"
    ).fetchone()[0]
    corrupted = original.replace('"approval_hash":"', '"approval_hash":"f')
    db.execute("UPDATE audit_events SET after_json=?", (corrupted,))

    with pytest.raises(DomainError) as caught:
        service.list_candidates(seed.subject_ids[0])

    assert caught.value.code == "VOICE_REFERENCE_STORED_INVALID"
    assert db.execute(
        "SELECT after_json FROM audit_events ORDER BY id LIMIT 1"
    ).fetchone()[0] == corrupted


def test_calibration_requires_complete_reference_set_and_thirty_seconds(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, *_ = task5_service(db, tmp_path)
    service.approve_clip(
        ReferenceClipCommand(
            seed.subject_ids[0], seed.video_ids[0], 0, 14_000,
            "local_user", "short enrollment one",
        )
    )
    service.approve_clip(
        ReferenceClipCommand(
            seed.subject_ids[0], seed.video_ids[0], 14_000, 29_000,
            "local_user", "short enrollment two",
        )
    )

    with pytest.raises(DomainError) as caught:
        service.calibrate(models)

    assert caught.value.code == "VOICE_REFERENCE_INCOMPLETE"
    assert db.execute("SELECT COUNT(*) FROM local_artifacts").fetchone()[0] == 0


def test_calibration_selects_widest_separable_model_and_cleans_every_artifact(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, media, scorer, retention, _ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)

    result = service.calibrate(tuple(reversed(models)))

    assert result.model_name == "wespeaker_zh_cnceleb_resnet34.onnx"
    assert result.subject_boundary == 0.75
    assert result.interviewer_boundary == 0.10
    assert result.margin == pytest.approx(0.65)
    assert result.dry_run_cpu_ms == 1_100
    assert len(media.calls) == 48
    assert len(scorer.enrollment_calls) == 8
    assert all(call[2] == (1, 2) for call in scorer.enrollment_calls)
    assert len(scorer.score_calls) == 32
    assert scorer.dry_run_calls == [
        (models[0].model_name, 20),
        (models[1].model_name, 20),
    ]
    assert retention.calls == list(range(1, 145))
    assert len(result.subjects) == 4
    assert all(len(item.clips) == 6 for item in result.subjects)


def test_calibration_preregisters_all_targets_and_cleans_files_produced_on_failure(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "models")

    class ProducingFailureMedia(FakeReferenceMedia):
        def prepare(self, model, approval, plan):
            del model, approval
            for index, path in enumerate(plan.artifact_paths, start=1):
                path.write_bytes(bytes((index,)))
            registered = tuple(
                Path(row["local_path"])
                for row in db.execute(
                    "SELECT local_path FROM local_artifacts ORDER BY id"
                )
            )
            assert registered == plan.artifact_paths
            raise RuntimeError("C:/private/producer-failure")

    media = ProducingFailureMedia(tmp_path / "private")
    retention = FakeRetention(db=db)
    scorer = FakeReferenceScorer(
        db,
        {model.model_name: (0.8, 0.2) for model in models},
        elapsed_ms={model.model_name: 100 for model in models},
    )
    service = VoiceReferenceService(
        db,
        media=media,
        scorer=scorer,
        retention=retention,
        clock=lambda: NOW,
    )
    approve_complete_reference_set(service, seed)

    with pytest.raises(DomainError) as caught:
        service.calibrate(models)

    assert caught.value.code == "VOICE_REFERENCE_CALIBRATION_FAILED"
    rows = db.execute(
        "SELECT id, local_path FROM local_artifacts ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert retention.calls == [1, 2, 3]
    assert all(not Path(row["local_path"]).exists() for row in rows)


def test_calibration_retains_nested_unexpected_leaves_and_reports_dir_failure(
    db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "models")
    private_root = tmp_path / "private"
    unrelated_job = private_root / "unrelated-job"
    unrelated_job.mkdir(parents=True)
    unrelated_target = unrelated_job / "keep.bin"
    unrelated_target.write_bytes(b"unrelated-private-data")
    simulated_reparse: set[Path] = set()
    original_is_reparse = voice_media._is_reparse

    class NestedFailureMedia(FakeReferenceMedia):
        def prepare(self, model, approval, plan):
            del model, approval
            for path in plan.artifact_paths:
                path.write_bytes(b"planned-registered-output")
            unexpected = plan.source_path.parent / "unexpected-dir"
            deep = unexpected / "deep"
            deep.mkdir(parents=True)
            (deep / "private.bin").write_bytes(b"unexpected-private-data")
            (unexpected / "direct.bin").write_bytes(
                b"direct-delete-private-data"
            )
            private_link = deep / "private-link.bin"
            try:
                private_link.symlink_to(unrelated_target)
            except OSError:
                private_link.write_bytes(b"synthetic-reparse-leaf")
                simulated_reparse.add(private_link.absolute())
            raise RuntimeError("C:/private/nested-producer-failure")

    unlink_failures = {"private.bin", "private-link.bin"}

    def flaky_unlink(path: Path) -> None:
        candidate = Path(path)
        if candidate.name in unlink_failures:
            unlink_failures.remove(candidate.name)
            raise PermissionError("injected unlink failure")
        candidate.unlink()

    rmdir_failed = False

    def flaky_rmdir(path: Path) -> None:
        nonlocal rmdir_failed
        candidate = Path(path)
        if candidate.name == "unexpected-dir" and not rmdir_failed:
            rmdir_failed = True
            raise PermissionError("injected rmdir failure")
        candidate.rmdir()

    def is_reparse(path: Path) -> bool:
        candidate = Path(path).absolute()
        return candidate in simulated_reparse or original_is_reparse(path)

    monkeypatch.setattr(
        voice_media, "_unlink_unregistered_leaf", flaky_unlink, raising=False
    )
    monkeypatch.setattr(
        voice_media, "_rmdir_unregistered_directory", flaky_rmdir, raising=False
    )
    monkeypatch.setattr(voice_media, "_is_reparse", is_reparse)
    media = NestedFailureMedia(private_root)
    retention = FakeRetention(db=db)
    service = VoiceReferenceService(
        db,
        media=media,
        scorer=FakeReferenceScorer(
            db,
            {model.model_name: (0.8, 0.2) for model in models},
            elapsed_ms={model.model_name: 100 for model in models},
        ),
        retention=retention,
        clock=lambda: NOW,
    )
    approve_complete_reference_set(service, seed)

    with pytest.raises(DomainError) as caught:
        service.calibrate(models)

    assert caught.value.code == "VOICE_REFERENCE_CLEANUP_FAILED"
    rows = db.execute(
        "SELECT id, local_path FROM local_artifacts ORDER BY id"
    ).fetchall()
    names = tuple(Path(row["local_path"]).name for row in rows)
    assert names[:3] == ("source.media", "source.media.part", "normalized.wav")
    assert unlink_failures == set()
    assert frozenset(names[3:]) == {
        "private.bin",
        "private-link.bin",
    }
    assert retention.calls == [1, 2, 3, 4, 5]
    assert tuple(
        path
        for path in private_root.rglob("*")
        if path.is_file() or path.is_symlink()
    ) == (unrelated_target,)
    assert unrelated_target.read_bytes() == b"unrelated-private-data"
    assert rmdir_failed is True


def test_calibration_direct_deletes_unexpected_leaf_if_registration_fails(
    db: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "models")
    private_root = tmp_path / "private"

    class NestedFailureMedia(FakeReferenceMedia):
        def prepare(self, model, approval, plan):
            del model, approval
            for path in plan.artifact_paths:
                path.write_bytes(b"planned-registered-output")
            nested = plan.source_path.parent / "unexpected-dir"
            nested.mkdir()
            (nested / "registration-failure.bin").write_bytes(
                b"unexpected-private-data"
            )
            raise RuntimeError("C:/private/nested-producer-failure")

    first_unlink = True

    def flaky_unlink(path: Path) -> None:
        nonlocal first_unlink
        candidate = Path(path)
        if candidate.name == "registration-failure.bin" and first_unlink:
            first_unlink = False
            raise PermissionError("injected unlink failure")
        candidate.unlink()

    monkeypatch.setattr(
        voice_media, "_unlink_unregistered_leaf", flaky_unlink, raising=False
    )
    media = NestedFailureMedia(private_root)
    retention = FakeRetention(db=db)
    service = VoiceReferenceService(
        db,
        media=media,
        scorer=FakeReferenceScorer(
            db,
            {model.model_name: (0.8, 0.2) for model in models},
            elapsed_ms={model.model_name: 100 for model in models},
        ),
        retention=retention,
        clock=lambda: NOW,
    )
    add_artifact = service._artifacts.add_audio_artifact

    def failing_registration(
        path: Path, *, created_at: datetime | None = None
    ) -> int:
        if path.name == "registration-failure.bin":
            raise sqlite3.OperationalError("injected registration failure")
        return add_artifact(path, created_at=created_at)

    monkeypatch.setattr(
        service._artifacts, "add_audio_artifact", failing_registration
    )
    approve_complete_reference_set(service, seed)

    with pytest.raises(DomainError) as caught:
        service.calibrate(models)

    assert caught.value.code == "VOICE_REFERENCE_CALIBRATION_FAILED"
    assert retention.calls == [1, 2, 3]
    assert db.execute("SELECT COUNT(*) FROM local_artifacts").fetchone()[0] == 3
    assert tuple(
        path for path in private_root.rglob("*") if path.is_file()
    ) == ()


def test_calibration_rejects_ambient_transaction_before_private_production(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "models")

    class ProducingFailureMedia(FakeReferenceMedia):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.plan_calls = 0

        def plan(self, model, approval):
            self.plan_calls += 1
            return super().plan(model, approval)

        def prepare(self, model, approval, plan):
            del model, approval
            for path in plan.artifact_paths:
                path.write_bytes(b"private-producer-output")
            raise RuntimeError("C:/private/producer-failure")

    private_root = tmp_path / "private"
    media = ProducingFailureMedia(private_root)
    retention = FakeRetention(
        db=db,
        fail_ids=frozenset(range(1, 1_000)),
    )
    scorer = FakeReferenceScorer(
        db,
        {model.model_name: (0.8, 0.2) for model in models},
        elapsed_ms={model.model_name: 100 for model in models},
    )
    service = VoiceReferenceService(
        db,
        media=media,
        scorer=scorer,
        retention=retention,
        clock=lambda: NOW,
    )
    approve_complete_reference_set(service, seed)

    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DomainError) as caught:
            service.calibrate(models)
    finally:
        db.rollback()

    assert caught.value.code == "VOICE_REFERENCE_CALIBRATION_FAILED"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert media.plan_calls == 0
    assert retention.calls == []
    assert db.execute("SELECT COUNT(*) FROM local_artifacts").fetchone()[0] == 0
    assert tuple(
        path for path in private_root.rglob("*") if path.is_file()
    ) == ()


def test_calibration_tie_breaks_by_cpu_then_lexicographic_model_name(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "models")
    equal_scores = {model.model_name: (0.8, 0.2) for model in models}
    service, cpu_models, *_ = task5_service(
        db,
        tmp_path / "cpu",
        scores=equal_scores,
        elapsed_ms={models[0].model_name: 700, models[1].model_name: 600},
    )
    approve_complete_reference_set(service, seed)
    cpu_winner = service.calibrate(tuple(reversed(cpu_models)))
    assert cpu_winner.model_name == models[1].model_name

    db2 = open_database(tmp_path / "lexicographic.sqlite3")
    apply_migrations(db2)
    bootstrap_reference_data(db2)
    try:
        seed2 = seed_calibration_candidates(db2)
        lexical_models = calibration_models(tmp_path / "lex-models")
        lexical_scores = {
            model.model_name: (0.8, 0.2) for model in lexical_models
        }
        lexical_service, lexical_candidates, *_ = task5_service(
            db2,
            tmp_path / "lex",
            scores=lexical_scores,
            elapsed_ms={model.model_name: 600 for model in lexical_models},
        )
        approve_complete_reference_set(lexical_service, seed2)
        winner = lexical_service.calibrate(tuple(reversed(lexical_candidates)))
        assert winner.model_name == lexical_models[0].model_name
    finally:
        db2.close()


def test_calibration_discards_only_nonseparable_models_and_all_failure_is_safe(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    models = calibration_models(tmp_path / "identities")
    scores = {
        models[0].model_name: (0.4, 0.5),
        models[1].model_name: (0.7, 0.1),
    }
    service, candidates, *_ = task5_service(
        db,
        tmp_path / "one-good",
        scores=scores,
    )
    approve_complete_reference_set(service, seed)
    result = service.calibrate(candidates)
    assert result.model_name == models[1].model_name

    db2 = open_database(tmp_path / "all-nonseparable.sqlite3")
    apply_migrations(db2)
    bootstrap_reference_data(db2)
    try:
        seed2 = seed_calibration_candidates(db2)
        all_models = calibration_models(tmp_path / "all-identities")
        all_bad = {model.model_name: (0.5, 0.5) for model in all_models}
        bad_service, bad_candidates, _, _, cleanup, _ = task5_service(
            db2,
            tmp_path / "all-bad",
            scores=all_bad,
        )
        approve_complete_reference_set(bad_service, seed2)
        with pytest.raises(DomainError) as caught:
            bad_service.calibrate(bad_candidates)
        assert caught.value.code == "VOICE_MODEL_NOT_SEPARABLE"
        assert len(cleanup.calls) == 144
        assert db2.execute(
            "SELECT COUNT(*) FROM speaker_threshold_configs"
        ).fetchone()[0] == 0
        assert db2.execute(
            "SELECT COUNT(*) FROM voice_reference_profiles"
        ).fetchone()[0] == 0
    finally:
        db2.close()


def test_calibration_rejects_invalid_attestation_and_maps_native_failure(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, media, *_ = task5_service(db, tmp_path / "invalid")
    approve_complete_reference_set(service, seed)
    with pytest.raises(DomainError) as invalid:
        service.calibrate((replace(models[0], model_sha256="bad"), models[1]))
    assert invalid.value.code == "VOICE_MODEL_CANDIDATES_INVALID"
    assert media.calls == []

    failing_name = models[1].model_name
    failing, candidates, _, _, cleanup, _ = task5_service(
        db,
        tmp_path / "failure",
        fail_model=failing_name,
    )
    with pytest.raises(DomainError) as failed:
        failing.calibrate(candidates)
    assert failed.value.code == "VOICE_REFERENCE_CALIBRATION_FAILED"
    assert "private" not in str(failed.value).casefold()
    assert failed.value.__cause__ is None
    assert failed.value.__context__ is None
    assert "private" not in "".join(
        traceback.format_exception(failed.value)
    ).casefold()
    assert len(cleanup.calls) == 144


def test_calibration_cleanup_failure_attempts_all_and_blocks_result(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    retention = FakeRetention(fail_ids=frozenset({3, 21}))
    service, models, _, _, _, _ = task5_service(
        db,
        tmp_path,
        retention=retention,
    )
    approve_complete_reference_set(service, seed)

    with pytest.raises(DomainError) as caught:
        service.calibrate(models)

    assert caught.value.code == "VOICE_REFERENCE_CLEANUP_FAILED"
    assert retention.calls == list(range(1, 73))
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles"
    ).fetchone()[0] == 0


def seed_old_active_calibration(
    db: sqlite3.Connection,
    seed: CalibrationSeed,
) -> tuple[str, tuple[int, ...]]:
    version = "old-threshold-v1"
    old_ids: list[int] = []
    with transaction(db):
        SpeakerRepository(db).add_threshold_config(
            SpeakerThresholdConfig(
                version=version,
                model_name="old-model.onnx",
                model_version="old-v1",
                subject_rule=ScoreRule("gte", 0.9),
                interviewer_rule=ScoreRule("lte", 0.0),
            ),
            NOW,
            True,
        )
        for subject_id in seed.subject_ids:
            feature_body = struct.pack("<4f", float(subject_id), 0.0, 0.0, 0.0)
            feature_hash = hashlib.sha256(feature_body).hexdigest()
            profile_id = db.execute(
                """
                INSERT INTO voice_reference_profiles(
                    subject_id, model_name, model_version, adapter_version,
                    feature_hash, threshold_config_version, created_at,
                    is_active
                ) VALUES (?, 'old-model.onnx', 'old-v1', 'adapter-v0', ?,
                          ?, ?, 1)
                """,
                (subject_id, feature_hash, version, utc_iso(NOW)),
            ).lastrowid
            assert type(profile_id) is int
            old_ids.append(profile_id)
            repository = VoiceVerificationRepository(db)
            clip_kinds = (
                "enrollment",
                "enrollment",
                "held_out_positive",
                "negative",
                "negative",
                "negative",
            )
            for ordinal, clip_kind in enumerate(
                clip_kinds,
                start=1,
            ):
                repository.add_reference_clip(
                    profile_id,
                    ordinal,
                    clip_kind,
                    ReferenceClipCommand(
                        subject_id=subject_id,
                        video_id=seed.video_ids[(ordinal - 1) % len(seed.video_ids)],
                        start_ms=0,
                        end_ms=3_000,
                        actor="local_user",
                        reason="old-reference",
                    ),
                    normalized_audio_sha256=hashlib.sha256(
                        f"old:{subject_id}:{ordinal}".encode("ascii")
                    ).hexdigest(),
                    approved_at=NOW,
                )
            repository.add_reference_feature(
                profile_id,
                encoding_version="speaker-embedding-v1",
                float_dtype="float32",
                dimension=4,
                embedding_blob=feature_body,
                created_at=NOW,
            )
    return version, tuple(old_ids)


def test_activate_calibration_atomically_replaces_exact_four_reference_bundles(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    old_version, old_profile_ids = seed_old_active_calibration(db, seed)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    audit_count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    activated = service.activate_calibration(result)

    assert len(activated.reference_profile_ids) == 4
    assert db.execute(
        "SELECT is_active FROM speaker_threshold_configs WHERE version=?",
        (old_version,),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM speaker_threshold_configs WHERE is_active=1"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles WHERE is_active=1"
    ).fetchone()[0] == 4
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles WHERE id IN "
        f"({','.join('?' for _ in old_profile_ids)}) AND is_active=0",
        old_profile_ids,
    ).fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM voice_reference_clips").fetchone()[0] == 48
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_features"
    ).fetchone()[0] == 8
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_count
    for subject_id, profile_id in activated.reference_profile_ids:
        bundle = VoiceVerificationRepository(db).get_reference_bundle(profile_id)
        subject_result = next(
            item for item in result.subjects if item.subject_id == subject_id
        )
        assert bundle.subject_id == subject_id
        assert bundle.model_name == result.model_name
        assert bundle.feature.feature_sha256 == subject_result.feature.feature_sha256
        assert bundle.feature.embedding_blob == subject_result.feature.embedding_blob
        assert tuple(clip.ordinal for clip in bundle.clips) == tuple(range(1, 7))
        assert tuple(clip.normalized_audio_sha256 for clip in bundle.clips) == tuple(
            item.normalized_audio_sha256 for item in subject_result.clips
        )


def test_activation_replay_is_idempotent_and_stale_calibration_is_rejected(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    seed_old_active_calibration(db, seed)
    first_service, models, *_ = task5_service(db, tmp_path / "first")
    approve_complete_reference_set(first_service, seed)
    first_result = first_service.calibrate(models)
    second_service, second_models, *_ = task5_service(
        db,
        tmp_path / "second",
        scores={model.model_name: (0.79, 0.20) for model in models},
        elapsed_ms={model.model_name: 222 for model in models},
    )
    stale_result = second_service.calibrate(second_models)

    first_activation = first_service.activate_calibration(first_result)
    counts = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
            "voice_reference_calibrations",
        )
    )

    replay = first_service.activate_calibration(first_result)

    assert replay == first_activation
    assert tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
            "voice_reference_calibrations",
        )
    ) == counts
    with pytest.raises(DomainError) as caught:
        second_service.activate_calibration(stale_result)
    assert caught.value.code == "VOICE_REFERENCE_ACTIVATION_STALE"
    assert tuple(
        row["id"]
        for row in db.execute(
            "SELECT id FROM voice_reference_profiles WHERE is_active=1 ORDER BY id"
        )
    ) == tuple(profile_id for _, profile_id in first_activation.reference_profile_ids)


def test_activation_replay_rejects_canonical_threshold_corruption_without_heal(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    activation = service.activate_calibration(result)
    version = activation.threshold_config_version
    original = db.execute(
        "SELECT model_name, model_version, subject_operator, "
        "subject_boundary, interviewer_operator, interviewer_boundary "
        "FROM speaker_threshold_configs WHERE version=?",
        (version,),
    ).fetchone()
    assert original is not None
    db.execute("DROP TRIGGER speaker_threshold_configs_limited_update")
    db.execute("PRAGMA ignore_check_constraints=ON")
    mutations = (
        ("model_name", "other-model.onnx"),
        ("model_version", "other-v1"),
        ("subject_operator", "lte"),
        ("subject_boundary", 0.76),
        ("interviewer_operator", "gte"),
        ("interviewer_boundary", 0.11),
    )
    counts = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
            "voice_reference_calibrations",
        )
    )

    for column, corrupt_value in mutations:
        db.execute(
            f"UPDATE speaker_threshold_configs SET {column}=? WHERE version=?",
            (corrupt_value, version),
        )
        with pytest.raises(DomainError) as caught:
            service.activate_calibration(result)
        assert caught.value.code in {
            "VOICE_REFERENCE_ACTIVATION_FAILED",
            "VOICE_REFERENCE_ACTIVATION_STALE",
        }
        assert db.execute(
            f"SELECT {column} FROM speaker_threshold_configs WHERE version=?",
            (version,),
        ).fetchone()[0] == corrupt_value
        assert tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "speaker_threshold_configs",
                "voice_reference_profiles",
                "voice_reference_clips",
                "voice_reference_features",
                "voice_reference_calibrations",
            )
        ) == counts
        db.execute(
            f"UPDATE speaker_threshold_configs SET {column}=? WHERE version=?",
            (original[column], version),
        )


def test_reference_bundle_rejects_feature_contract_metadata_corruption(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    activation = service.activate_calibration(result)
    profile_id = activation.reference_profile_ids[0][1]
    db.execute("DROP TRIGGER voice_reference_features_no_update")
    db.execute(
        "UPDATE voice_reference_features SET encoding_version=? "
        "WHERE reference_profile_id=?",
        ("sherpa-speaker-embedding-v2", profile_id),
    )

    with pytest.raises(DomainError) as caught:
        VoiceVerificationRepository(db).get_reference_bundle(profile_id)

    assert caught.value.code == "VOICE_REFERENCE_STORED_INVALID"
    assert db.execute(
        "SELECT encoding_version FROM voice_reference_features "
        "WHERE reference_profile_id=?",
        (profile_id,),
    ).fetchone()[0] == "sherpa-speaker-embedding-v2"


def test_active_reference_ids_traverse_canonical_bundles(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    activation = service.activate_calibration(result)
    repository = VoiceVerificationRepository(db)
    expected_ids = tuple(
        profile_id for _, profile_id in activation.reference_profile_ids
    )

    assert repository.list_active_reference_profile_ids() == expected_ids

    db.execute("DROP TRIGGER voice_reference_features_no_update")
    db.execute(
        "UPDATE voice_reference_features SET encoding_version=? "
        "WHERE reference_profile_id=?",
        ("sherpa-speaker-embedding-v2", expected_ids[0]),
    )

    with pytest.raises(DomainError) as caught:
        repository.list_active_reference_profile_ids()

    assert caught.value.code == "VOICE_REFERENCE_STORED_INVALID"


def test_activation_rereads_complete_existing_bundles_before_mutation(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    seed = seed_calibration_candidates(db)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    with transaction(db):
        version = "corrupt-old-threshold"
        SpeakerRepository(db).add_threshold_config(
            SpeakerThresholdConfig(
                version=version,
                model_name="old-model.onnx",
                model_version="old-v1",
                subject_rule=ScoreRule("gte", 0.9),
                interviewer_rule=ScoreRule("lte", 0.0),
            ),
            NOW,
            True,
        )
        db.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (?, 'old-model.onnx', 'old-v1', 'adapter-v0', ?, ?, ?, 1)
            """,
            (seed.subject_ids[0], "a" * 64, version, utc_iso(NOW)),
        )
    before = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
        )
    )

    with pytest.raises(DomainError) as caught:
        service.activate_calibration(result)

    assert caught.value.code == "VOICE_REFERENCE_ACTIVATION_FAILED"
    assert tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
        )
    ) == before


def test_activate_calibration_rejects_corrupt_feature_before_mutation(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    old_version, old_ids = seed_old_active_calibration(db, seed)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    first = result.subjects[0]
    corrupt_feature = replace(first.feature, embedding_blob=b"corrupt!")
    corrupt = replace(
        result,
        subjects=(replace(first, feature=corrupt_feature), *result.subjects[1:]),
    )

    with pytest.raises(DomainError) as caught:
        service.activate_calibration(corrupt)

    assert caught.value.code == "VOICE_REFERENCE_ACTIVATION_INVALID"
    assert db.execute(
        "SELECT is_active FROM speaker_threshold_configs WHERE version=?",
        (old_version,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles WHERE id IN "
        f"({','.join('?' for _ in old_ids)}) AND is_active=1",
        old_ids,
    ).fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM voice_reference_clips").fetchone()[0] == 24


def test_activate_calibration_rejects_mutated_identity_clip_and_result_hash(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    old_version, old_ids = seed_old_active_calibration(db, seed)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    first = result.subjects[0]
    first_clip = first.clips[0]
    mutations = (
        replace(result, model_sha256="f" * 64),
        replace(result, expected_prior_fingerprint="f" * 64),
        replace(
            result,
            subjects=(
                replace(
                    first,
                    feature=replace(first.feature, encoding_version="other-v1"),
                ),
                *result.subjects[1:],
            ),
        ),
        replace(
            result,
            subjects=(
                replace(
                    first,
                    feature=replace(first.feature, float_dtype="float32"),
                ),
                *result.subjects[1:],
            ),
        ),
        replace(
            result,
            subjects=(
                replace(
                    first,
                    feature=replace(
                        first.feature,
                        dimension=192 if first.feature.dimension == 256 else 256,
                    ),
                ),
                *result.subjects[1:],
            ),
        ),
        replace(
            result,
            subjects=(
                replace(
                    first,
                    clips=(
                        replace(first_clip, normalized_audio_sha256="f" * 64),
                        *first.clips[1:],
                    ),
                ),
                *result.subjects[1:],
            ),
        ),
        replace(result, calibration_hash="f" * 64),
        replace(
            result,
            subject_boundary=result.interviewer_boundary,
        ),
    )
    before_counts = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
        )
    )

    for mutation in mutations:
        with pytest.raises(DomainError) as caught:
            service.activate_calibration(mutation)
        assert caught.value.code == "VOICE_REFERENCE_ACTIVATION_INVALID"
        assert tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "speaker_threshold_configs",
                "voice_reference_profiles",
                "voice_reference_clips",
                "voice_reference_features",
            )
        ) == before_counts

    assert db.execute(
        "SELECT is_active FROM speaker_threshold_configs WHERE version=?",
        (old_version,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles WHERE id IN "
        f"({','.join('?' for _ in old_ids)}) AND is_active=1",
        old_ids,
    ).fetchone()[0] == 4


def test_activate_calibration_rolls_back_and_resets_transition_authority(
    db,
    tmp_path,
) -> None:
    seed = seed_calibration_candidates(db)
    old_version, old_ids = seed_old_active_calibration(db, seed)
    service, models, *_ = task5_service(db, tmp_path)
    approve_complete_reference_set(service, seed)
    result = service.calibrate(models)
    before_counts = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "audit_events",
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
        )
    )
    db.executescript(
        """
        CREATE TEMP TRIGGER task5_threshold_authority
        BEFORE UPDATE OF is_active ON speaker_threshold_configs
        WHEN voice_reference_threshold_transition_authorized(
            OLD.version, OLD.is_active, NEW.is_active
        ) != 1
        BEGIN SELECT RAISE(ABORT, 'ACTIVATION_NOT_AUTHORIZED'); END;
        CREATE TEMP TRIGGER task5_profile_authority
        BEFORE UPDATE OF is_active ON voice_reference_profiles
        WHEN voice_reference_profile_transition_authorized(
            OLD.id, OLD.subject_id, OLD.is_active, NEW.is_active
        ) != 1
        BEGIN SELECT RAISE(ABORT, 'ACTIVATION_NOT_AUTHORIZED'); END;
        CREATE TEMP TRIGGER task5_injected_failure
        BEFORE INSERT ON voice_reference_features
        WHEN (SELECT COUNT(*) FROM voice_reference_features) = 7
        BEGIN SELECT RAISE(ABORT, 'PRIVATE_INJECTED_FAILURE'); END;
        """
    )

    with pytest.raises(DomainError) as caught:
        service.activate_calibration(result)

    assert caught.value.code == "VOICE_REFERENCE_ACTIVATION_FAILED"
    assert "injected" not in str(caught.value).casefold()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value)).casefold()
    assert "private_injected_failure" not in rendered
    assert tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "audit_events",
            "speaker_threshold_configs",
            "voice_reference_profiles",
            "voice_reference_clips",
            "voice_reference_features",
        )
    ) == before_counts
    assert db.execute(
        "SELECT is_active FROM speaker_threshold_configs WHERE version=?",
        (old_version,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM voice_reference_profiles WHERE id IN "
        f"({','.join('?' for _ in old_ids)}) AND is_active=1",
        old_ids,
    ).fetchone()[0] == 4
    with pytest.raises(sqlite3.IntegrityError, match="ACTIVATION_NOT_AUTHORIZED"):
        db.execute(
            "UPDATE speaker_threshold_configs SET is_active=0 WHERE version=?",
            (old_version,),
        )
