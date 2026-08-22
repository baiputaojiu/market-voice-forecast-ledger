import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Callable

from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.discovery import (
    PresenceOrigin,
    PresenceState,
    canonical_presence_decision_hash,
)
from market_voice_forecast_ledger.domain.enums import JobKind, JobStage, JobStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, ManifestUnit
from market_voice_forecast_ledger.domain.voice_verification import (
    ReferenceClipCommand,
    ReviewAction,
    VoiceManifestSnapshot,
    VoiceProposal,
    VoiceRunResult,
    VoiceSegmentScore,
    build_presence_job_manifest,
)
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.services.audit import validate_audit_reason


_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_CANONICAL_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_RESULT_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CLIP_KINDS = frozenset({"enrollment", "held_out_positive", "negative"})
_RUNNABLE_STATUSES = frozenset({JobStatus.QUEUED.value, JobStatus.RETRYING.value})
_JOB_STATUSES = frozenset(status.value for status in JobStatus)


@dataclass(frozen=True, slots=True)
class StoredReferenceClip:
    id: int
    reference_profile_id: int
    ordinal: int
    clip_kind: str
    subject_id: int
    video_id: int
    start_ms: int
    end_ms: int
    normalized_audio_sha256: str
    approval_actor: str
    approval_reason: str
    approved_at: datetime
    clip_hash: str


@dataclass(frozen=True, slots=True)
class StoredReferenceFeature:
    id: int
    reference_profile_id: int
    encoding_version: str
    float_dtype: str
    dimension: int
    embedding_blob: bytes
    feature_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReferenceBundle:
    reference_profile_id: int
    subject_id: int
    model_name: str
    model_version: str
    adapter_version: str
    feature_hash: str
    threshold_config_version: str
    created_at: datetime
    is_active: bool
    clips: tuple[StoredReferenceClip, ...]
    feature: StoredReferenceFeature


@dataclass(frozen=True, slots=True)
class StoredVoiceManifest:
    id: int
    job_id: int
    snapshot: VoiceManifestSnapshot
    manifest_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredVoiceRun:
    id: int
    result: VoiceRunResult
    segments: tuple[VoiceSegmentScore, ...]
    completed_at: datetime

    @property
    def job_id(self) -> int:
        return self.result.job_id

    @property
    def candidate_id(self) -> int:
        return self.result.candidate_id

    @property
    def input_hash(self) -> str:
        return self.result.input_hash

    @property
    def output_hash(self) -> str:
        return self.result.output_hash

    @property
    def proposal(self) -> VoiceProposal:
        return self.result.proposal

    @property
    def result_code(self) -> str:
        return self.result.result_code


@dataclass(frozen=True, slots=True)
class VoiceJobArtifacts:
    manifest: StoredVoiceManifest
    reference: ReferenceBundle
    run: StoredVoiceRun | None


def canonical_voice_clip_hash(
    *,
    reference_profile_id: int,
    ordinal: int,
    clip_kind: str,
    subject_id: int,
    video_id: int,
    start_ms: int,
    end_ms: int,
    normalized_audio_sha256: str,
    approval_actor: str,
    approval_reason: str,
    approved_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "approval_actor": approval_actor,
                "approval_reason": approval_reason,
                "approved_at": utc_iso(approved_at),
                "clip_kind": clip_kind,
                "end_ms": end_ms,
                "normalized_audio_sha256": normalized_audio_sha256,
                "ordinal": ordinal,
                "reference_profile_id": reference_profile_id,
                "schema": "voice-reference-clip.v1",
                "start_ms": start_ms,
                "subject_id": subject_id,
                "video_id": video_id,
            }
        )
    )


def canonical_voice_segment_hash(
    snapshot: VoiceManifestSnapshot,
    input_hash: str,
    *,
    ordinal: int,
    start_ms: int,
    end_ms: int,
    raw_match_score: float,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": snapshot.adapter_version,
                "end_ms": end_ms,
                "input_hash": input_hash,
                "model_name": snapshot.model_name,
                "model_version": snapshot.model_version,
                "ordinal": ordinal,
                "raw_match_score": raw_match_score,
                "schema": "voice-verification-segment.v1",
                "start_ms": start_ms,
                "threshold_config_version": snapshot.threshold_config_version,
                "vad_contract_version": snapshot.vad_contract_version,
            }
        )
    )


def canonical_voice_run_output_hash(
    snapshot: VoiceManifestSnapshot,
    input_hash: str,
    proposal: VoiceProposal,
    segments: tuple[VoiceSegmentScore, ...],
) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": snapshot.adapter_version,
                "input_hash": input_hash,
                "model_name": snapshot.model_name,
                "model_version": snapshot.model_version,
                "proposal": proposal.value,
                "schema": "voice-verification-output.v1",
                "segments": [
                    {
                        "end_ms": segment.end_ms,
                        "evidence_hash": segment.evidence_hash,
                        "ordinal": segment.ordinal,
                        "raw_match_score": segment.raw_match_score,
                        "start_ms": segment.start_ms,
                    }
                    for segment in segments
                ],
                "threshold_config_version": snapshot.threshold_config_version,
                "vad_contract_version": snapshot.vad_contract_version,
            }
        )
    )


class VoiceVerificationRepository:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def add_reference_clip(
        self,
        reference_profile_id: int,
        ordinal: int,
        clip_kind: str,
        command: ReferenceClipCommand,
        *,
        normalized_audio_sha256: str,
        approved_at: datetime,
    ) -> int:
        if (
            type(reference_profile_id) is not int
            or reference_profile_id <= 0
            or type(ordinal) is not int
            or ordinal <= 0
            or type(clip_kind) is not str
            or clip_kind not in _CLIP_KINDS
            or type(command) is not ReferenceClipCommand
            or type(command.subject_id) is not int
            or command.subject_id <= 0
            or type(command.video_id) is not int
            or command.video_id <= 0
            or type(command.start_ms) is not int
            or type(command.end_ms) is not int
            or command.start_ms < 0
            or command.start_ms >= command.end_ms
            or type(command.actor) is not str
            or command.actor != "local_user"
            or not _is_hash(normalized_audio_sha256)
            or not _is_exact_utc(approved_at)
        ):
            _invalid_reference()
        self._validate_reason(command.reason, "VOICE_REFERENCE_INVALID")
        profile = self._read_reference_profile(reference_profile_id)
        if profile["subject_id"] != command.subject_id:
            _invalid_reference()
        if self._conn.execute(
            "SELECT 1 FROM videos WHERE id=?", (command.video_id,)
        ).fetchone() is None:
            _invalid_reference()
        stored = tuple(
            self._conn.execute(
                "SELECT * FROM voice_reference_clips "
                "WHERE reference_profile_id=? ORDER BY ordinal",
                (reference_profile_id,),
            )
        )
        if any(row["ordinal"] != index for index, row in enumerate(stored, 1)):
            _stored_reference_invalid()
        if ordinal != len(stored) + 1:
            _invalid_reference()
        clip_hash = canonical_voice_clip_hash(
            reference_profile_id=reference_profile_id,
            ordinal=ordinal,
            clip_kind=clip_kind,
            subject_id=command.subject_id,
            video_id=command.video_id,
            start_ms=command.start_ms,
            end_ms=command.end_ms,
            normalized_audio_sha256=normalized_audio_sha256,
            approval_actor=command.actor,
            approval_reason=command.reason,
            approved_at=approved_at,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO voice_reference_clips(
                reference_profile_id, ordinal, clip_kind, subject_id, video_id,
                start_ms, end_ms, normalized_audio_sha256, approval_actor,
                approval_reason, approved_at, clip_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference_profile_id,
                ordinal,
                clip_kind,
                command.subject_id,
                command.video_id,
                command.start_ms,
                command.end_ms,
                normalized_audio_sha256,
                command.actor,
                command.reason,
                utc_iso(approved_at),
                clip_hash,
            ),
        )
        return _lastrowid(cursor)

    def add_reference_feature(
        self,
        reference_profile_id: int,
        *,
        encoding_version: str,
        float_dtype: str,
        dimension: int,
        embedding_blob: bytes,
        created_at: datetime,
    ) -> int:
        if (
            type(reference_profile_id) is not int
            or reference_profile_id <= 0
            or not _is_token(encoding_version)
            or float_dtype not in {"float32", "float64"}
            or type(dimension) is not int
            or dimension <= 0
            or type(embedding_blob) is not bytes
            or len(embedding_blob)
            != dimension * (4 if float_dtype == "float32" else 8)
            or not _is_exact_utc(created_at)
        ):
            _invalid_reference()
        profile = self._read_reference_profile(reference_profile_id)
        feature_hash = hashlib.sha256(embedding_blob).hexdigest()
        if profile["feature_hash"] != feature_hash:
            _invalid_reference()
        if self._conn.execute(
            "SELECT 1 FROM voice_reference_features WHERE reference_profile_id=?",
            (reference_profile_id,),
        ).fetchone() is not None:
            _invalid_reference()
        cursor = self._conn.execute(
            """
            INSERT INTO voice_reference_features(
                reference_profile_id, encoding_version, float_dtype, dimension,
                embedding_blob, feature_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference_profile_id,
                encoding_version,
                float_dtype,
                dimension,
                sqlite3.Binary(embedding_blob),
                feature_hash,
                utc_iso(created_at),
            ),
        )
        return _lastrowid(cursor)

    def get_reference_bundle(self, reference_profile_id: int) -> ReferenceBundle:
        if type(reference_profile_id) is not int or reference_profile_id <= 0:
            _stored_reference_invalid()
        try:
            profile = self._read_reference_profile(reference_profile_id)
            feature_row = self._conn.execute(
                """
                SELECT *,
                       typeof(id) AS type_id,
                       typeof(reference_profile_id) AS type_reference_profile_id,
                       typeof(encoding_version) AS type_encoding_version,
                       typeof(float_dtype) AS type_float_dtype,
                       typeof(dimension) AS type_dimension,
                       typeof(embedding_blob) AS type_embedding_blob,
                       typeof(feature_sha256) AS type_feature_sha256,
                       typeof(created_at) AS type_created_at
                FROM voice_reference_features
                WHERE reference_profile_id=?
                """,
                (reference_profile_id,),
            ).fetchone()
            if feature_row is None:
                raise ValueError("feature missing")
            feature = self._feature_from_row(feature_row, profile)
            clip_rows = tuple(
                self._conn.execute(
                    """
                    SELECT *,
                           typeof(id) AS type_id,
                           typeof(reference_profile_id) AS type_reference_profile_id,
                           typeof(ordinal) AS type_ordinal,
                           typeof(clip_kind) AS type_clip_kind,
                           typeof(subject_id) AS type_subject_id,
                           typeof(video_id) AS type_video_id,
                           typeof(start_ms) AS type_start_ms,
                           typeof(end_ms) AS type_end_ms,
                           typeof(normalized_audio_sha256) AS type_audio_hash,
                           typeof(approval_actor) AS type_approval_actor,
                           typeof(approval_reason) AS type_approval_reason,
                           typeof(approved_at) AS type_approved_at,
                           typeof(clip_hash) AS type_clip_hash
                    FROM voice_reference_clips
                    WHERE reference_profile_id=?
                    ORDER BY ordinal
                    """,
                    (reference_profile_id,),
                )
            )
            if not clip_rows:
                raise ValueError("clips missing")
            clips = tuple(
                self._clip_from_row(row, profile, expected_ordinal=ordinal)
                for ordinal, row in enumerate(clip_rows, start=1)
            )
        except (DomainError, LookupError, TypeError, ValueError) as cause:
            if (
                isinstance(cause, DomainError)
                and cause.code == "VOICE_REFERENCE_STORED_INVALID"
            ):
                raise
            raise DomainError(
                "VOICE_REFERENCE_STORED_INVALID",
                "VOICE_REFERENCE_STORED_INVALID: stored voice reference is invalid",
            ) from cause
        return ReferenceBundle(
            reference_profile_id=profile["id"],
            subject_id=profile["subject_id"],
            model_name=profile["model_name"],
            model_version=profile["model_version"],
            adapter_version=profile["adapter_version"],
            feature_hash=profile["feature_hash"],
            threshold_config_version=profile["threshold_config_version"],
            created_at=_parse_utc(profile["created_at"]),
            is_active=bool(profile["is_active"]),
            clips=clips,
            feature=feature,
        )

    def add_manifest(
        self,
        job_id: int,
        snapshot: VoiceManifestSnapshot,
        *,
        created_at: datetime,
    ) -> int:
        if (
            type(job_id) is not int
            or job_id <= 0
            or not _is_exact_utc(created_at)
        ):
            _invalid_manifest()
        self._validate_snapshot(snapshot, "VOICE_MANIFEST_INVALID")
        expected = build_presence_job_manifest(snapshot)
        self._validate_manifest_owners(
            job_id,
            snapshot,
            expected.manifest_hash,
            require_frozen_current=True,
            error_code="VOICE_MANIFEST_INVALID",
        )
        cursor = self._conn.execute(
            """
            INSERT INTO voice_verification_manifests(
                job_id, candidate_id, video_id, profile_id,
                presence_decision_id, presence_decision_hash,
                reference_profile_id, reference_feature_hash,
                threshold_config_version, model_name, model_version,
                adapter_version, vad_contract_version,
                selection_contract_version, manifest_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                snapshot.candidate_id,
                snapshot.video_id,
                snapshot.profile_id,
                snapshot.presence_decision_id,
                snapshot.presence_decision_hash,
                snapshot.reference_profile_id,
                snapshot.reference_feature_hash,
                snapshot.threshold_config_version,
                snapshot.model_name,
                snapshot.model_version,
                snapshot.adapter_version,
                snapshot.vad_contract_version,
                snapshot.selection_contract_version,
                expected.manifest_hash,
                utc_iso(created_at),
            ),
        )
        return _lastrowid(cursor)

    def get_manifest_for_job(self, job_id: int) -> StoredVoiceManifest:
        row = self._conn.execute(
            """
            SELECT *,
                   typeof(id) AS type_id,
                   typeof(job_id) AS type_job_id,
                   typeof(candidate_id) AS type_candidate_id,
                   typeof(video_id) AS type_video_id,
                   typeof(profile_id) AS type_profile_id,
                   typeof(presence_decision_id) AS type_presence_decision_id,
                   typeof(presence_decision_hash) AS type_presence_decision_hash,
                   typeof(reference_profile_id) AS type_reference_profile_id,
                   typeof(reference_feature_hash) AS type_reference_feature_hash,
                   typeof(threshold_config_version) AS type_threshold,
                   typeof(model_name) AS type_model_name,
                   typeof(model_version) AS type_model_version,
                   typeof(adapter_version) AS type_adapter_version,
                   typeof(vad_contract_version) AS type_vad_version,
                   typeof(selection_contract_version) AS type_selection_version,
                   typeof(manifest_hash) AS type_manifest_hash,
                   typeof(created_at) AS type_created_at
            FROM voice_verification_manifests
            WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"voice verification manifest not found: {job_id}")
        try:
            if (
                any(
                    row[key] != "integer"
                    for key in (
                        "type_id",
                        "type_job_id",
                        "type_candidate_id",
                        "type_video_id",
                        "type_profile_id",
                        "type_presence_decision_id",
                        "type_reference_profile_id",
                    )
                )
                or any(
                    row[key] != "text"
                    for key in (
                        "type_presence_decision_hash",
                        "type_reference_feature_hash",
                        "type_threshold",
                        "type_model_name",
                        "type_model_version",
                        "type_adapter_version",
                        "type_vad_version",
                        "type_selection_version",
                        "type_manifest_hash",
                        "type_created_at",
                    )
                )
            ):
                raise ValueError("manifest storage type")
            snapshot = VoiceManifestSnapshot(
                candidate_id=row["candidate_id"],
                video_id=row["video_id"],
                profile_id=row["profile_id"],
                presence_decision_id=row["presence_decision_id"],
                presence_decision_hash=row["presence_decision_hash"],
                reference_profile_id=row["reference_profile_id"],
                reference_feature_hash=row["reference_feature_hash"],
                threshold_config_version=row["threshold_config_version"],
                model_name=row["model_name"],
                model_version=row["model_version"],
                adapter_version=row["adapter_version"],
                vad_contract_version=row["vad_contract_version"],
                selection_contract_version=row["selection_contract_version"],
            )
            self._validate_snapshot(snapshot, "VOICE_MANIFEST_STORED_INVALID")
            created_at = _parse_utc(row["created_at"])
            if not _is_hash(row["manifest_hash"]):
                raise ValueError("manifest hash")
            self._validate_manifest_owners(
                row["job_id"],
                snapshot,
                row["manifest_hash"],
                require_frozen_current=False,
                error_code="VOICE_MANIFEST_STORED_INVALID",
            )
        except (DomainError, LookupError, TypeError, ValueError) as cause:
            if (
                isinstance(cause, DomainError)
                and cause.code == "VOICE_MANIFEST_STORED_INVALID"
            ):
                raise
            raise DomainError(
                "VOICE_MANIFEST_STORED_INVALID",
                "VOICE_MANIFEST_STORED_INVALID: stored voice verification "
                "manifest is invalid",
            ) from cause
        return StoredVoiceManifest(
            id=row["id"],
            job_id=row["job_id"],
            snapshot=snapshot,
            manifest_hash=row["manifest_hash"],
            created_at=created_at,
        )

    def add_run_with_segments(
        self,
        result: VoiceRunResult,
        segments: tuple[VoiceSegmentScore, ...],
        *,
        completed_at: datetime,
    ) -> int:
        self._require_transaction()
        manifest, canonical_segments = self._canonicalize_run(
            result, segments, completed_at=completed_at, stored=False
        )
        if self._conn.execute(
            "SELECT 1 FROM voice_verification_runs WHERE job_id=?",
            (result.job_id,),
        ).fetchone() is not None:
            _invalid_run()
        run_id = self._insert_run(result, completed_at)
        for segment in canonical_segments:
            self._insert_segment(run_id, segment)
        return run_id

    def get_run(self, run_id: int) -> StoredVoiceRun:
        return self._get_run(run_id, validate_review=True)

    def list_pending_reviews(self) -> tuple[StoredVoiceRun, ...]:
        rows = tuple(
            self._conn.execute(
                """
                SELECT run.id
                FROM voice_verification_runs AS run
                JOIN jobs AS job ON job.id=run.job_id
                LEFT JOIN voice_verification_reviews AS review ON review.run_id=run.id
                WHERE review.id IS NULL
                ORDER BY run.id
                """
            )
        )
        pending: list[StoredVoiceRun] = []
        for row in rows:
            if type(row["id"]) is not int:
                _stored_run_invalid()
            run = self.get_run(row["id"])
            job = self._conn.execute(
                "SELECT status, typeof(status) AS type_status FROM jobs WHERE id=?",
                (run.job_id,),
            ).fetchone()
            if (
                job is None
                or job["type_status"] != "text"
                or job["status"] not in _JOB_STATUSES
            ):
                _stored_run_invalid()
            if job["status"] != JobStatus.SUCCEEDED.value:
                continue
            artifacts = self.require_job_artifacts(
                run.job_id
            )
            if artifacts.run is None or artifacts.run.id != row["id"]:
                _stored_run_invalid()
            pending.append(artifacts.run)
        return tuple(pending)

    def list_runnable_job_ids(self) -> tuple[int, ...]:
        rows = tuple(
            self._conn.execute(
                """
                SELECT job.id, typeof(job.id) AS type_id, job.status,
                       typeof(job.status) AS type_status
                FROM voice_verification_manifests AS manifest
                JOIN jobs AS job ON job.id=manifest.job_id
                ORDER BY job.id
                """
            )
        )
        job_ids: list[int] = []
        for row in rows:
            if (
                row["type_id"] != "integer"
                or row["type_status"] != "text"
                or row["status"] not in _JOB_STATUSES
            ):
                _stored_manifest_invalid()
            manifest = self.get_manifest_for_job(row["id"])
            if row["status"] not in _RUNNABLE_STATUSES:
                continue
            current = self._candidate_current_decision_id(
                manifest.snapshot.candidate_id
            )
            if current != manifest.snapshot.presence_decision_id:
                _stored_manifest_invalid()
            self.require_job_artifacts(row["id"])
            job_ids.append(row["id"])
        return tuple(job_ids)

    def require_job_artifacts(self, job_id: int) -> VoiceJobArtifacts:
        manifest = self.get_manifest_for_job(job_id)
        reference = self.get_reference_bundle(manifest.snapshot.reference_profile_id)
        job = self._conn.execute(
            "SELECT status, typeof(status) AS type_status FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if (
            job is None
            or job["type_status"] != "text"
            or job["status"] not in _JOB_STATUSES
        ):
            _job_artifacts_invalid()
        run_row = self._conn.execute(
            "SELECT id FROM voice_verification_runs WHERE job_id=?", (job_id,)
        ).fetchone()
        run = None if run_row is None else self.get_run(run_row["id"])
        if job["status"] == JobStatus.SUCCEEDED.value:
            unit_rows = tuple(
                self._conn.execute(
                    """
                    SELECT unit_key, status, output_hash, bound_input_hash,
                           typeof(unit_key) AS type_unit_key,
                           typeof(status) AS type_status,
                           typeof(output_hash) AS type_output_hash,
                           typeof(bound_input_hash) AS type_bound_input_hash
                    FROM job_units
                    WHERE job_id=?
                    ORDER BY ordinal
                    """,
                    (job_id,),
                )
            )
            proposal_rows = tuple(
                row for row in unit_rows if row["unit_key"] == "voice:proposal"
            )
            if (
                run is None
                or len(unit_rows) != 7
                or any(
                    row["type_unit_key"] != "text"
                    or row["type_status"] != "text"
                    or row["type_output_hash"] != "text"
                    or row["type_bound_input_hash"] != "text"
                    or row["status"] != "success"
                    or not _is_hash(row["output_hash"])
                    or not _is_hash(row["bound_input_hash"])
                    for row in unit_rows
                )
                or len(proposal_rows) != 1
                or proposal_rows[0]["output_hash"] != run.output_hash
            ):
                _job_artifacts_invalid()
        return VoiceJobArtifacts(manifest=manifest, reference=reference, run=run)

    def add_review_and_decision(self, command: object) -> int:
        self._require_transaction()
        run_id, action, reason, actor = self._validate_review_command(command)
        pointer = self._conn.execute(
            """
            SELECT candidate.current_presence_decision_id,
                   manifest.presence_decision_id
            FROM voice_verification_runs AS run
            JOIN voice_verification_manifests AS manifest ON manifest.job_id=run.job_id
            JOIN subject_video_candidates AS candidate ON candidate.id=run.candidate_id
            WHERE run.id=?
            """,
            (run_id,),
        ).fetchone()
        if (
            pointer is None
            or type(pointer["current_presence_decision_id"]) is not int
            or pointer["current_presence_decision_id"]
            != pointer["presence_decision_id"]
        ):
            raise DomainError(
                "PRESENCE_REVIEW_STALE",
                "PRESENCE_REVIEW_STALE: presence review no longer has current input",
            )
        run = self._get_run(run_id, validate_review=False)
        manifest = self.get_manifest_for_job(run.job_id)
        artifacts = self.require_job_artifacts(run.job_id)
        if artifacts.run is None or artifacts.run.id != run_id:
            _invalid_review()
        if self._conn.execute(
            "SELECT 1 FROM voice_verification_reviews WHERE run_id=?", (run_id,)
        ).fetchone() is not None:
            raise DomainError(
                "PRESENCE_REVIEW_STALE",
                "PRESENCE_REVIEW_STALE: presence review no longer has current input",
            )
        if (
            self._candidate_current_decision_id(run.candidate_id)
            != manifest.snapshot.presence_decision_id
        ):
            raise DomainError(
                "PRESENCE_REVIEW_STALE",
                "PRESENCE_REVIEW_STALE: presence review no longer has current input",
            )
        reviewed_at = self._clock()
        if not _is_exact_utc(reviewed_at):
            _invalid_review()
        review_hash = _canonical_review_hash(
            run_id=run_id,
            action=action,
            actor=actor,
            reason=reason,
            prior_presence_decision_id=manifest.snapshot.presence_decision_id,
            prior_presence_decision_hash=manifest.snapshot.presence_decision_hash,
            reviewed_at=reviewed_at,
        )
        cursor = self._conn.execute(
            """
            INSERT INTO voice_verification_reviews(
                run_id, action, actor, reason, prior_presence_decision_id,
                prior_presence_decision_hash, review_hash, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                action.value,
                actor,
                reason,
                manifest.snapshot.presence_decision_id,
                manifest.snapshot.presence_decision_hash,
                review_hash,
                utc_iso(reviewed_at),
            ),
        )
        review_id = _lastrowid(cursor)
        if action is ReviewAction.HOLD:
            return review_id
        state = (
            PresenceState.CONFIRMED
            if action is ReviewAction.CONFIRM
            else PresenceState.REJECTED
        )
        decision_hash = canonical_presence_decision_hash(
            candidate_id=run.candidate_id,
            state=state,
            decision_origin=PresenceOrigin.VOICE_VERIFICATION,
            evidence_ref=str(review_id),
            evidence_hash=review_hash,
            created_at=reviewed_at,
        )
        decision_cursor = self._conn.execute(
            """
            INSERT INTO presence_decisions(
                candidate_id, state, decision_origin, evidence_ref,
                evidence_hash, decision_hash, created_at
            ) VALUES (?, ?, 'voice_verification', ?, ?, ?, ?)
            """,
            (
                run.candidate_id,
                state.value,
                str(review_id),
                review_hash,
                decision_hash,
                utc_iso(reviewed_at),
            ),
        )
        decision_id = _lastrowid(decision_cursor)
        pointer = self._conn.execute(
            """
            UPDATE subject_video_candidates
            SET current_presence_decision_id=?
            WHERE id=? AND current_presence_decision_id=?
            """,
            (
                decision_id,
                run.candidate_id,
                manifest.snapshot.presence_decision_id,
            ),
        )
        if pointer.rowcount != 1:
            raise DomainError(
                "PRESENCE_REVIEW_STALE",
                "PRESENCE_REVIEW_STALE: presence review no longer has current input",
            )
        return review_id

    def _get_run(self, run_id: int, *, validate_review: bool) -> StoredVoiceRun:
        row = self._conn.execute(
            """
            SELECT *,
                   typeof(id) AS type_id,
                   typeof(job_id) AS type_job_id,
                   typeof(candidate_id) AS type_candidate_id,
                   typeof(input_hash) AS type_input_hash,
                   typeof(output_hash) AS type_output_hash,
                   typeof(proposal) AS type_proposal,
                   typeof(result_code) AS type_result_code,
                   typeof(completed_at) AS type_completed_at
            FROM voice_verification_runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"voice verification run not found: {run_id}")
        try:
            if any(
                row[key] != expected
                for key, expected in (
                    ("type_id", "integer"),
                    ("type_job_id", "integer"),
                    ("type_candidate_id", "integer"),
                    ("type_input_hash", "text"),
                    ("type_output_hash", "text"),
                    ("type_proposal", "text"),
                    ("type_result_code", "text"),
                    ("type_completed_at", "text"),
                )
            ):
                raise ValueError("run storage type")
            proposal = VoiceProposal(row["proposal"])
            completed_at = _parse_utc(row["completed_at"])
            result = VoiceRunResult(
                job_id=row["job_id"],
                candidate_id=row["candidate_id"],
                input_hash=row["input_hash"],
                output_hash=row["output_hash"],
                proposal=proposal,
                result_code=row["result_code"],
            )
            segment_rows = tuple(
                self._conn.execute(
                    """
                    SELECT *,
                           typeof(id) AS type_id,
                           typeof(run_id) AS type_run_id,
                           typeof(ordinal) AS type_ordinal,
                           typeof(start_ms) AS type_start_ms,
                           typeof(end_ms) AS type_end_ms,
                           typeof(raw_match_score) AS type_score,
                           typeof(evidence_hash) AS type_evidence_hash
                    FROM voice_verification_segments
                    WHERE run_id=? ORDER BY ordinal
                    """,
                    (run_id,),
                )
            )
            segments = tuple(self._segment_from_row(item) for item in segment_rows)
            manifest, canonical_segments = self._canonicalize_run(
                result, segments, completed_at=completed_at, stored=True
            )
            stored = StoredVoiceRun(
                id=row["id"],
                result=result,
                segments=canonical_segments,
                completed_at=completed_at,
            )
            if validate_review:
                self._validate_review_linkage(stored, manifest)
            return stored
        except (DomainError, LookupError, TypeError, ValueError) as cause:
            if (
                isinstance(cause, DomainError)
                and cause.code == "VOICE_RUN_STORED_INVALID"
            ):
                raise
            raise DomainError(
                "VOICE_RUN_STORED_INVALID",
                "VOICE_RUN_STORED_INVALID: stored voice verification run is invalid",
            ) from cause

    def _canonicalize_run(
        self,
        result: VoiceRunResult,
        segments: tuple[VoiceSegmentScore, ...],
        *,
        completed_at: datetime,
        stored: bool,
    ) -> tuple[StoredVoiceManifest, tuple[VoiceSegmentScore, ...]]:
        invalid = _stored_run_invalid if stored else _invalid_run
        if (
            type(result) is not VoiceRunResult
            or type(result.job_id) is not int
            or result.job_id <= 0
            or type(result.candidate_id) is not int
            or result.candidate_id <= 0
            or not _is_hash(result.input_hash)
            or not _is_hash(result.output_hash)
            or type(result.proposal) is not VoiceProposal
            or type(result.result_code) is not str
            or _SAFE_RESULT_CODE.fullmatch(result.result_code) is None
            or result.result_code != "VOICE_PROPOSAL_READY"
            or type(segments) is not tuple
            or not segments
            or not _is_exact_utc(completed_at)
        ):
            invalid()
        try:
            manifest = self.get_manifest_for_job(result.job_id)
        except (DomainError, LookupError) as cause:
            if stored:
                raise DomainError(
                    "VOICE_RUN_STORED_INVALID",
                    "VOICE_RUN_STORED_INVALID: stored voice verification run "
                    "is invalid",
                ) from cause
            raise DomainError(
                "VOICE_RUN_INVALID", "VOICE_RUN_INVALID: voice run is invalid"
            ) from cause
        if result.candidate_id != manifest.snapshot.candidate_id:
            invalid()
        previous_end = -1
        canonical_segments: list[VoiceSegmentScore] = []
        for expected_ordinal, segment in enumerate(segments, start=1):
            if (
                type(segment) is not VoiceSegmentScore
                or type(segment.ordinal) is not int
                or segment.ordinal != expected_ordinal
                or type(segment.start_ms) is not int
                or type(segment.end_ms) is not int
                or segment.start_ms < 0
                or segment.start_ms < previous_end
                or segment.start_ms >= segment.end_ms
                or type(segment.raw_match_score) is not float
                or not isfinite(segment.raw_match_score)
                or abs(segment.raw_match_score) > 1.0e6
                or not _is_hash(segment.evidence_hash)
            ):
                invalid()
            expected_hash = canonical_voice_segment_hash(
                manifest.snapshot,
                result.input_hash,
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                raw_match_score=segment.raw_match_score,
            )
            if segment.evidence_hash != expected_hash:
                invalid()
            canonical_segments.append(segment)
            previous_end = segment.end_ms
        canonical_tuple = tuple(canonical_segments)
        expected_output = canonical_voice_run_output_hash(
            manifest.snapshot, result.input_hash, result.proposal, canonical_tuple
        )
        if result.output_hash != expected_output:
            invalid()
        return manifest, canonical_tuple

    def _insert_run(self, result: VoiceRunResult, completed_at: datetime) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO voice_verification_runs(
                job_id, candidate_id, input_hash, output_hash, proposal,
                result_code, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.job_id,
                result.candidate_id,
                result.input_hash,
                result.output_hash,
                result.proposal.value,
                result.result_code,
                utc_iso(completed_at),
            ),
        )
        return _lastrowid(cursor)

    def _insert_segment(self, run_id: int, segment: VoiceSegmentScore) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO voice_verification_segments(
                run_id, ordinal, start_ms, end_ms, raw_match_score, evidence_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                segment.ordinal,
                segment.start_ms,
                segment.end_ms,
                segment.raw_match_score,
                segment.evidence_hash,
            ),
        )
        return _lastrowid(cursor)

    def _segment_from_row(self, row: sqlite3.Row) -> VoiceSegmentScore:
        if any(
            row[key] != expected
            for key, expected in (
                ("type_id", "integer"),
                ("type_run_id", "integer"),
                ("type_ordinal", "integer"),
                ("type_start_ms", "integer"),
                ("type_end_ms", "integer"),
                ("type_score", "real"),
                ("type_evidence_hash", "text"),
            )
        ):
            _stored_run_invalid()
        return VoiceSegmentScore(
            ordinal=row["ordinal"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            raw_match_score=row["raw_match_score"],
            evidence_hash=row["evidence_hash"],
        )

    def _validate_manifest_owners(
        self,
        job_id: int,
        snapshot: VoiceManifestSnapshot,
        manifest_hash: str,
        *,
        require_frozen_current: bool,
        error_code: str,
    ) -> None:
        try:
            expected_manifest = build_presence_job_manifest(snapshot)
            if expected_manifest.manifest_hash != manifest_hash:
                raise ValueError("manifest hash")
            self._validate_job_manifest(job_id, expected_manifest)
            candidate = self._conn.execute(
                """
                SELECT candidate.*, profile.subject_id,
                       typeof(candidate.id) AS type_id,
                       typeof(candidate.profile_id) AS type_profile_id,
                       typeof(candidate.video_id) AS type_video_id,
                       typeof(candidate.current_presence_decision_id) AS type_current_id
                FROM subject_video_candidates AS candidate
                JOIN discovery_profiles AS profile ON profile.id=candidate.profile_id
                WHERE candidate.id=?
                """,
                (snapshot.candidate_id,),
            ).fetchone()
            if (
                candidate is None
                or any(
                    candidate[key] != "integer"
                    for key in (
                        "type_id",
                        "type_profile_id",
                        "type_video_id",
                        "type_current_id",
                    )
                )
                or candidate["profile_id"] != snapshot.profile_id
                or candidate["video_id"] != snapshot.video_id
            ):
                raise ValueError("candidate owner")
            bindings = tuple(
                self._conn.execute(
                    """
                    SELECT binding.candidate_id, set_row.expected_binding_count,
                           set_row.is_sealed
                    FROM video_pipeline_job_bindings AS binding
                    JOIN video_pipeline_job_binding_sets AS set_row
                      ON set_row.job_id=binding.job_id
                    WHERE binding.job_id=? ORDER BY binding.candidate_id
                    """,
                    (job_id,),
                )
            )
            if (
                len(bindings) != 1
                or bindings[0]["candidate_id"] != snapshot.candidate_id
                or bindings[0]["expected_binding_count"] != 1
                or bindings[0]["is_sealed"] != 1
            ):
                raise ValueError("binding owner")
            frozen = DiscoveryRepository(self._conn).get_presence_decision(
                snapshot.presence_decision_id
            )
            current = DiscoveryRepository(self._conn).get_presence_decision(
                candidate["current_presence_decision_id"]
            )
            if (
                frozen.candidate_id != snapshot.candidate_id
                or frozen.decision_hash != snapshot.presence_decision_hash
                or current.candidate_id != snapshot.candidate_id
                or (
                    require_frozen_current
                    and current.id != snapshot.presence_decision_id
                )
            ):
                raise ValueError("decision owner")
            reference = self.get_reference_bundle(snapshot.reference_profile_id)
            if (
                reference.subject_id != candidate["subject_id"]
                or reference.feature_hash != snapshot.reference_feature_hash
                or reference.threshold_config_version
                != snapshot.threshold_config_version
                or reference.model_name != snapshot.model_name
                or reference.model_version != snapshot.model_version
                or reference.adapter_version != snapshot.adapter_version
            ):
                raise ValueError("reference owner")
        except (DomainError, LookupError, TypeError, ValueError) as cause:
            raise DomainError(
                error_code, f"{error_code}: voice verification manifest is invalid"
            ) from cause

    def _validate_job_manifest(
        self, job_id: int, expected_manifest: JobManifest
    ) -> None:
        job = self._conn.execute(
            """
            SELECT *, typeof(id) AS type_id, typeof(job_kind) AS type_kind,
                   typeof(manifest_hash) AS type_hash,
                   typeof(total_units) AS type_total, typeof(status) AS type_status,
                   typeof(created_at) AS type_created,
                   typeof(updated_at) AS type_updated
            FROM jobs WHERE id=?
            """,
            (job_id,),
        ).fetchone()
        if (
            job is None
            or job["type_id"] != "integer"
            or job["type_kind"] != "text"
            or job["type_hash"] != "text"
            or job["type_total"] != "integer"
            or job["type_status"] != "text"
            or job["type_created"] != "text"
            or job["type_updated"] != "text"
            or job["job_kind"] != JobKind.VIDEO_PIPELINE.value
            or job["manifest_hash"] != expected_manifest.manifest_hash
            or job["total_units"] != len(expected_manifest.units)
            or job["status"] not in _JOB_STATUSES
        ):
            raise ValueError("job manifest")
        _parse_utc(job["created_at"])
        _parse_utc(job["updated_at"])
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal", (job_id,)
            )
        )
        units: list[ManifestUnit] = []
        for row in rows:
            dependencies = json.loads(row["dependency_keys_json"])
            if (
                type(row["ordinal"]) is not int
                or type(row["unit_key"]) is not str
                or type(row["stage"]) is not str
                or type(dependencies) is not list
                or any(type(item) is not str for item in dependencies)
                or canonical_json(dependencies) != row["dependency_keys_json"]
            ):
                raise ValueError("job unit")
            units.append(
                ManifestUnit(
                    unit_key=row["unit_key"],
                    stage=JobStage(row["stage"]),
                    ordinal=row["ordinal"],
                    declared_input_hash=row["declared_input_hash"],
                    dependency_keys=tuple(dependencies),
                    execution_contract_hash=row["execution_contract_hash"],
                )
            )
        rebuilt = JobManifest.build(JobKind.VIDEO_PIPELINE, tuple(units))
        if rebuilt != expected_manifest:
            raise ValueError("job units")

    def _validate_snapshot(self, snapshot: object, error_code: str) -> None:
        if (
            type(snapshot) is not VoiceManifestSnapshot
            or any(
                type(value) is not int or value <= 0
                for value in (
                    snapshot.candidate_id,
                    snapshot.video_id,
                    snapshot.profile_id,
                    snapshot.presence_decision_id,
                    snapshot.reference_profile_id,
                )
            )
            or not _is_hash(snapshot.presence_decision_hash)
            or not _is_hash(snapshot.reference_feature_hash)
            or any(
                not _is_token(value)
                for value in (
                    snapshot.threshold_config_version,
                    snapshot.model_name,
                    snapshot.model_version,
                    snapshot.adapter_version,
                    snapshot.vad_contract_version,
                    snapshot.selection_contract_version,
                )
            )
        ):
            raise DomainError(
                error_code, f"{error_code}: voice verification manifest is invalid"
            )

    def _read_reference_profile(self, reference_profile_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            """
            SELECT *,
                   typeof(id) AS type_id, typeof(subject_id) AS type_subject_id,
                   typeof(model_name) AS type_model_name,
                   typeof(model_version) AS type_model_version,
                   typeof(adapter_version) AS type_adapter_version,
                   typeof(feature_hash) AS type_feature_hash,
                   typeof(threshold_config_version) AS type_threshold,
                   typeof(created_at) AS type_created_at,
                   typeof(is_active) AS type_is_active
            FROM voice_reference_profiles WHERE id=?
            """,
            (reference_profile_id,),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"voice reference profile not found: {reference_profile_id}"
            )
        try:
            if (
                row["type_id"] != "integer"
                or row["type_subject_id"] != "integer"
                or row["type_model_name"] != "text"
                or row["type_model_version"] != "text"
                or row["type_adapter_version"] != "text"
                or row["type_feature_hash"] != "text"
                or row["type_threshold"] != "text"
                or row["type_created_at"] != "text"
                or row["type_is_active"] != "integer"
                or row["id"] <= 0
                or row["subject_id"] <= 0
                or not _is_token(row["model_name"])
                or not _is_token(row["model_version"])
                or not _is_token(row["adapter_version"])
                or not _is_hash(row["feature_hash"])
                or not _is_token(row["threshold_config_version"])
                or row["is_active"] not in {0, 1}
            ):
                raise ValueError("reference profile")
            _parse_utc(row["created_at"])
            threshold = self._conn.execute(
                "SELECT * FROM speaker_threshold_configs WHERE version=?",
                (row["threshold_config_version"],),
            ).fetchone()
            if (
                threshold is None
                or threshold["model_name"] != row["model_name"]
                or threshold["model_version"] != row["model_version"]
                or threshold["subject_operator"] != "gte"
                or threshold["interviewer_operator"] != "lte"
                or type(threshold["subject_boundary"]) is not float
                or type(threshold["interviewer_boundary"]) is not float
                or not isfinite(threshold["subject_boundary"])
                or not isfinite(threshold["interviewer_boundary"])
                or threshold["subject_boundary"] <= threshold["interviewer_boundary"]
                or type(threshold["is_active"]) is not int
                or threshold["is_active"] not in {0, 1}
            ):
                raise ValueError("reference threshold")
            _parse_utc(threshold["created_at"])
        except (TypeError, ValueError) as cause:
            raise DomainError(
                "VOICE_REFERENCE_STORED_INVALID",
                "VOICE_REFERENCE_STORED_INVALID: stored voice reference is invalid",
            ) from cause
        return row

    def _feature_from_row(
        self, row: sqlite3.Row, profile: sqlite3.Row
    ) -> StoredReferenceFeature:
        if (
            any(
                row[key] != expected
                for key, expected in (
                    ("type_id", "integer"),
                    ("type_reference_profile_id", "integer"),
                    ("type_encoding_version", "text"),
                    ("type_float_dtype", "text"),
                    ("type_dimension", "integer"),
                    ("type_embedding_blob", "blob"),
                    ("type_feature_sha256", "text"),
                    ("type_created_at", "text"),
                )
            )
            or row["reference_profile_id"] != profile["id"]
            or not _is_token(row["encoding_version"])
            or row["float_dtype"] not in {"float32", "float64"}
            or row["dimension"] <= 0
            or type(row["embedding_blob"]) is not bytes
            or len(row["embedding_blob"])
            != row["dimension"] * (4 if row["float_dtype"] == "float32" else 8)
            or not _is_hash(row["feature_sha256"])
            or hashlib.sha256(row["embedding_blob"]).hexdigest()
            != row["feature_sha256"]
            or row["feature_sha256"] != profile["feature_hash"]
        ):
            _stored_reference_invalid()
        return StoredReferenceFeature(
            id=row["id"],
            reference_profile_id=row["reference_profile_id"],
            encoding_version=row["encoding_version"],
            float_dtype=row["float_dtype"],
            dimension=row["dimension"],
            embedding_blob=row["embedding_blob"],
            feature_sha256=row["feature_sha256"],
            created_at=_parse_utc(row["created_at"]),
        )

    def _clip_from_row(
        self, row: sqlite3.Row, profile: sqlite3.Row, *, expected_ordinal: int
    ) -> StoredReferenceClip:
        try:
            approved_at = _parse_utc(row["approved_at"])
            if (
                any(
                    row[key] != expected
                    for key, expected in (
                        ("type_id", "integer"),
                        ("type_reference_profile_id", "integer"),
                        ("type_ordinal", "integer"),
                        ("type_clip_kind", "text"),
                        ("type_subject_id", "integer"),
                        ("type_video_id", "integer"),
                        ("type_start_ms", "integer"),
                        ("type_end_ms", "integer"),
                        ("type_audio_hash", "text"),
                        ("type_approval_actor", "text"),
                        ("type_approval_reason", "text"),
                        ("type_approved_at", "text"),
                        ("type_clip_hash", "text"),
                    )
                )
                or row["reference_profile_id"] != profile["id"]
                or row["ordinal"] != expected_ordinal
                or row["clip_kind"] not in _CLIP_KINDS
                or row["subject_id"] != profile["subject_id"]
                or row["video_id"] <= 0
                or row["start_ms"] < 0
                or row["start_ms"] >= row["end_ms"]
                or not _is_hash(row["normalized_audio_sha256"])
                or row["approval_actor"] != "local_user"
                or type(row["approval_reason"]) is not str
                or not 1 <= len(row["approval_reason"]) <= 240
                or not _is_hash(row["clip_hash"])
            ):
                raise ValueError("reference clip")
            self._validate_reason(
                row["approval_reason"], "VOICE_REFERENCE_STORED_INVALID"
            )
            expected_hash = canonical_voice_clip_hash(
                reference_profile_id=row["reference_profile_id"],
                ordinal=row["ordinal"],
                clip_kind=row["clip_kind"],
                subject_id=row["subject_id"],
                video_id=row["video_id"],
                start_ms=row["start_ms"],
                end_ms=row["end_ms"],
                normalized_audio_sha256=row["normalized_audio_sha256"],
                approval_actor=row["approval_actor"],
                approval_reason=row["approval_reason"],
                approved_at=approved_at,
            )
            if row["clip_hash"] != expected_hash:
                raise ValueError("reference clip hash")
        except (DomainError, TypeError, ValueError) as cause:
            raise DomainError(
                "VOICE_REFERENCE_STORED_INVALID",
                "VOICE_REFERENCE_STORED_INVALID: stored voice reference is invalid",
            ) from cause
        return StoredReferenceClip(
            id=row["id"],
            reference_profile_id=row["reference_profile_id"],
            ordinal=row["ordinal"],
            clip_kind=row["clip_kind"],
            subject_id=row["subject_id"],
            video_id=row["video_id"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            normalized_audio_sha256=row["normalized_audio_sha256"],
            approval_actor=row["approval_actor"],
            approval_reason=row["approval_reason"],
            approved_at=approved_at,
            clip_hash=row["clip_hash"],
        )

    def _validate_review_command(
        self, command: object
    ) -> tuple[int, ReviewAction, str, str]:
        try:
            run_id = command.run_id
            action = command.action
            reason = command.reason
            actor = command.actor
        except AttributeError as cause:
            raise DomainError(
                "VOICE_REVIEW_INVALID", "VOICE_REVIEW_INVALID: voice review is invalid"
            ) from cause
        if (
            type(command).__name__ != "ReviewCommand"
            or type(run_id) is not int
            or run_id <= 0
            or type(action) is not ReviewAction
            or type(actor) is not str
            or actor != "local_user"
            or type(reason) is not str
            or not 1 <= len(reason) <= 240
        ):
            _invalid_review()
        self._validate_reason(reason, "VOICE_REVIEW_INVALID")
        return run_id, action, reason, actor

    def _validate_review_linkage(
        self, run: StoredVoiceRun, manifest: StoredVoiceManifest
    ) -> None:
        rows = tuple(
            self._conn.execute(
                "SELECT * FROM voice_verification_reviews WHERE run_id=?",
                (run.id,),
            )
        )
        current_id = self._candidate_current_decision_id(run.candidate_id)
        if not rows:
            if current_id != manifest.snapshot.presence_decision_id:
                _stored_run_invalid()
            return
        if len(rows) != 1:
            _stored_run_invalid()
        row = rows[0]
        try:
            action = ReviewAction(row["action"])
            reviewed_at = _parse_utc(row["reviewed_at"])
            if (
                type(row["id"]) is not int
                or type(row["run_id"]) is not int
                or row["run_id"] != run.id
                or row["actor"] != "local_user"
                or type(row["reason"]) is not str
                or not 1 <= len(row["reason"]) <= 240
                or type(row["prior_presence_decision_id"]) is not int
                or row["prior_presence_decision_id"]
                != manifest.snapshot.presence_decision_id
                or row["prior_presence_decision_hash"]
                != manifest.snapshot.presence_decision_hash
                or not _is_hash(row["review_hash"])
            ):
                raise ValueError("review row")
            self._validate_reason(row["reason"], "VOICE_RUN_STORED_INVALID")
            expected_hash = _canonical_review_hash(
                run_id=run.id,
                action=action,
                actor=row["actor"],
                reason=row["reason"],
                prior_presence_decision_id=row["prior_presence_decision_id"],
                prior_presence_decision_hash=row["prior_presence_decision_hash"],
                reviewed_at=reviewed_at,
            )
            if row["review_hash"] != expected_hash:
                raise ValueError("review hash")
            decisions = tuple(
                self._conn.execute(
                    """
                    SELECT id FROM presence_decisions
                    WHERE candidate_id=? AND decision_origin='voice_verification'
                      AND evidence_ref=?
                    ORDER BY id
                    """,
                    (run.candidate_id, str(row["id"])),
                )
            )
            if action is ReviewAction.HOLD:
                if decisions or current_id != manifest.snapshot.presence_decision_id:
                    raise ValueError("hold decision")
                return
            if len(decisions) != 1:
                raise ValueError("review decision")
            decision = DiscoveryRepository(self._conn).get_presence_decision(
                decisions[0]["id"]
            )
            expected_state = (
                PresenceState.CONFIRMED
                if action is ReviewAction.CONFIRM
                else PresenceState.REJECTED
            )
            if (
                decision.state is not expected_state
                or decision.decision_origin is not PresenceOrigin.VOICE_VERIFICATION
                or decision.evidence_hash != row["review_hash"]
                or decision.created_at != reviewed_at
                or current_id != decision.id
            ):
                raise ValueError("review evidence")
        except (DomainError, LookupError, TypeError, ValueError) as cause:
            raise DomainError(
                "VOICE_RUN_STORED_INVALID",
                "VOICE_RUN_STORED_INVALID: stored voice verification run is invalid",
            ) from cause

    def _candidate_current_decision_id(self, candidate_id: int) -> int:
        row = self._conn.execute(
            """
            SELECT current_presence_decision_id,
                   typeof(current_presence_decision_id) AS type_current_id
            FROM subject_video_candidates WHERE id=?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None or row["type_current_id"] != "integer":
            raise ValueError("candidate current decision")
        return row["current_presence_decision_id"]

    def _validate_reason(self, reason: object, error_code: str) -> None:
        if type(reason) is not str or not 1 <= len(reason) <= 240:
            raise DomainError(
                error_code, f"{error_code}: voice persistence input is invalid"
            )
        try:
            validate_audit_reason(self._conn, reason)
        except DomainError as cause:
            raise DomainError(
                error_code, f"{error_code}: voice persistence input is invalid"
            ) from cause

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "TRANSACTION_REQUIRED",
                "TRANSACTION_REQUIRED: voice persistence requires an active "
                "caller transaction",
            )


def _canonical_review_hash(
    *,
    run_id: int,
    action: ReviewAction,
    actor: str,
    reason: str,
    prior_presence_decision_id: int,
    prior_presence_decision_hash: str,
    reviewed_at: datetime,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "action": action.value,
                "actor": actor,
                "prior_presence_decision_hash": prior_presence_decision_hash,
                "prior_presence_decision_id": prior_presence_decision_id,
                "reason": reason,
                "reviewed_at": utc_iso(reviewed_at),
                "run_id": run_id,
                "schema": "voice-verification-review.v1",
            }
        )
    )


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or _UTC_TEXT.fullmatch(value) is None:
        raise ValueError("stored datetime is not canonical UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not _is_exact_utc(parsed) or utc_iso(parsed) != value:
        raise ValueError("stored datetime is not canonical UTC")
    return parsed


def _is_exact_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is timezone.utc


def _is_hash(value: object) -> bool:
    return type(value) is str and _CANONICAL_HASH.fullmatch(value) is not None


def _is_token(value: object) -> bool:
    return type(value) is str and _SAFE_TOKEN.fullmatch(value) is not None


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if type(cursor.lastrowid) is not int or cursor.lastrowid <= 0:
        raise RuntimeError("voice persistence insert did not return an id")
    return cursor.lastrowid


def _invalid_reference() -> None:
    raise DomainError(
        "VOICE_REFERENCE_INVALID", "VOICE_REFERENCE_INVALID: voice reference is invalid"
    )


def _stored_reference_invalid() -> None:
    raise DomainError(
        "VOICE_REFERENCE_STORED_INVALID",
        "VOICE_REFERENCE_STORED_INVALID: stored voice reference is invalid",
    )


def _invalid_manifest() -> None:
    raise DomainError(
        "VOICE_MANIFEST_INVALID",
        "VOICE_MANIFEST_INVALID: voice verification manifest is invalid",
    )


def _stored_manifest_invalid() -> None:
    raise DomainError(
        "VOICE_MANIFEST_STORED_INVALID",
        "VOICE_MANIFEST_STORED_INVALID: stored voice verification manifest is invalid",
    )


def _invalid_run() -> None:
    raise DomainError("VOICE_RUN_INVALID", "VOICE_RUN_INVALID: voice run is invalid")


def _stored_run_invalid() -> None:
    raise DomainError(
        "VOICE_RUN_STORED_INVALID",
        "VOICE_RUN_STORED_INVALID: stored voice run is invalid",
    )


def _invalid_review() -> None:
    raise DomainError(
        "VOICE_REVIEW_INVALID", "VOICE_REVIEW_INVALID: voice review is invalid"
    )


def _job_artifacts_invalid() -> None:
    raise DomainError(
        "VOICE_JOB_ARTIFACTS_INVALID",
        "VOICE_JOB_ARTIFACTS_INVALID: voice job artifacts are invalid",
    )
