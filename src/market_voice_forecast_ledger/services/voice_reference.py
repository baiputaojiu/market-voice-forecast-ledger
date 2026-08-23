"""Approve, calibrate, and atomically activate private voice references."""

import hashlib
import base64
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Protocol

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import (
    CalibrationSample,
    ReferenceClipCommand,
    calibrate_thresholds,
)
from market_voice_forecast_ledger.repositories.audit import (
    AuditEventInput,
    AuditRepository,
)
from market_voice_forecast_ledger.repositories.retention import (
    RetentionRepository,
)
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.voice_verification import (
    StoredCalibrationIdentity,
    VoiceVerificationRepository,
    canonical_reference_feature_contract_hash,
)
from market_voice_forecast_ledger.services.audit import validate_audit_reason
from market_voice_forecast_ledger.services.retention import AudioDeletionResult
from market_voice_forecast_ledger.voice.media import (
    AcquiredMedia,
    NormalizedAudio,
    PrivateJobWorkspace,
    create_private_job_directory,
    normalized_wav_duration_ms,
)
from market_voice_forecast_ledger.voice.process import ReferenceAdapterProcess
from market_voice_forecast_ledger.voice.protocol import (
    ReferenceAudioInput,
    ReferenceDryRunRequest,
    ReferenceDryRunResponse,
    ReferenceEnrollmentRequest,
    ReferenceEnrollmentResponse,
    ReferenceFeatureInput,
    ReferenceScoreRequest,
    ReferenceScoreResponse,
)
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MODEL_NAMES = frozenset(
    {
        "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "wespeaker_zh_cnceleb_resnet34.onnx",
    }
)
_SLOT_KINDS = (
    "enrollment",
    "enrollment",
    "held_out_positive",
    "negative",
    "negative",
    "negative",
)
_APPROVAL_ENTITY = "voice_reference_approval"
_SQLITE_INT_MAX = 2**63 - 1


@dataclass(frozen=True, slots=True)
class ApprovedReferenceClip:
    subject_id: int
    video_id: int
    start_ms: int
    end_ms: int
    ordinal: int
    clip_kind: str
    actor: str
    reason: str
    approved_at: datetime
    approval_hash: str


@dataclass(frozen=True, slots=True)
class PreparedReferenceAudio:
    local_path: Path
    audio_duration_ms: int
    normalized_audio_sha256: str


@dataclass(frozen=True, slots=True)
class ReferenceMediaPlan:
    model_sha256: str
    approval_hash: str
    video_id: int
    source_path: Path
    source_part_path: Path
    normalized_path: Path

    @property
    def artifact_paths(self) -> tuple[Path, Path, Path]:
        return (self.source_path, self.source_part_path, self.normalized_path)


@dataclass(frozen=True, slots=True)
class ReferenceFeatureData:
    subject_id: int
    encoding_version: str
    float_dtype: str
    dimension: int
    embedding_blob: bytes
    feature_sha256: str


@dataclass(frozen=True, slots=True)
class CalibratedReferenceClip:
    approval: ApprovedReferenceClip
    normalized_audio_sha256: str


@dataclass(frozen=True, slots=True)
class CalibratedSubject:
    subject_id: int
    feature: ReferenceFeatureData
    clips: tuple[CalibratedReferenceClip, ...]


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    model_name: str
    model_version: str
    adapter_version: str
    model_sha256: str
    subject_boundary: float
    interviewer_boundary: float
    margin: float
    dry_run_cpu_ms: int
    expected_prior_fingerprint: str
    subjects: tuple[CalibratedSubject, ...]
    calibration_hash: str


@dataclass(frozen=True, slots=True)
class CalibrationActivation:
    threshold_config_version: str
    reference_profile_ids: tuple[tuple[int, int], ...]


class ReferenceMedia(Protocol):
    def plan(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
    ) -> ReferenceMediaPlan: ...

    def prepare(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
        plan: ReferenceMediaPlan,
    ) -> PreparedReferenceAudio: ...


class ReferenceVideoResolver(Protocol):
    def youtube_video_id(self, video_id: int) -> str: ...


class RegisteredMediaAcquirer(Protocol):
    def acquire_registered(
        self,
        video_id: str,
        target_dir: Path,
        *,
        source_path: Path,
        part_path: Path,
    ) -> AcquiredMedia: ...


class RegisteredMediaNormalizer(Protocol):
    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio: ...


class IsolatedReferenceMedia:
    """Plan registered paths before using Task 4 media subprocesses."""

    def __init__(
        self,
        resolver: ReferenceVideoResolver,
        acquirer: RegisteredMediaAcquirer,
        normalizer: RegisteredMediaNormalizer,
        private_work_root: Path,
    ) -> None:
        self._resolver = resolver
        self._acquirer = acquirer
        self._normalizer = normalizer
        self._root = private_work_root

    def plan(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
    ) -> ReferenceMediaPlan:
        try:
            if (
                not isinstance(model, RuntimeAttestation)
                or type(approval) is not ApprovedReferenceClip
                or not isinstance(self._root, Path)
            ):
                raise ValueError("reference media plan is invalid")
            root = self._root.absolute().resolve(strict=True)
            if not root.is_dir() or root != self._root.absolute():
                raise ValueError("reference media root is invalid")
            job = create_private_job_directory(root)
            return ReferenceMediaPlan(
                model_sha256=model.model_sha256,
                approval_hash=approval.approval_hash,
                video_id=approval.video_id,
                source_path=(job / "source.media").resolve(),
                source_part_path=(job / "source.media.part").resolve(),
                normalized_path=(job / "normalized.wav").resolve(),
            )
        except Exception:
            raise DomainError(
                "VOICE_REFERENCE_MEDIA_PLAN_FAILED",
                "reference media planning failed",
            ) from None

    def prepare(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
        plan: ReferenceMediaPlan,
    ) -> PreparedReferenceAudio:
        try:
            if (
                type(plan) is not ReferenceMediaPlan
                or plan.model_sha256 != model.model_sha256
                or plan.approval_hash != approval.approval_hash
                or plan.video_id != approval.video_id
            ):
                raise ValueError("reference media plan identity mismatch")
            youtube_video_id = self._resolver.youtube_video_id(approval.video_id)
            acquired = self._acquirer.acquire_registered(
                youtube_video_id,
                plan.source_path.parent,
                source_path=plan.source_path,
                part_path=plan.source_part_path,
            )
            if (
                type(acquired) is not AcquiredMedia
                or acquired.path != plan.source_path
                or acquired.video_id != youtube_video_id
            ):
                raise ValueError("reference acquisition result is invalid")
            normalized = self._normalizer.normalize_registered(
                plan.source_path, plan.normalized_path
            )
            if (
                type(normalized) is not NormalizedAudio
                or normalized.path != plan.normalized_path
                or normalized.source_sha256 != acquired.sha256
            ):
                raise ValueError("reference normalization result is invalid")
            return PreparedReferenceAudio(
                local_path=normalized.path,
                audio_duration_ms=normalized_wav_duration_ms(normalized.path),
                normalized_audio_sha256=normalized.sha256,
            )
        except Exception:
            raise DomainError(
                "VOICE_REFERENCE_MEDIA_PREPARATION_FAILED",
                "reference media preparation failed",
            ) from None


class ReferenceScorer(Protocol):
    def derive_enrollment_feature(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        clips: tuple[
            tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...
        ],
    ) -> ReferenceFeatureData: ...

    def score(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        feature: ReferenceFeatureData,
        approval: ApprovedReferenceClip,
        audio: PreparedReferenceAudio,
    ) -> float: ...

    def dry_run(
        self,
        model: RuntimeAttestation,
        features: tuple[ReferenceFeatureData, ...],
        *,
        candidate_count: int,
    ) -> int: ...


class ReferenceProcessFactory(Protocol):
    def __call__(self, model: RuntimeAttestation) -> ReferenceAdapterProcess: ...


class IsolatedReferenceScorer:
    """Bridge reference calibration to the attested isolated adapter child."""

    def __init__(self, process_factory: ReferenceProcessFactory) -> None:
        if not callable(process_factory):
            raise ValueError("reference process factory is invalid")
        self._process_factory = process_factory
        self._scored_audio: dict[
            str, list[tuple[ApprovedReferenceClip, PreparedReferenceAudio]]
        ] = {}

    def derive_enrollment_feature(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        clips: tuple[
            tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...
        ],
    ) -> ReferenceFeatureData:
        request = ReferenceEnrollmentRequest.with_canonical_hash(
            **_reference_model_identity(model),
            operation="reference_enrollment",
            audios=tuple(
                _reference_audio(approval, audio) for approval, audio in clips
            ),
        )
        response = self._process_factory(model).execute(request)
        if not isinstance(response, ReferenceEnrollmentResponse):
            raise ValueError("reference enrollment response is invalid")
        try:
            embedding = base64.b64decode(
                response.feature_b64.encode("ascii"), validate=True
            )
        except Exception:
            raise ValueError("reference enrollment response is invalid") from None
        return ReferenceFeatureData(
            subject_id=subject_id,
            encoding_version=response.encoding_version,
            float_dtype=response.float_dtype,
            dimension=response.dimension,
            embedding_blob=embedding,
            feature_sha256=response.feature_sha256,
        )

    def score(
        self,
        model: RuntimeAttestation,
        subject_id: int,
        feature: ReferenceFeatureData,
        approval: ApprovedReferenceClip,
        audio: PreparedReferenceAudio,
    ) -> float:
        request = ReferenceScoreRequest.with_canonical_hash(
            **_reference_model_identity(model),
            operation="reference_score",
            audio=_reference_audio(approval, audio),
            feature=_reference_feature(feature),
        )
        response = self._process_factory(model).execute(request)
        if not isinstance(response, ReferenceScoreResponse):
            raise ValueError("reference score response is invalid")
        self._scored_audio.setdefault(model.model_sha256, []).append(
            (approval, audio)
        )
        return response.raw_score

    def dry_run(
        self,
        model: RuntimeAttestation,
        features: tuple[ReferenceFeatureData, ...],
        *,
        candidate_count: int,
    ) -> int:
        observed = tuple(self._scored_audio.get(model.model_sha256, ()))
        if not observed or candidate_count != 20:
            raise ValueError("reference dry run input is invalid")
        request = ReferenceDryRunRequest.with_canonical_hash(
            **_reference_model_identity(model),
            operation="reference_dry_run",
            audios=tuple(
                _reference_audio(*observed[index % len(observed)])
                for index in range(candidate_count)
            ),
            candidate_count=candidate_count,
            features=tuple(_reference_feature(feature) for feature in features),
        )
        response = self._process_factory(model).execute(request)
        if not isinstance(response, ReferenceDryRunResponse):
            raise ValueError("reference dry run response is invalid")
        return response.cpu_time_ms


class AudioRetention(Protocol):
    def delete_audio(self, artifact_id: int) -> AudioDeletionResult: ...


def canonical_reference_approval_hash(
    *,
    subject_id: int,
    video_id: int,
    start_ms: int,
    end_ms: int,
    ordinal: int,
    clip_kind: str,
    actor: str,
    reason: str,
    approved_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "actor": actor,
                "approved_at": utc_iso(approved_at),
                "clip_kind": clip_kind,
                "end_ms": end_ms,
                "ordinal": ordinal,
                "reason": reason,
                "schema": "voice-reference-approval.v1",
                "start_ms": start_ms,
                "subject_id": subject_id,
                "video_id": video_id,
            }
        )
    )


class VoiceReferenceService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        media: ReferenceMedia | None = None,
        scorer: ReferenceScorer | None = None,
        retention: AudioRetention | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._media = media
        self._scorer = scorer
        self._retention = retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._audit = AuditRepository(conn)
        self._artifacts = RetentionRepository(conn)
        self._voice = VoiceVerificationRepository(conn, clock=self._clock)
        self._speakers = SpeakerRepository(conn)
        self._threshold_transitions: frozenset[tuple[object, ...]] = frozenset()
        self._profile_transitions: frozenset[tuple[object, ...]] = frozenset()
        self._register_transition_authorizers()

    def approve_clip(
        self, command: ReferenceClipCommand
    ) -> ApprovedReferenceClip:
        self._validate_command(command)
        approval_error: DomainError | None = None
        try:
            with transaction(self._conn):
                existing = self.list_candidates(command.subject_id)
                if len(existing) >= len(_SLOT_KINDS):
                    _invalid_reference()
                ordinal = len(existing) + 1
                clip_kind = _SLOT_KINDS[ordinal - 1]
                self._validate_slot(command, existing, clip_kind)
                approved_at = _utc_datetime(self._clock())
                approval_hash = canonical_reference_approval_hash(
                    subject_id=command.subject_id,
                    video_id=command.video_id,
                    start_ms=command.start_ms,
                    end_ms=command.end_ms,
                    ordinal=ordinal,
                    clip_kind=clip_kind,
                    actor=command.actor,
                    reason=command.reason,
                    approved_at=approved_at,
                )
                self._audit.append(
                    AuditEventInput(
                        entity_type=_APPROVAL_ENTITY,
                        entity_id=str(command.subject_id),
                        scope_id=command.subject_id,
                        operation="approve",
                        actor_kind="user",
                        reason_code="VOICE_REFERENCE_CLIP_APPROVED",
                        reason_text=command.reason,
                        before=None,
                        after={
                            "approval_hash": approval_hash,
                            "end_ms": command.end_ms,
                            "ordinal": ordinal,
                            "role": clip_kind,
                            "start_ms": command.start_ms,
                            "subject_id": command.subject_id,
                            "video_id": command.video_id,
                        },
                        created_at=approved_at,
                    )
                )
        except DomainError as cause:
            if cause.code in {
                "VOICE_REFERENCE_INVALID",
                "VOICE_REFERENCE_STORED_INVALID",
            }:
                approval_error = cause
            else:
                approval_error = _invalid_reference_error()
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError):
            approval_error = _invalid_reference_error()
        if approval_error is not None:
            raise approval_error
        return ApprovedReferenceClip(
            subject_id=command.subject_id,
            video_id=command.video_id,
            start_ms=command.start_ms,
            end_ms=command.end_ms,
            ordinal=ordinal,
            clip_kind=clip_kind,
            actor=command.actor,
            reason=command.reason,
            approved_at=approved_at,
            approval_hash=approval_hash,
        )

    def list_candidates(
        self, subject_id: int
    ) -> tuple[ApprovedReferenceClip, ...]:
        if not _positive_int(subject_id) or not self._active_subject(subject_id):
            _invalid_reference()
        events: tuple[object, ...] = ()
        stored_error: DomainError | None = None
        try:
            events = self._audit.list_for_entity(
                _APPROVAL_ENTITY, str(subject_id)
            )
            approvals: list[ApprovedReferenceClip] = []
            for expected_ordinal, event in enumerate(events, start=1):
                approval = self._approval_from_event(
                    subject_id, expected_ordinal, event
                )
                self._validate_slot(
                    ReferenceClipCommand(
                        subject_id=approval.subject_id,
                        video_id=approval.video_id,
                        start_ms=approval.start_ms,
                        end_ms=approval.end_ms,
                        actor=approval.actor,
                        reason=approval.reason,
                    ),
                    tuple(approvals),
                    approval.clip_kind,
                )
                approvals.append(approval)
            return tuple(approvals)
        except DomainError as cause:
            if cause.code == "VOICE_REFERENCE_INVALID" and not events:
                stored_error = cause
            else:
                stored_error = _stored_reference_invalid_error()
        except (sqlite3.DatabaseError, LookupError, TypeError, ValueError):
            stored_error = _stored_reference_invalid_error()
        if stored_error is not None:
            raise stored_error
        raise _stored_reference_invalid_error()

    def list_all_candidates(self) -> tuple[ApprovedReferenceClip, ...]:
        """Return the approved slots for the exact active-person cohort."""
        try:
            subject_ids = self._active_subject_ids()
            if (
                len(subject_ids) != 4
                or len(set(subject_ids)) != 4
                or any(not _positive_int(subject_id) for subject_id in subject_ids)
            ):
                raise ValueError("invalid active subject cohort")
            approvals = tuple(
                approval
                for subject_id in subject_ids
                for approval in self.list_candidates(subject_id)
            )
            identities = tuple(
                (item.subject_id, item.ordinal) for item in approvals
            )
            if (
                len(identities) != len(set(identities))
                or tuple(sorted(identities)) != identities
            ):
                raise ValueError("duplicate reference approval")
            return approvals
        except DomainError as cause:
            if cause.code == "VOICE_REFERENCE_STORED_INVALID":
                raise
            raise _stored_reference_invalid_error() from None
        except (sqlite3.DatabaseError, LookupError, TypeError, ValueError):
            raise _stored_reference_invalid_error() from None

    def calibrate(
        self, model_candidates: Sequence[RuntimeAttestation]
    ) -> CalibrationResult:
        if self._conn.in_transaction:
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            )
        candidates = self._model_candidates(model_candidates)
        approvals_by_subject = self._complete_approval_set()
        try:
            expected_prior_fingerprint = self._active_calibration_fingerprint()
        except Exception:
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            ) from None
        if self._media is None or self._scorer is None or self._retention is None:
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            )
        results: list[CalibrationResult] = []
        for model in candidates:
            try:
                result = self._evaluate_model(
                    model,
                    approvals_by_subject,
                    expected_prior_fingerprint,
                )
            except DomainError as cause:
                if cause.code == "VOICE_MODEL_NOT_SEPARABLE":
                    continue
                raise
            results.append(result)
        if not results:
            raise DomainError(
                "VOICE_MODEL_NOT_SEPARABLE", "voice model is not separable"
            )
        return min(
            results,
            key=lambda item: (
                -item.margin,
                item.dry_run_cpu_ms,
                item.model_name,
            ),
        )

    def activate_calibration(
        self, result: CalibrationResult
    ) -> CalibrationActivation:
        self._validate_activation_result(result)
        self._register_transition_authorizers()
        activation_error: DomainError | None = None
        try:
            try:
                with transaction(self._conn):
                    self._validate_activation_result(result)
                    now = _utc_datetime(self._clock())
                    replay = self._replayed_activation(result)
                    if replay is not None:
                        return replay
                    old_thresholds = tuple(
                        self._conn.execute(
                            "SELECT version, is_active "
                            "FROM speaker_threshold_configs "
                            "WHERE is_active=1 ORDER BY version"
                        )
                    )
                    old_profiles = tuple(
                        self._conn.execute(
                            "SELECT id, subject_id, is_active "
                            "FROM voice_reference_profiles "
                            "WHERE is_active=1 ORDER BY subject_id, id"
                        )
                    )
                    self._validate_old_activation(old_thresholds, old_profiles)
                    if (
                        self._active_calibration_fingerprint()
                        != result.expected_prior_fingerprint
                    ):
                        raise DomainError(
                            "VOICE_REFERENCE_ACTIVATION_STALE",
                            "voice reference activation is stale",
                        )
                    self._threshold_transitions = frozenset(
                        (row["version"], 1, 0) for row in old_thresholds
                    )
                    self._profile_transitions = frozenset(
                        (row["id"], row["subject_id"], 1, 0)
                        for row in old_profiles
                    )
                    if self._conn.execute(
                        "UPDATE speaker_threshold_configs "
                        "SET is_active=0 WHERE is_active=1"
                    ).rowcount != len(old_thresholds):
                        raise RuntimeError("threshold transition count mismatch")
                    if self._conn.execute(
                        "UPDATE voice_reference_profiles "
                        "SET is_active=0 WHERE is_active=1"
                    ).rowcount != len(old_profiles):
                        raise RuntimeError("profile transition count mismatch")
                    threshold_version = (
                        f"voice-calibration-{result.calibration_hash}"
                    )
                    calibration = calibrate_thresholds(
                        tuple(
                            CalibrationSample(
                                subject_id=subject.subject_id,
                                sample_kind="held_out_positive",
                                score=result.subject_boundary,
                            )
                            for subject in result.subjects
                        )
                        + tuple(
                            CalibrationSample(
                                subject_id=subject.subject_id,
                                sample_kind="negative",
                                score=result.interviewer_boundary,
                            )
                            for subject in result.subjects
                        )
                    )
                    self._speakers.add_threshold_config(
                        calibration.to_threshold_config(
                            version=threshold_version,
                            model_name=result.model_name,
                            model_version=result.model_version,
                        ),
                        now,
                        True,
                    )
                    profile_ids: list[tuple[int, int]] = []
                    for subject in result.subjects:
                        profile_id = self._voice.add_reference_profile(
                            subject_id=subject.subject_id,
                            model_name=result.model_name,
                            model_version=result.model_version,
                            adapter_version=result.adapter_version,
                            feature_hash=subject.feature.feature_sha256,
                            threshold_config_version=threshold_version,
                            created_at=now,
                            is_active=True,
                        )
                        profile_ids.append((subject.subject_id, profile_id))
                        for item in subject.clips:
                            approval = item.approval
                            self._voice.add_reference_clip(
                                profile_id,
                                approval.ordinal,
                                approval.clip_kind,
                                ReferenceClipCommand(
                                    subject_id=approval.subject_id,
                                    video_id=approval.video_id,
                                    start_ms=approval.start_ms,
                                    end_ms=approval.end_ms,
                                    actor=approval.actor,
                                    reason=approval.reason,
                                ),
                                normalized_audio_sha256=(
                                    item.normalized_audio_sha256
                                ),
                                approved_at=approval.approved_at,
                            )
                        self._voice.add_reference_feature(
                            profile_id,
                            encoding_version=subject.feature.encoding_version,
                            float_dtype=subject.feature.float_dtype,
                            dimension=subject.feature.dimension,
                            embedding_blob=subject.feature.embedding_blob,
                            created_at=now,
                        )
                    self._voice.add_calibration_identity(
                        StoredCalibrationIdentity(
                            calibration_hash=result.calibration_hash,
                            expected_prior_fingerprint=(
                                result.expected_prior_fingerprint
                            ),
                            threshold_config_version=threshold_version,
                            model_sha256=result.model_sha256,
                            feature_contract_hash=_feature_contract_hash(
                                result.subjects
                            ),
                            activated_at=now,
                        )
                    )
            except (
                DomainError,
                sqlite3.DatabaseError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as cause:
                if (
                    isinstance(cause, DomainError)
                    and cause.code == "VOICE_REFERENCE_ACTIVATION_STALE"
                ):
                    activation_error = DomainError(
                        "VOICE_REFERENCE_ACTIVATION_STALE",
                        "voice reference activation is stale",
                    )
                else:
                    activation_error = DomainError(
                        "VOICE_REFERENCE_ACTIVATION_FAILED",
                        "voice reference activation failed",
                    )
            if activation_error is not None:
                raise activation_error
        finally:
            self._threshold_transitions = frozenset()
            self._profile_transitions = frozenset()
        return CalibrationActivation(
            threshold_config_version=threshold_version,
            reference_profile_ids=tuple(profile_ids),
        )

    def _validate_command(self, command: object) -> None:
        if (
            type(command) is not ReferenceClipCommand
            or not _positive_int(command.subject_id)
            or not _positive_int(command.video_id)
            or type(command.start_ms) is not int
            or type(command.end_ms) is not int
            or command.start_ms < 0
            or command.start_ms > _SQLITE_INT_MAX
            or command.end_ms > _SQLITE_INT_MAX
            or command.start_ms >= command.end_ms
            or not 3_000 <= command.end_ms - command.start_ms <= 120_000
            or type(command.actor) is not str
            or command.actor != "local_user"
            or type(command.reason) is not str
            or not 1 <= len(command.reason) <= 240
            or not self._active_subject(command.subject_id)
            or not self._video_exists(command.video_id)
        ):
            _invalid_reference()
        try:
            validate_audit_reason(self._conn, command.reason)
        except DomainError:
            invalid_reason = True
        else:
            invalid_reason = False
        if invalid_reason:
            raise _invalid_reference_error()

    def _validate_slot(
        self,
        command: ReferenceClipCommand,
        existing: tuple[ApprovedReferenceClip, ...],
        clip_kind: str,
    ) -> None:
        expected_kind = _SLOT_KINDS[len(existing)]
        if clip_kind != expected_kind:
            _invalid_reference()
        if any(
            item.video_id == command.video_id
            and command.start_ms < item.end_ms
            and item.start_ms < command.end_ms
            for item in existing
        ):
            _invalid_reference()
        owners = self._candidate_subjects(command.video_id)
        if not owners:
            _invalid_reference()
        if clip_kind in {"enrollment", "held_out_positive"}:
            if command.subject_id not in owners:
                _invalid_reference()
            return
        if command.subject_id in owners or len(owners) != 1:
            _invalid_reference()
        negative_owners = {
            self._candidate_subjects(item.video_id)[0]
            for item in existing
            if item.clip_kind == "negative"
        }
        if owners[0] in negative_owners:
            _invalid_reference()

    def _approval_from_event(
        self, subject_id: int, expected_ordinal: int, event: object
    ) -> ApprovedReferenceClip:
        if expected_ordinal > len(_SLOT_KINDS):
            _stored_reference_invalid()
        after = getattr(event, "after", None)
        if (
            getattr(event, "entity_type", None) != _APPROVAL_ENTITY
            or getattr(event, "entity_id", None) != str(subject_id)
            or getattr(event, "scope_id", None) != subject_id
            or getattr(event, "operation", None) != "approve"
            or getattr(event, "actor_kind", None) != "user"
            or getattr(event, "reason_code", None)
            != "VOICE_REFERENCE_CLIP_APPROVED"
            or getattr(event, "before", object()) is not None
            or type(after) is not dict
            or set(after)
            != {
                "approval_hash",
                "end_ms",
                "ordinal",
                "role",
                "start_ms",
                "subject_id",
                "video_id",
            }
            or after["subject_id"] != subject_id
            or type(after["subject_id"]) is not int
            or not _positive_int(after["video_id"])
            or type(after["start_ms"]) is not int
            or type(after["end_ms"]) is not int
            or after["ordinal"] != expected_ordinal
            or type(after["ordinal"]) is not int
            or after["role"] != _SLOT_KINDS[expected_ordinal - 1]
            or not _hash(after["approval_hash"])
            or type(getattr(event, "reason_text", None)) is not str
        ):
            _stored_reference_invalid()
        approved_at = _utc_datetime(getattr(event, "created_at", None))
        command = ReferenceClipCommand(
            subject_id=subject_id,
            video_id=after["video_id"],
            start_ms=after["start_ms"],
            end_ms=after["end_ms"],
            actor="local_user",
            reason=event.reason_text,
        )
        self._validate_command(command)
        approval_hash = canonical_reference_approval_hash(
            subject_id=command.subject_id,
            video_id=command.video_id,
            start_ms=command.start_ms,
            end_ms=command.end_ms,
            ordinal=expected_ordinal,
            clip_kind=after["role"],
            actor=command.actor,
            reason=command.reason,
            approved_at=approved_at,
        )
        if approval_hash != after["approval_hash"]:
            _stored_reference_invalid()
        return ApprovedReferenceClip(
            subject_id=subject_id,
            video_id=command.video_id,
            start_ms=command.start_ms,
            end_ms=command.end_ms,
            ordinal=expected_ordinal,
            clip_kind=after["role"],
            actor=command.actor,
            reason=command.reason,
            approved_at=approved_at,
            approval_hash=approval_hash,
        )

    def _complete_approval_set(
        self,
    ) -> tuple[tuple[int, tuple[ApprovedReferenceClip, ...]], ...]:
        subject_ids = self._active_subject_ids()
        if len(subject_ids) != 4:
            _incomplete_reference()
        completed: list[tuple[int, tuple[ApprovedReferenceClip, ...]]] = []
        for subject_id in subject_ids:
            approvals = self.list_candidates(subject_id)
            if (
                len(approvals) != 6
                or tuple(item.clip_kind for item in approvals) != _SLOT_KINDS
                or sum(
                    item.end_ms - item.start_ms
                    for item in approvals
                    if item.clip_kind == "enrollment"
                )
                < 30_000
            ):
                _incomplete_reference()
            completed.append((subject_id, approvals))
        return tuple(completed)

    def _model_candidates(
        self, model_candidates: object
    ) -> tuple[RuntimeAttestation, ...]:
        if not isinstance(model_candidates, (tuple, list)):
            _invalid_model_candidates()
        candidates = tuple(model_candidates)
        if (
            len(candidates) != 2
            or any(type(item) is not RuntimeAttestation for item in candidates)
            or {item.model_name for item in candidates} != _MODEL_NAMES
            or any(not self._valid_attestation(item) for item in candidates)
        ):
            _invalid_model_candidates()
        return tuple(sorted(candidates, key=lambda item: item.model_name))

    def _valid_attestation(self, item: RuntimeAttestation) -> bool:
        try:
            if type(item.python_import_files) is not tuple:
                return False
            hashes = (
                item.python_sha256,
                *(digest for _, digest in item.python_import_files),
                item.python_startup_manifest_sha256,
                item.python_pyvenv_sha256,
                item.yt_dlp_sha256,
                item.deno_sha256,
                item.ffmpeg_sha256,
                item.model_sha256,
                item.vad_sha256,
                item.sherpa_wheel_sha256,
            )
            paths = (
                item.python_path,
                item.python_import_root,
                item.python_startup_manifest_path,
                item.python_pyvenv_path,
                item.yt_dlp_path,
                item.deno_path,
                item.ffmpeg_path,
                item.model_path,
                item.vad_path,
            )
            tokens = (
                item.python_version,
                item.yt_dlp_version,
                item.deno_version,
                item.ffmpeg_version,
                item.model_name,
                item.model_version,
                item.vad_version,
                item.adapter_contract_version,
                item.vad_contract_version,
                item.sherpa_onnx_version,
            )
            return (
                item.provider == "CPUExecutionProvider"
                and all(_hash(value) for value in hashes)
                and all(
                    isinstance(path, Path) and path.is_absolute()
                    for path in paths
                )
                and all(_token(value) for value in tokens)
                and all(
                    type(relative) is str
                    and relative
                    and not Path(relative).is_absolute()
                    for relative, _ in item.python_import_files
                )
            )
        except (TypeError, ValueError):
            return False

    def _evaluate_model(
        self,
        model: RuntimeAttestation,
        approvals_by_subject: tuple[
            tuple[int, tuple[ApprovedReferenceClip, ...]], ...
        ],
        expected_prior_fingerprint: str,
    ) -> CalibrationResult:
        artifact_ids: list[int] = []
        prepared: dict[
            int,
            tuple[tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...],
        ] = {}
        value: CalibrationResult | None = None
        failure: BaseException | None = None
        planned_paths: set[Path] = set()
        workspaces: list[
            tuple[PrivateJobWorkspace, tuple[Path, Path, Path]]
        ] = []
        registered_paths: set[Path] = set()
        try:
            for subject_id, approvals in approvals_by_subject:
                subject_audio: list[
                    tuple[ApprovedReferenceClip, PreparedReferenceAudio]
                ] = []
                for approval in approvals:
                    plan = self._media.plan(model, approval)  # type: ignore[union-attr]
                    self._validate_media_plan(
                        plan, planned_paths, model, approval
                    )
                    workspace = PrivateJobWorkspace.capture(
                        plan.source_path.parent
                    )
                    workspace.require_empty()
                    workspaces.append((workspace, plan.artifact_paths))
                    planned_paths.update(plan.artifact_paths)
                    for target in plan.artifact_paths:
                        artifact_id = self._artifacts.add_audio_artifact(
                            target, created_at=_utc_datetime(self._clock())
                        )
                        artifact_ids.append(artifact_id)
                        registered_paths.add(target)
                    audio = self._media.prepare(  # type: ignore[union-attr]
                        model, approval, plan
                    )
                    self._validate_prepared_audio(audio, plan)
                    subject_audio.append((approval, audio))
                prepared[subject_id] = tuple(subject_audio)
            features: dict[int, ReferenceFeatureData] = {}
            samples: list[CalibrationSample] = []
            calibrated_subjects: list[CalibratedSubject] = []
            for subject_id, _ in approvals_by_subject:
                subject_audio = prepared[subject_id]
                enrollment = tuple(
                    item
                    for item in subject_audio
                    if item[0].clip_kind == "enrollment"
                )
                feature = self._scorer.derive_enrollment_feature(
                    model, subject_id, enrollment
                )  # type: ignore[union-attr]
                self._validate_feature(feature, model.model_name)
                features[subject_id] = feature
                for approval, audio in subject_audio:
                    if approval.clip_kind == "enrollment":
                        continue
                    score = self._scorer.score(  # type: ignore[union-attr]
                        model, subject_id, feature, approval, audio
                    )
                    if type(score) not in {int, float} or not isfinite(score):
                        raise ValueError("invalid score")
                    samples.append(
                        CalibrationSample(
                            subject_id=subject_id,
                            sample_kind=approval.clip_kind,
                            score=float(score),
                        )
                    )
                calibrated_subjects.append(
                    CalibratedSubject(
                        subject_id=subject_id,
                        feature=feature,
                        clips=tuple(
                            CalibratedReferenceClip(
                                approval=approval,
                                normalized_audio_sha256=(
                                    audio.normalized_audio_sha256
                                ),
                            )
                            for approval, audio in subject_audio
                        ),
                    )
                )
            calibration = calibrate_thresholds(samples)
            dry_run_ms = self._scorer.dry_run(  # type: ignore[union-attr]
                model,
                tuple(features[key] for key in sorted(features)),
                candidate_count=20,
            )
            if type(dry_run_ms) is not int or dry_run_ms < 0:
                raise ValueError("invalid CPU time")
            subjects = tuple(calibrated_subjects)
            calibration_hash = _canonical_calibration_hash(
                model_name=model.model_name,
                model_version=model.model_version,
                adapter_version=model.adapter_contract_version,
                model_sha256=model.model_sha256,
                subject_boundary=calibration.subject_boundary,
                interviewer_boundary=calibration.interviewer_boundary,
                margin=calibration.margin,
                dry_run_cpu_ms=dry_run_ms,
                expected_prior_fingerprint=expected_prior_fingerprint,
                subjects=subjects,
            )
            value = CalibrationResult(
                model_name=model.model_name,
                model_version=model.model_version,
                adapter_version=model.adapter_contract_version,
                model_sha256=model.model_sha256,
                subject_boundary=calibration.subject_boundary,
                interviewer_boundary=calibration.interviewer_boundary,
                margin=calibration.margin,
                dry_run_cpu_ms=dry_run_ms,
                expected_prior_fingerprint=expected_prior_fingerprint,
                subjects=subjects,
                calibration_hash=calibration_hash,
            )
        except BaseException as cause:
            failure = cause
        reconciliation_failed = self._reconcile_unexpected_artifacts(
            tuple(workspaces), artifact_ids, registered_paths
        )
        cleanup_failed = self._cleanup_artifacts(tuple(artifact_ids))
        postcondition_failed = self._private_job_postcondition_failed(
            tuple(workspaces), registered_paths
        )
        directory_cleanup_failed = self._remove_private_job_directories(
            tuple(workspaces)
        )
        if (
            reconciliation_failed
            or cleanup_failed
            or postcondition_failed
            or directory_cleanup_failed
        ):
            raise DomainError(
                "VOICE_REFERENCE_CLEANUP_FAILED",
                "voice reference cleanup failed",
            )
        if failure is not None:
            if (
                isinstance(failure, DomainError)
                and failure.code == "VOICE_MODEL_NOT_SEPARABLE"
            ):
                raise failure
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise failure
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            )
        if value is None:
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            )
        return value

    def _cleanup_artifacts(self, artifact_ids: tuple[int, ...]) -> bool:
        failed = False
        for artifact_id in artifact_ids:
            try:
                result = self._retention.delete_audio(  # type: ignore[union-attr]
                    artifact_id
                )
                if (
                    type(result) is not AudioDeletionResult
                    or result.artifact_id != artifact_id
                    or result.deleted is not True
                    or result.retryable is not False
                    or result.error_code is not None
                ):
                    failed = True
            except BaseException as cause:
                if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
        return failed

    def _reconcile_unexpected_artifacts(
        self,
        workspaces: tuple[
            tuple[PrivateJobWorkspace, tuple[Path, Path, Path]], ...
        ],
        artifact_ids: list[int],
        registered_paths: set[Path],
    ) -> bool:
        failed = False
        for workspace, planned in workspaces:
            try:
                leaves = workspace.unexpected_leaves(planned)
            except BaseException as cause:
                if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
                continue
            for leaf in leaves:
                try:
                    workspace.remove_unregistered_leaf(leaf, planned)
                    continue
                except BaseException as cause:
                    if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                        raise
                try:
                    artifact_id = self._artifacts.add_audio_artifact(
                        leaf, created_at=_utc_datetime(self._clock())
                    )
                    artifact_ids.append(artifact_id)
                    registered_paths.add(leaf)
                except BaseException as cause:
                    if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                        raise
                    try:
                        workspace.remove_unregistered_leaf(leaf, planned)
                    except BaseException as removal_cause:
                        if isinstance(
                            removal_cause, (KeyboardInterrupt, SystemExit)
                        ):
                            raise
                        failed = True
        return failed

    def _private_job_postcondition_failed(
        self,
        workspaces: tuple[
            tuple[PrivateJobWorkspace, tuple[Path, Path, Path]], ...
        ],
        registered_paths: set[Path],
    ) -> bool:
        failed = False
        for workspace, planned in workspaces:
            try:
                leaves = workspace.unexpected_leaves(planned)
                if any(leaf not in registered_paths for leaf in leaves):
                    failed = True
            except BaseException as cause:
                if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
        return failed

    def _remove_private_job_directories(
        self,
        workspaces: tuple[
            tuple[PrivateJobWorkspace, tuple[Path, Path, Path]], ...
        ],
    ) -> bool:
        failed = False
        for workspace, _ in workspaces:
            try:
                workspace.remove_verified_empty_directories()
            except BaseException as cause:
                if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
        return failed

    def _validate_media_plan(
        self,
        value: object,
        planned_paths: set[Path],
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
    ) -> None:
        if (
            type(value) is not ReferenceMediaPlan
            or value.model_sha256 != model.model_sha256
            or value.approval_hash != approval.approval_hash
            or value.video_id != approval.video_id
            or any(
                not isinstance(path, Path) or not path.is_absolute()
                for path in value.artifact_paths
            )
            or tuple(path.name for path in value.artifact_paths)
            != ("source.media", "source.media.part", "normalized.wav")
            or len({path.parent for path in value.artifact_paths}) != 1
            or any(path in planned_paths for path in value.artifact_paths)
            or any(path.exists() for path in value.artifact_paths)
        ):
            raise ValueError("reference media plan is invalid")

    def _validate_prepared_audio(
        self, value: object, plan: ReferenceMediaPlan
    ) -> None:
        if (
            type(value) is not PreparedReferenceAudio
            or not isinstance(value.local_path, Path)
            or not value.local_path.is_absolute()
            or value.local_path != plan.normalized_path
            or not _positive_int(value.audio_duration_ms)
            or not _hash(value.normalized_audio_sha256)
        ):
            raise ValueError("prepared reference audio is invalid")

    def _validate_feature(
        self, value: object, model_name: str | None = None
    ) -> None:
        expected_dimension = {
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx": 192,
            "wespeaker_zh_cnceleb_resnet34.onnx": 256,
        }.get(model_name)
        if (
            type(value) is not ReferenceFeatureData
            or not _positive_int(value.subject_id)
            or value.encoding_version != "sherpa-speaker-embedding-v1"
            or value.float_dtype != "float32-le"
            or type(value.dimension) is not int
            or (
                expected_dimension is not None
                and value.dimension != expected_dimension
            )
            or type(value.embedding_blob) is not bytes
            or len(value.embedding_blob) != value.dimension * 4
            or not _hash(value.feature_sha256)
            or hashlib.sha256(value.embedding_blob).hexdigest()
            != value.feature_sha256
        ):
            raise ValueError("reference feature is invalid")

    def _validate_activation_result(self, value: object) -> None:
        invalid = False
        try:
            if (
                type(value) is not CalibrationResult
                or value.model_name not in _MODEL_NAMES
                or not _token(value.model_version)
                or not _token(value.adapter_version)
                or not _hash(value.model_sha256)
                or type(value.subject_boundary) is not float
                or type(value.interviewer_boundary) is not float
                or type(value.margin) is not float
                or not all(
                    isfinite(item)
                    for item in (
                        value.subject_boundary,
                        value.interviewer_boundary,
                        value.margin,
                    )
                )
                or value.subject_boundary <= value.interviewer_boundary
                or value.margin
                != value.subject_boundary - value.interviewer_boundary
                or type(value.dry_run_cpu_ms) is not int
                or value.dry_run_cpu_ms < 0
                or not _hash(value.expected_prior_fingerprint)
                or type(value.subjects) is not tuple
                or len(value.subjects) != 4
                or not _hash(value.calibration_hash)
            ):
                raise ValueError("calibration result")
            active_subjects = self._active_subject_ids()
            if tuple(item.subject_id for item in value.subjects) != active_subjects:
                raise ValueError("calibration subjects")
            for subject in value.subjects:
                if (
                    type(subject) is not CalibratedSubject
                    or type(subject.subject_id) is not int
                    or type(subject.clips) is not tuple
                    or len(subject.clips) != 6
                    or subject.feature.subject_id != subject.subject_id
                ):
                    raise ValueError("calibrated subject")
                self._validate_feature(subject.feature, value.model_name)
                approvals = self.list_candidates(subject.subject_id)
                if tuple(item.approval for item in subject.clips) != approvals:
                    raise ValueError("calibration approvals")
                for item in subject.clips:
                    if (
                        type(item) is not CalibratedReferenceClip
                        or not _hash(item.normalized_audio_sha256)
                    ):
                        raise ValueError("calibrated clip")
            expected_hash = _canonical_calibration_hash(
                model_name=value.model_name,
                model_version=value.model_version,
                adapter_version=value.adapter_version,
                model_sha256=value.model_sha256,
                subject_boundary=value.subject_boundary,
                interviewer_boundary=value.interviewer_boundary,
                margin=value.margin,
                dry_run_cpu_ms=value.dry_run_cpu_ms,
                expected_prior_fingerprint=value.expected_prior_fingerprint,
                subjects=value.subjects,
            )
            if expected_hash != value.calibration_hash:
                raise ValueError("calibration hash")
        except (AttributeError, DomainError, TypeError, ValueError):
            invalid = True
        if invalid:
            raise DomainError(
                "VOICE_REFERENCE_ACTIVATION_INVALID",
                "voice reference activation input is invalid",
            )

    def _validate_old_activation(
        self,
        old_thresholds: tuple[sqlite3.Row, ...],
        old_profiles: tuple[sqlite3.Row, ...],
    ) -> None:
        if len(old_thresholds) > 1 or len(old_profiles) not in {0, 4}:
            raise ValueError("old activation state")
        if old_profiles and (
            len(old_thresholds) != 1
            or tuple(row["subject_id"] for row in old_profiles)
            != self._active_subject_ids()
        ):
            raise ValueError("old activation ownership")

    def _active_calibration_fingerprint(self) -> str:
        thresholds = tuple(
            self._conn.execute(
                """
                SELECT version, model_name, model_version, subject_operator,
                       subject_boundary, interviewer_operator,
                       interviewer_boundary, is_active
                FROM speaker_threshold_configs
                WHERE is_active=1 ORDER BY version
                """
            )
        )
        profile_rows = tuple(
            self._conn.execute(
                "SELECT id, subject_id, is_active FROM voice_reference_profiles "
                "WHERE is_active=1 ORDER BY subject_id, id"
            )
        )
        self._validate_old_activation(thresholds, profile_rows)
        bundles = tuple(
            self._voice.get_reference_bundle(row["id"]) for row in profile_rows
        )
        if bundles:
            threshold = thresholds[0]
            expected_subjects = self._active_subject_ids()
            if (
                tuple(bundle.subject_id for bundle in bundles)
                != expected_subjects
                or any(not bundle.is_active for bundle in bundles)
                or any(
                    bundle.threshold_config_version != threshold["version"]
                    or bundle.model_name != threshold["model_name"]
                    or bundle.model_version != threshold["model_version"]
                    or len(bundle.clips) != 6
                    or tuple(item.ordinal for item in bundle.clips)
                    != tuple(range(1, 7))
                    for bundle in bundles
                )
            ):
                raise ValueError("active reference bundle state is invalid")
        return sha256_text(
            canonical_json(
                {
                    "bundles": [
                        {
                            "adapter_version": bundle.adapter_version,
                            "clips": [item.clip_hash for item in bundle.clips],
                            "feature": {
                                "dimension": bundle.feature.dimension,
                                "encoding_version": bundle.feature.encoding_version,
                                "feature_sha256": bundle.feature.feature_sha256,
                                "float_dtype": bundle.feature.float_dtype,
                            },
                            "model_name": bundle.model_name,
                            "model_version": bundle.model_version,
                            "profile_id": bundle.reference_profile_id,
                            "subject_id": bundle.subject_id,
                            "threshold_config_version": (
                                bundle.threshold_config_version
                            ),
                        }
                        for bundle in bundles
                    ],
                    "schema": "voice-reference-active-state.v1",
                    "thresholds": [
                        {
                            key: row[key]
                            for key in (
                                "interviewer_boundary",
                                "interviewer_operator",
                                "model_name",
                                "model_version",
                                "subject_boundary",
                                "subject_operator",
                                "version",
                            )
                        }
                        for row in thresholds
                    ],
                }
            )
        )

    def _replayed_activation(
        self, result: CalibrationResult
    ) -> CalibrationActivation | None:
        identity = self._voice.find_calibration_identity(
            result.calibration_hash
        )
        if identity is None:
            return None
        if (
            identity.expected_prior_fingerprint
            != result.expected_prior_fingerprint
            or identity.model_sha256 != result.model_sha256
            or identity.feature_contract_hash
            != _feature_contract_hash(result.subjects)
        ):
            raise DomainError(
                "VOICE_REFERENCE_ACTIVATION_STALE",
                "voice reference activation is stale",
            )
        threshold_version = identity.threshold_config_version
        active_thresholds = tuple(
            self._conn.execute(
                "SELECT version, model_name, model_version, subject_operator, "
                "subject_boundary, interviewer_operator, "
                "interviewer_boundary, created_at, is_active "
                "FROM speaker_threshold_configs "
                "WHERE is_active=1 ORDER BY version"
            )
        )
        active = tuple(
            self._conn.execute(
                "SELECT id, subject_id FROM voice_reference_profiles "
                "WHERE is_active=1 AND threshold_config_version=? "
                "ORDER BY subject_id, id",
                (threshold_version,),
            )
        )
        active_ids = tuple(row["id"] for row in active)
        if (
            len(active_thresholds) != 1
            or active_thresholds[0]["version"] != threshold_version
            or threshold_version
            != f"voice-calibration-{result.calibration_hash}"
            or active_thresholds[0]["model_name"] != result.model_name
            or active_thresholds[0]["model_version"] != result.model_version
            or active_thresholds[0]["subject_operator"] != "gte"
            or active_thresholds[0]["subject_boundary"]
            != result.subject_boundary
            or active_thresholds[0]["interviewer_operator"] != "lte"
            or active_thresholds[0]["interviewer_boundary"]
            != result.interviewer_boundary
            or active_thresholds[0]["created_at"]
            != utc_iso(identity.activated_at)
            or active_thresholds[0]["is_active"] != 1
            or len(active) != 4
            or active_ids != self._voice.list_active_reference_profile_ids()
        ):
            raise DomainError(
                "VOICE_REFERENCE_ACTIVATION_STALE",
                "voice reference activation is stale",
            )
        for expected, stored in zip(result.subjects, active, strict=True):
            bundle = self._voice.get_reference_bundle(stored["id"])
            if (
                stored["subject_id"] != expected.subject_id
                or bundle.model_name != result.model_name
                or bundle.model_version != result.model_version
                or bundle.adapter_version != result.adapter_version
                or bundle.feature_hash != expected.feature.feature_sha256
                or bundle.feature.encoding_version
                != expected.feature.encoding_version
                or bundle.feature.float_dtype != expected.feature.float_dtype
                or bundle.feature.dimension != expected.feature.dimension
                or bundle.feature.embedding_blob != expected.feature.embedding_blob
                or tuple(
                    (
                        item.ordinal,
                        item.clip_kind,
                        item.subject_id,
                        item.video_id,
                        item.start_ms,
                        item.end_ms,
                        item.normalized_audio_sha256,
                        item.approval_actor,
                        item.approval_reason,
                        item.approved_at,
                    )
                    for item in bundle.clips
                )
                != tuple(
                    (
                        item.approval.ordinal,
                        item.approval.clip_kind,
                        item.approval.subject_id,
                        item.approval.video_id,
                        item.approval.start_ms,
                        item.approval.end_ms,
                        item.normalized_audio_sha256,
                        item.approval.actor,
                        item.approval.reason,
                        item.approval.approved_at,
                    )
                    for item in expected.clips
                )
            ):
                raise DomainError(
                    "VOICE_REFERENCE_ACTIVATION_STALE",
                    "voice reference activation is stale",
                )
        return CalibrationActivation(
            threshold_config_version=threshold_version,
            reference_profile_ids=tuple(
                (row["subject_id"], row["id"]) for row in active
            ),
        )

    def _active_subject(self, subject_id: int) -> bool:
        return (
            self._conn.execute(
                """
                SELECT 1
                FROM analysis_subjects AS subject
                JOIN discovery_profiles AS profile
                  ON profile.subject_id=subject.id
                WHERE subject.id=? AND subject.is_active=1
                  AND profile.is_active=1
                """,
                (subject_id,),
            ).fetchone()
            is not None
        )

    def _active_subject_ids(self) -> tuple[int, ...]:
        return tuple(
            row["subject_id"]
            for row in self._conn.execute(
                """
                SELECT subject.id AS subject_id
                FROM analysis_subjects AS subject
                JOIN discovery_profiles AS profile
                  ON profile.subject_id=subject.id
                WHERE subject.is_active=1 AND profile.is_active=1
                ORDER BY subject.id
                """
            )
        )

    def _video_exists(self, video_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM videos WHERE id=?", (video_id,)
        ).fetchone() is not None

    def _candidate_subjects(self, video_id: int) -> tuple[int, ...]:
        return tuple(
            row["subject_id"]
            for row in self._conn.execute(
                """
                SELECT DISTINCT profile.subject_id
                FROM subject_video_candidates AS candidate
                JOIN discovery_profiles AS profile
                  ON profile.id=candidate.profile_id
                JOIN analysis_subjects AS subject
                  ON subject.id=profile.subject_id
                WHERE candidate.video_id=? AND profile.is_active=1
                  AND subject.is_active=1
                ORDER BY profile.subject_id
                """,
                (video_id,),
            )
        )

    def _register_transition_authorizers(self) -> None:
        self._conn.create_function(
            "voice_reference_threshold_transition_authorized",
            3,
            lambda version, old, new: int(
                (version, old, new) in self._threshold_transitions
            ),
        )
        self._conn.create_function(
            "voice_reference_profile_transition_authorized",
            4,
            lambda row_id, subject_id, old, new: int(
                (row_id, subject_id, old, new) in self._profile_transitions
            ),
        )


def _canonical_calibration_hash(
    *,
    model_name: str,
    model_version: str,
    adapter_version: str,
    model_sha256: str,
    subject_boundary: float,
    interviewer_boundary: float,
    margin: float,
    dry_run_cpu_ms: int,
    expected_prior_fingerprint: str,
    subjects: tuple[CalibratedSubject, ...],
) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": adapter_version,
                "dry_run_cpu_ms": dry_run_cpu_ms,
                "expected_prior_fingerprint": expected_prior_fingerprint,
                "interviewer_boundary": interviewer_boundary,
                "margin": margin,
                "model_name": model_name,
                "model_sha256": model_sha256,
                "model_version": model_version,
                "schema": "voice-reference-calibration.v1",
                "subject_boundary": subject_boundary,
                "subjects": [
                    {
                        "clips": [
                            {
                                "approval_hash": item.approval.approval_hash,
                                "normalized_audio_sha256": (
                                    item.normalized_audio_sha256
                                ),
                            }
                            for item in subject.clips
                        ],
                        "feature": {
                            "dimension": subject.feature.dimension,
                            "encoding_version": subject.feature.encoding_version,
                            "feature_sha256": subject.feature.feature_sha256,
                            "float_dtype": subject.feature.float_dtype,
                            "subject_id": subject.feature.subject_id,
                        },
                        "subject_id": subject.subject_id,
                    }
                    for subject in subjects
                ],
            }
        )
    )


def _reference_model_identity(model: RuntimeAttestation) -> dict[str, object]:
    return {
        "adapter_contract_version": model.adapter_contract_version,
        "model_name": model.model_name,
        "model_path": str(model.model_path),
        "model_sha256": model.model_sha256,
        "model_version": model.model_version,
    }


def _feature_contract_hash(subjects: tuple[CalibratedSubject, ...]) -> str:
    return canonical_reference_feature_contract_hash(
        tuple(
            (
                item.feature.subject_id,
                item.feature.encoding_version,
                item.feature.float_dtype,
                item.feature.dimension,
                item.feature.embedding_blob,
                item.feature.feature_sha256,
            )
            for item in subjects
        )
    )


def _reference_audio(
    approval: ApprovedReferenceClip, audio: PreparedReferenceAudio
) -> ReferenceAudioInput:
    return ReferenceAudioInput(
        approval_hash=approval.approval_hash,
        audio_duration_ms=audio.audio_duration_ms,
        audio_path=str(audio.local_path),
        audio_sha256=audio.normalized_audio_sha256,
        clip_kind=approval.clip_kind,
        end_ms=approval.end_ms,
        ordinal=approval.ordinal,
        start_ms=approval.start_ms,
        subject_id=approval.subject_id,
        video_id=approval.video_id,
    )


def _reference_feature(feature: ReferenceFeatureData) -> ReferenceFeatureInput:
    return ReferenceFeatureInput.from_bytes(
        encoding_version=feature.encoding_version,
        float_dtype=feature.float_dtype,
        dimension=feature.dimension,
        embedding_blob=feature.embedding_blob,
        subject_id=feature.subject_id,
    )


def _positive_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _SQLITE_INT_MAX


def _hash(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _utc_datetime(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError("UTC time is invalid")
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("UTC time is invalid")
    return value


def _invalid_reference_error() -> DomainError:
    return DomainError(
        "VOICE_REFERENCE_INVALID", "voice reference is invalid"
    )


def _invalid_reference() -> None:
    raise _invalid_reference_error()


def _stored_reference_invalid_error() -> DomainError:
    return DomainError(
        "VOICE_REFERENCE_STORED_INVALID",
        "stored voice reference approval is invalid",
    )


def _stored_reference_invalid() -> None:
    raise _stored_reference_invalid_error()


def _incomplete_reference() -> None:
    raise DomainError(
        "VOICE_REFERENCE_INCOMPLETE", "voice reference enrollment is incomplete"
    )


def _invalid_model_candidates() -> None:
    raise DomainError(
        "VOICE_MODEL_CANDIDATES_INVALID", "voice model candidates are invalid"
    )
