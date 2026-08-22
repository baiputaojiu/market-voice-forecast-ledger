"""Approve, calibrate, and atomically activate private voice references."""

import hashlib
import re
import sqlite3
import time
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
    VoiceVerificationRepository,
)
from market_voice_forecast_ledger.services.audit import validate_audit_reason
from market_voice_forecast_ledger.services.retention import AudioDeletionResult
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
    normalized_audio_sha256: str


@dataclass(frozen=True, slots=True)
class ReferenceFeatureData:
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
    subjects: tuple[CalibratedSubject, ...]
    calibration_hash: str


@dataclass(frozen=True, slots=True)
class CalibrationActivation:
    threshold_config_version: str
    reference_profile_ids: tuple[tuple[int, int], ...]


class ReferenceMedia(Protocol):
    def prepare(
        self,
        model: RuntimeAttestation,
        approval: ApprovedReferenceClip,
    ) -> PreparedReferenceAudio: ...


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
    ) -> None: ...


class AudioRetention(Protocol):
    def delete_audio(self, artifact_id: int) -> AudioDeletionResult: ...


CpuTimer = Callable[[str, Callable[[], None]], int]


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
        cpu_timer: CpuTimer | None = None,
    ) -> None:
        self._conn = conn
        self._media = media
        self._scorer = scorer
        self._retention = retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cpu_timer = cpu_timer or _measure_cpu_ms
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
                raise
            raise _invalid_reference_error() from cause
        except (sqlite3.DatabaseError, RuntimeError, TypeError, ValueError) as cause:
            raise _invalid_reference_error() from cause
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
                raise
            raise _stored_reference_invalid_error() from cause
        except (sqlite3.DatabaseError, LookupError, TypeError, ValueError) as cause:
            raise _stored_reference_invalid_error() from cause

    def calibrate(
        self, model_candidates: Sequence[RuntimeAttestation]
    ) -> CalibrationResult:
        candidates = self._model_candidates(model_candidates)
        approvals_by_subject = self._complete_approval_set()
        if self._media is None or self._scorer is None or self._retention is None:
            raise DomainError(
                "VOICE_REFERENCE_CALIBRATION_FAILED",
                "voice reference calibration failed",
            )
        results: list[CalibrationResult] = []
        for model in candidates:
            try:
                result = self._evaluate_model(model, approvals_by_subject)
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
        try:
            try:
                with transaction(self._conn):
                    self._validate_activation_result(result)
                    now = _utc_datetime(self._clock())
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
                    threshold_version = self._next_threshold_version()
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
                        cursor = self._conn.execute(
                            """
                            INSERT INTO voice_reference_profiles(
                                subject_id, model_name, model_version,
                                adapter_version, feature_hash,
                                threshold_config_version, created_at, is_active
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                subject.subject_id,
                                result.model_name,
                                result.model_version,
                                result.adapter_version,
                                subject.feature.feature_sha256,
                                threshold_version,
                                utc_iso(now),
                            ),
                        )
                        profile_id = _lastrowid(cursor)
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
            except (
                DomainError,
                sqlite3.DatabaseError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as cause:
                raise DomainError(
                    "VOICE_REFERENCE_ACTIVATION_FAILED",
                    "voice reference activation failed",
                ) from cause
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
        except DomainError as cause:
            raise _invalid_reference_error() from cause

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
    ) -> CalibrationResult:
        artifact_ids: list[int] = []
        prepared: dict[
            int,
            tuple[tuple[ApprovedReferenceClip, PreparedReferenceAudio], ...],
        ] = {}
        value: CalibrationResult | None = None
        failure: BaseException | None = None
        try:
            for subject_id, approvals in approvals_by_subject:
                subject_audio: list[
                    tuple[ApprovedReferenceClip, PreparedReferenceAudio]
                ] = []
                for approval in approvals:
                    audio = self._media.prepare(model, approval)  # type: ignore[union-attr]
                    self._validate_prepared_audio(audio)
                    artifact_id = self._artifacts.add_audio_artifact(
                        audio.local_path, created_at=_utc_datetime(self._clock())
                    )
                    artifact_ids.append(artifact_id)
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
                feature = self._scorer.derive_enrollment_feature(  # type: ignore[union-attr]
                    model, subject_id, enrollment
                )
                self._validate_feature(feature)
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
            dry_run_ms = self._cpu_timer(
                model.model_name,
                lambda: self._scorer.dry_run(  # type: ignore[union-attr]
                    model,
                    tuple(features[key] for key in sorted(features)),
                    candidate_count=20,
                ),
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
                subjects=subjects,
                calibration_hash=calibration_hash,
            )
        except BaseException as cause:
            failure = cause
        cleanup_failed = self._cleanup_artifacts(tuple(artifact_ids))
        if cleanup_failed:
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
            ) from failure
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
                result = self._retention.delete_audio(artifact_id)  # type: ignore[union-attr]
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

    def _validate_prepared_audio(self, value: object) -> None:
        if (
            type(value) is not PreparedReferenceAudio
            or not isinstance(value.local_path, Path)
            or not value.local_path.is_absolute()
            or not _hash(value.normalized_audio_sha256)
        ):
            raise ValueError("prepared reference audio is invalid")

    def _validate_feature(self, value: object) -> None:
        if (
            type(value) is not ReferenceFeatureData
            or not _token(value.encoding_version)
            or value.float_dtype not in {"float32", "float64"}
            or type(value.dimension) is not int
            or value.dimension <= 0
            or type(value.embedding_blob) is not bytes
            or len(value.embedding_blob)
            != value.dimension * (4 if value.float_dtype == "float32" else 8)
            or not _hash(value.feature_sha256)
            or hashlib.sha256(value.embedding_blob).hexdigest()
            != value.feature_sha256
        ):
            raise ValueError("reference feature is invalid")

    def _validate_activation_result(self, value: object) -> None:
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
                ):
                    raise ValueError("calibrated subject")
                self._validate_feature(subject.feature)
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
                subjects=value.subjects,
            )
            if expected_hash != value.calibration_hash:
                raise ValueError("calibration hash")
        except (AttributeError, DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "VOICE_REFERENCE_ACTIVATION_INVALID",
                "voice reference activation input is invalid",
            ) from cause

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

    def _next_threshold_version(self) -> str:
        number = self._conn.execute(
            "SELECT COUNT(*) FROM speaker_threshold_configs"
        ).fetchone()[0] + 1
        while True:
            version = f"voice-calibration-v{number}"
            if self._conn.execute(
                "SELECT 1 FROM speaker_threshold_configs WHERE version=?",
                (version,),
            ).fetchone() is None:
                return version
            number += 1

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
    subjects: tuple[CalibratedSubject, ...],
) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": adapter_version,
                "dry_run_cpu_ms": dry_run_cpu_ms,
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
                        "feature_sha256": subject.feature.feature_sha256,
                        "subject_id": subject.subject_id,
                    }
                    for subject in subjects
                ],
            }
        )
    )


def _measure_cpu_ms(_: str, operation: Callable[[], None]) -> int:
    started = time.process_time_ns()
    operation()
    return (time.process_time_ns() - started) // 1_000_000


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


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


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if type(cursor.lastrowid) is not int or cursor.lastrowid <= 0:
        raise RuntimeError("voice reference insert did not return an id")
    return cursor.lastrowid


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
