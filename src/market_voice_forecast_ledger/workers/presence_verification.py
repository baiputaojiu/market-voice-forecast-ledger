from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from threading import Lock
from typing import Protocol

try:
    import _winapi
except ImportError:  # pragma: no cover - the supported runtime is Windows
    _winapi = None

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import JobStatus, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import ResumePlan
from market_voice_forecast_ledger.domain.voice_verification import (
    PRESENCE_UNITS,
    VoiceRunResult,
    VoiceSegmentScore,
)
from market_voice_forecast_ledger.repositories.discovery import (
    DiscoveryRepository,
)
from market_voice_forecast_ledger.repositories.retention import (
    LocalArtifact,
    RetentionRepository,
)
from market_voice_forecast_ledger.repositories.voice_verification import (
    StoredVoiceManifest,
    StoredVoiceRun,
    VoiceJobArtifacts,
    VoiceVerificationRepository,
    canonical_voice_run_output_hash,
    canonical_voice_segment_hash,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.retention import (
    AudioDeletionResult,
    RetentionService,
)
from market_voice_forecast_ledger.voice.media import (
    AcquiredMedia,
    NormalizedAudio,
    PrivateJobWorkspace,
    normalized_wav_duration_ms,
)
from market_voice_forecast_ledger.voice.protocol import (
    AdapterRequest,
    AdapterResponse,
)
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation


_UNIT_KEYS = tuple(unit_key for unit_key, _ in PRESENCE_UNITS)
_KNOWN_FAILURE_CODES = frozenset(
    {
        "AUDIO_DELETE_OS_ERROR",
        "AUDIO_DELETE_PERMISSION",
        "AUDIO_PATH_OUTSIDE_TEMP_ROOT",
        "VOICE_ADAPTER_PROCESS_FAILED",
        "VOICE_ADAPTER_RESPONSE_INVALID",
        "VOICE_MEDIA_ACQUISITION_FAILED",
        "VOICE_MEDIA_NORMALIZATION_FAILED",
        "VOICE_PROCESSING_FAILED",
    }
)
_RECOVERABLE_STATUSES = frozenset(
    {
        JobStatus.RUNNING.value,
        JobStatus.FAILED.value,
        JobStatus.PAUSE_REQUESTED.value,
        JobStatus.CANCEL_REQUESTED.value,
    }
)
_CANDIDATE_STATUSES = _RECOVERABLE_STATUSES | frozenset(
    {JobStatus.QUEUED.value, JobStatus.RETRYING.value}
)
_JOB_STATUSES = frozenset(status.value for status in JobStatus)
_WAKE_LOCK = Lock()
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102


class MediaAcquirer(Protocol):
    def acquire_registered(
        self,
        video_id: str,
        target_dir: Path,
        *,
        source_path: Path,
        part_path: Path,
    ) -> AcquiredMedia: ...


class MediaNormalizer(Protocol):
    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio: ...


class VoiceAdapter(Protocol):
    def score(self, request: AdapterRequest) -> AdapterResponse: ...


@dataclass(frozen=True, slots=True)
class PresenceWorkerSummary:
    job_id: int | None
    succeeded_jobs: int
    failed_jobs: int
    paused_jobs: int
    stopped_jobs: int
    failed_code: str | None


@dataclass(frozen=True, slots=True)
class _Threshold:
    version: str
    subject_boundary: float
    interviewer_boundary: float


@dataclass(frozen=True, slots=True)
class _AudioPaths:
    workspace: Path
    source: Path
    part: Path
    normalized: Path

    @property
    def all(self) -> tuple[Path, Path, Path]:
        return (self.source, self.part, self.normalized)


@dataclass(slots=True)
class _WakeOwnership:
    mutex_handle: int

    def release(self) -> None:
        try:
            _release_windows_mutex(self.mutex_handle)
        except Exception:
            pass
        finally:
            _WAKE_LOCK.release()


class PresenceVerificationWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        media_acquirer: MediaAcquirer,
        media_normalizer: MediaNormalizer,
        adapter: VoiceAdapter,
        retention: RetentionService,
        runtime: RuntimeAttestation,
        temp_audio_root: Path,
        clock: Callable[[], datetime] | None = None,
        after_unit_committed: Callable[[int, str], None] | None = None,
    ) -> None:
        try:
            if (
                not isinstance(conn, sqlite3.Connection)
                or not isinstance(runtime, RuntimeAttestation)
                or not isinstance(temp_audio_root, Path)
                or not temp_audio_root.is_absolute()
            ):
                raise ValueError("invalid worker dependencies")
            root = temp_audio_root.resolve(strict=True)
            if root != temp_audio_root.absolute() or not root.is_dir():
                raise ValueError("invalid private audio root")
            PrivateJobWorkspace.capture(root)
            if not callable(getattr(media_acquirer, "acquire_registered", None)):
                raise ValueError("invalid media acquirer")
            if not callable(
                getattr(media_normalizer, "normalize_registered", None)
            ):
                raise ValueError("invalid media normalizer")
            if not callable(getattr(adapter, "score", None)):
                raise ValueError("invalid adapter")
            if not callable(getattr(retention, "delete_audio", None)):
                raise ValueError("invalid retention service")
            if after_unit_committed is not None and not callable(
                after_unit_committed
            ):
                raise ValueError("invalid unit observer")
            wake_mutex_name = _database_wake_mutex_name(conn)
        except Exception:
            raise DomainError(
                "VOICE_WORKER_CONFIG_INVALID",
                "presence worker configuration is invalid",
            ) from None
        self._conn = conn
        self._media_acquirer = media_acquirer
        self._media_normalizer = media_normalizer
        self._adapter = adapter
        self._retention_service = retention
        self._runtime = runtime
        self._root = root
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._after_unit_committed = after_unit_committed or (
            lambda _job_id, _unit_key: None
        )
        self._jobs = JobStateService(conn, clock=self._clock)
        self._voice = VoiceVerificationRepository(conn, clock=self._clock)
        self._retention = RetentionRepository(conn)
        self._responses: dict[int, AdapterResponse] = {}
        self._wake_mutex_name = wake_mutex_name

    def run_once(self) -> PresenceWorkerSummary:
        ownership = None
        try:
            ownership = self._try_acquire_wake()
            if ownership is None:
                return _empty_summary()
            return self._run_once_unlocked()
        except Exception as cause:
            return _failed_summary(None, _safe_failure_code(cause))
        finally:
            if ownership is not None:
                ownership.release()

    def _run_once_unlocked(self) -> PresenceWorkerSummary:
        try:
            target = self._claim_next_fifo()
        except Exception:
            return _failed_summary(None, "VOICE_PROCESSING_FAILED")
        if target is None:
            return _empty_summary()
        job_id, requires_recovery = target
        if requires_recovery:
            try:
                plan = self._recover_job(job_id)
            except Exception as cause:
                return _failed_summary(job_id, _safe_failure_code(cause))
            status = self._jobs.status(job_id)
            if status is JobStatus.SUCCEEDED:
                return _succeeded_summary(job_id)
            boundary = _boundary_summary(job_id, status)
            if boundary is not None:
                return boundary
            if plan.next_unit_key is None:
                return _failed_summary(job_id, "VOICE_PROCESSING_FAILED")
        return self._execute_job(job_id)

    def recover_job(self, job_id: int) -> ResumePlan:
        ownership = None
        try:
            ownership = self._try_acquire_wake()
            if ownership is None:
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "presence worker is unavailable",
                )
            return self._recover_job(job_id)
        except Exception as cause:
            raise _safe_domain_error(cause) from None
        finally:
            if ownership is not None:
                ownership.release()

    def _try_acquire_wake(self) -> _WakeOwnership | None:
        if not _WAKE_LOCK.acquire(blocking=False):
            return None
        try:
            handle = _try_acquire_windows_mutex(self._wake_mutex_name)
        except Exception:
            _WAKE_LOCK.release()
            raise
        if handle is None:
            _WAKE_LOCK.release()
            return None
        return _WakeOwnership(handle)

    def _recover_job(self, job_id: int) -> ResumePlan:
        boundary_plan = self._settle_requested_boundary(job_id)
        if boundary_plan is not None:
            return boundary_plan
        artifacts = self._canonical_artifacts(job_id)
        artifact_hashes = self._verified_artifact_hashes(artifacts)
        with transaction(self._conn):
            self._jobs.require_canonical_video_pipeline_job(job_id)
            current = self._voice.require_job_artifacts(job_id)
            self._require_same_artifacts(artifacts, current)
            status = self._jobs.status(job_id)
            if status is JobStatus.FAILED:
                plan = self._jobs.retry_failed_in_transaction(
                    job_id, artifact_hashes
                )
            elif status in {
                JobStatus.RUNNING,
                JobStatus.PAUSE_REQUESTED,
                JobStatus.CANCEL_REQUESTED,
            }:
                plan = self._jobs.recover_interrupted_in_transaction(
                    job_id, artifact_hashes
                )
            else:
                raise DomainError(
                    "VOICE_RECOVERY_INVALID",
                    "presence job is not recoverable",
                )
            if plan.next_unit_key is None and self._jobs.status(
                job_id
            ) not in {JobStatus.PAUSED, JobStatus.STOPPED}:
                self._jobs.succeed_job_in_transaction(job_id)
                self._voice.require_job_artifacts(job_id)
        if plan.next_unit_key in {"audio:acquire", "audio:normalize"}:
            self._discard_stale_audio(job_id, from_unit=plan.next_unit_key)
        return plan

    def _settle_requested_boundary(self, job_id: int) -> ResumePlan | None:
        with transaction(self._conn):
            self._jobs.require_canonical_video_pipeline_job(job_id)
            status = self._jobs.status(job_id)
            if status not in {
                JobStatus.PAUSE_REQUESTED,
                JobStatus.CANCEL_REQUESTED,
            }:
                return None
            try:
                plan = self._jobs.recover_interrupted_in_transaction(job_id, {})
            except DomainError as cause:
                if (
                    status is not JobStatus.CANCEL_REQUESTED
                    or cause.code != "STOPPED_JOB_REQUIRES_SUCCESSOR"
                    or self._jobs.status(job_id) is not JobStatus.STOPPED
                ):
                    raise
                plan = self._resume_plan_from_state(job_id)
            return plan

    def _resume_plan_from_state(self, job_id: int) -> ResumePlan:
        units = tuple(self._jobs.unit(job_id, key) for key in _UNIT_KEYS)
        reused = tuple(
            unit.unit_key for unit in units if unit.status is UnitStatus.SUCCESS
        )
        pending = tuple(
            unit.unit_key for unit in units if unit.status is UnitStatus.PENDING
        )
        return ResumePlan(reused, pending, pending[0] if pending else None)

    def _execute_job(self, job_id: int) -> PresenceWorkerSummary:
        while True:
            status = self._jobs.status(job_id)
            boundary = _boundary_summary(job_id, status)
            if boundary is not None:
                return boundary
            unit_key = self._next_unit_key(job_id)
            if unit_key is None:
                try:
                    with transaction(self._conn):
                        self._jobs.require_canonical_video_pipeline_job(job_id)
                        self._voice.require_job_artifacts(job_id)
                        self._jobs.succeed_job_in_transaction(job_id)
                        self._voice.require_job_artifacts(job_id)
                except Exception:
                    return _failed_summary(job_id, "VOICE_PROCESSING_FAILED")
                return _succeeded_summary(job_id)
            unit = self._jobs.unit(job_id, unit_key)
            try:
                if unit.status is UnitStatus.PENDING:
                    external_hash = self._external_input_hash(job_id, unit_key)
                    with transaction(self._conn):
                        self._jobs.require_canonical_video_pipeline_job(job_id)
                        self._voice.require_job_artifacts(job_id)
                        self._jobs.begin_unit_in_transaction(
                            job_id, unit_key, external_hash
                        )
                        self._voice.require_job_artifacts(job_id)
                elif unit.status is not UnitStatus.RUNNING:
                    raise DomainError(
                        "VOICE_PROCESSING_FAILED",
                        "presence unit state is invalid",
                    )
                self._execute_unit(job_id, unit_key)
                self._after_unit_committed(job_id, unit_key)
            except Exception as cause:
                code = _safe_failure_code(cause)
                try:
                    if self._jobs.unit(job_id, unit_key).status is UnitStatus.RUNNING:
                        self._jobs.fail_unit(job_id, unit_key, code)
                except Exception:
                    code = "VOICE_PROCESSING_FAILED"
                return _failed_summary(job_id, code)

    def _execute_unit(self, job_id: int, unit_key: str) -> None:
        artifacts = self._canonical_artifacts(job_id)
        if unit_key == "video:validate":
            output_hash = self._video_artifact_hash(artifacts)
            self._complete_unit(job_id, unit_key, output_hash)
            return
        paths = self._audio_paths(job_id, create=True)
        if unit_key == "audio:acquire":
            self._register_audio(paths.source)
            self._register_audio(paths.part)
            video_id = self._youtube_video_id(artifacts.manifest)
            acquired = self._media_acquirer.acquire_registered(
                video_id,
                paths.workspace,
                source_path=paths.source,
                part_path=paths.part,
            )
            if (
                type(acquired) is not AcquiredMedia
                or acquired.path != paths.source
                or acquired.video_id != video_id
                or _file_sha256(paths.source) != acquired.sha256
            ):
                raise DomainError(
                    "VOICE_MEDIA_ACQUISITION_FAILED",
                    "media acquisition failed",
                )
            self._complete_unit(job_id, unit_key, acquired.sha256)
            return
        if unit_key == "audio:normalize":
            self._register_audio(paths.normalized)
            normalized = self._media_normalizer.normalize_registered(
                paths.source, paths.normalized
            )
            if (
                type(normalized) is not NormalizedAudio
                or normalized.path != paths.normalized
                or _file_sha256(paths.normalized) != normalized.sha256
                or _file_sha256(paths.source) != normalized.source_sha256
            ):
                raise DomainError(
                    "VOICE_MEDIA_NORMALIZATION_FAILED",
                    "media normalization failed",
                )
            self._complete_unit(job_id, unit_key, normalized.sha256)
            return
        if unit_key in {"voice:vad", "voice:score"}:
            response = self._adapter_response(job_id, artifacts)
            output_hash = (
                self._vad_artifact_hash(artifacts, response)
                if unit_key == "voice:vad"
                else self._score_artifact_hash(artifacts, response)
            )
            self._complete_unit(job_id, unit_key, output_hash)
            return
        if unit_key == "voice:proposal":
            response = self._adapter_response(job_id, artifacts)
            self._commit_proposal(job_id, artifacts, response)
            return
        if unit_key == "audio:cleanup":
            output_hash = self._cleanup_audio(job_id)
            self._complete_unit(job_id, unit_key, output_hash)
            return
        raise DomainError(
            "VOICE_PROCESSING_FAILED", "presence unit is unsupported"
        )

    def _complete_unit(
        self, job_id: int, unit_key: str, output_hash: str
    ) -> None:
        with transaction(self._conn):
            artifacts = self._canonical_artifacts(job_id)
            self._require_current_unit_input(job_id, unit_key, artifacts)
            if self._current_unit_artifact_hash(
                job_id, unit_key, artifacts
            ) != output_hash:
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "presence unit artifact changed",
                )
            self._jobs.complete_unit_in_transaction(
                job_id, unit_key, output_hash
            )
            self._voice.require_job_artifacts(job_id)

    def _commit_proposal(
        self,
        job_id: int,
        artifacts: VoiceJobArtifacts,
        response: AdapterResponse,
    ) -> None:
        segments = tuple(
            VoiceSegmentScore(
                ordinal=segment.ordinal,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                raw_match_score=segment.raw_score,
                evidence_hash=canonical_voice_segment_hash(
                    artifacts.manifest.snapshot,
                    response.input_hash,
                    ordinal=segment.ordinal,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    raw_match_score=segment.raw_score,
                ),
            )
            for segment in response.segments
        )
        output_hash = canonical_voice_run_output_hash(
            artifacts.manifest.snapshot,
            response.input_hash,
            response.proposal,
            segments,
        )
        result = VoiceRunResult(
            job_id=job_id,
            candidate_id=artifacts.manifest.snapshot.candidate_id,
            input_hash=response.input_hash,
            output_hash=output_hash,
            proposal=response.proposal,
            result_code="VOICE_PROPOSAL_READY",
        )
        completed_at = self._clock()
        with transaction(self._conn):
            current = self._canonical_artifacts(job_id)
            self._require_same_artifacts(artifacts, current)
            self._require_current_unit_input(
                job_id,
                "voice:proposal",
                current,
                response=response,
            )
            self._voice.add_run_with_segments(
                result, segments, completed_at=completed_at
            )
            self._jobs.complete_unit_in_transaction(
                job_id, "voice:proposal", output_hash
            )
            reread = self._voice.require_job_artifacts(job_id)
            if reread.run is None or reread.run.output_hash != output_hash:
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "presence proposal could not be verified",
                )

    def _claim_next_fifo(self) -> tuple[int, bool] | None:
        with transaction(self._conn):
            candidates = self._candidate_jobs_in_transaction()
            if not candidates:
                return None
            job_id, status = candidates[0]
            if status in _RECOVERABLE_STATUSES:
                return job_id, True
            runnable = self._voice.list_runnable_job_ids()
            if job_id not in runnable:
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "presence job inventory is invalid",
                )
            self._jobs.require_canonical_video_pipeline_job(job_id)
            artifacts = self._voice.require_job_artifacts(job_id)
            self._require_frozen_current(artifacts)
            unit_key = self._next_unit_key(job_id)
            if unit_key is None:
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "runnable presence job has no pending unit",
                )
            external_hash = self._external_input_hash(
                job_id, unit_key, artifacts=artifacts
            )
            self._jobs.begin_unit_in_transaction(
                job_id, unit_key, external_hash
            )
            self._voice.require_job_artifacts(job_id)
            return job_id, False

    def _candidate_jobs_in_transaction(self) -> tuple[tuple[int, str], ...]:
        rows = tuple(
            self._conn.execute(
                "SELECT job.id, job.status, typeof(job.id) AS type_id, "
                "typeof(job.status) AS type_status "
                "FROM voice_verification_manifests AS manifest "
                "JOIN jobs AS job ON job.id=manifest.job_id "
                "ORDER BY job.id"
            )
        )
        result: list[tuple[int, str]] = []
        for row in rows:
            if (
                row["type_id"] != "integer"
                or row["type_status"] != "text"
                or row["status"] not in _JOB_STATUSES
            ):
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "presence job inventory is invalid",
                )
            if row["status"] not in _CANDIDATE_STATUSES:
                continue
            self._jobs.require_canonical_video_pipeline_job(row["id"])
            result.append((row["id"], row["status"]))
        return tuple(result)

    def _canonical_artifacts(self, job_id: int) -> VoiceJobArtifacts:
        self._jobs.require_canonical_video_pipeline_job(job_id)
        artifacts = self._voice.require_job_artifacts(job_id)
        self._require_frozen_current(artifacts)
        self._require_runtime_identity(artifacts.manifest)
        return artifacts

    def _require_frozen_current(self, artifacts: VoiceJobArtifacts) -> None:
        candidate = self._conn.execute(
            "SELECT current_presence_decision_id "
            "FROM subject_video_candidates WHERE id=?",
            (artifacts.manifest.snapshot.candidate_id,),
        ).fetchone()
        if (
            candidate is None
            or type(candidate["current_presence_decision_id"]) is not int
            or candidate["current_presence_decision_id"]
            != artifacts.manifest.snapshot.presence_decision_id
        ):
            raise DomainError(
                "VOICE_PROCESSING_FAILED",
                "presence manifest is no longer current",
            )
        decision = DiscoveryRepository(self._conn).get_presence_decision(
            candidate["current_presence_decision_id"]
        )
        if decision.decision_hash != artifacts.manifest.snapshot.presence_decision_hash:
            raise DomainError(
                "VOICE_PROCESSING_FAILED",
                "presence manifest is no longer current",
            )

    def _require_runtime_identity(self, manifest: StoredVoiceManifest) -> None:
        snapshot = manifest.snapshot
        if (
            self._runtime.model_name != snapshot.model_name
            or self._runtime.model_version != snapshot.model_version
            or self._runtime.adapter_contract_version != snapshot.adapter_version
            or self._runtime.vad_contract_version != snapshot.vad_contract_version
            or self._runtime.provider != "CPUExecutionProvider"
        ):
            raise DomainError(
                "VOICE_PROCESSING_FAILED",
                "presence runtime identity is invalid",
            )

    def _external_input_hash(
        self,
        job_id: int,
        unit_key: str,
        *,
        artifacts: VoiceJobArtifacts | None = None,
        response: AdapterResponse | None = None,
    ) -> str:
        canonical = artifacts or self._canonical_artifacts(job_id)
        manifest = canonical.manifest
        payload: dict[str, object] = {
            "manifest_hash": manifest.manifest_hash,
            "schema": "presence-worker-external-input.v1",
            "unit_key": unit_key,
        }
        paths = self._audio_paths(job_id, create=False)
        if unit_key == "video:validate":
            payload["presence_decision_hash"] = (
                manifest.snapshot.presence_decision_hash
            )
            payload["reference_feature_hash"] = (
                canonical.reference.feature.feature_sha256
            )
        elif unit_key == "audio:acquire":
            payload.update(
                {
                    "deno_sha256": self._runtime.deno_sha256,
                    "video_id": self._youtube_video_id(manifest),
                    "yt_dlp_sha256": self._runtime.yt_dlp_sha256,
                }
            )
        elif unit_key == "audio:normalize":
            payload.update(
                {
                    "ffmpeg_sha256": self._runtime.ffmpeg_sha256,
                    "source_sha256": _file_sha256(paths.source),
                }
            )
        elif unit_key in {"voice:vad", "voice:score", "voice:proposal"}:
            request = self._adapter_request(canonical, paths.normalized)
            payload.update(
                {
                    "adapter_input_hash": request.input_hash,
                    "model_sha256": self._runtime.model_sha256,
                    "normalized_audio_sha256": request.audio_sha256,
                    "reference_feature_sha256": (
                        canonical.reference.feature.feature_sha256
                    ),
                    "vad_model_sha256": self._runtime.vad_sha256,
                }
            )
            if unit_key == "voice:proposal":
                effective_response = response or self._adapter_response(
                    job_id, canonical
                )
                if effective_response.input_hash != request.input_hash:
                    raise DomainError(
                        "VOICE_ADAPTER_RESPONSE_INVALID",
                        "adapter response is invalid",
                    )
                payload["adapter_output_hash"] = effective_response.output_hash
        elif unit_key == "audio:cleanup":
            payload["artifacts"] = [
                {
                    "id": item.id,
                    "path_hash": sha256_text(str(item.local_path)),
                }
                for item in self._job_audio_artifacts(job_id)
            ]
        else:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence unit is unsupported"
            )
        return _hash_payload(payload)

    def _require_current_unit_input(
        self,
        job_id: int,
        unit_key: str,
        artifacts: VoiceJobArtifacts,
        *,
        response: AdapterResponse | None = None,
    ) -> None:
        unit = self._jobs.unit(job_id, unit_key)
        current_hash = self._external_input_hash(
            job_id,
            unit_key,
            artifacts=artifacts,
            response=response,
        )
        if unit.external_input_hash != current_hash:
            raise DomainError(
                "VOICE_PROCESSING_FAILED",
                "presence unit input changed",
            )

    def _current_unit_artifact_hash(
        self,
        job_id: int,
        unit_key: str,
        artifacts: VoiceJobArtifacts,
    ) -> str:
        paths = self._audio_paths(job_id, create=False)
        if unit_key == "video:validate":
            return self._video_artifact_hash(artifacts)
        if unit_key == "audio:acquire":
            return _file_sha256(paths.source)
        if unit_key == "audio:normalize":
            return _file_sha256(paths.normalized)
        if unit_key in {"voice:vad", "voice:score"}:
            response = self._responses.get(job_id)
            request = self._adapter_request(artifacts, paths.normalized)
            if response is None or response.input_hash != request.input_hash:
                raise DomainError(
                    "VOICE_ADAPTER_RESPONSE_INVALID",
                    "adapter response is invalid",
                )
            return (
                self._vad_artifact_hash(artifacts, response)
                if unit_key == "voice:vad"
                else self._score_artifact_hash(artifacts, response)
            )
        if unit_key == "audio:cleanup":
            cleanup_hash = self._cleanup_artifact_hash(job_id)
            if cleanup_hash is not None:
                return cleanup_hash
        raise DomainError(
            "VOICE_PROCESSING_FAILED",
            "presence unit artifact is unavailable",
        )

    def _adapter_request(
        self, artifacts: VoiceJobArtifacts, audio_path: Path
    ) -> AdapterRequest:
        threshold = self._threshold(artifacts.manifest)
        feature = artifacts.reference.feature.embedding_blob
        return AdapterRequest.with_canonical_hash(
            adapter_contract_version=self._runtime.adapter_contract_version,
            audio_duration_ms=normalized_wav_duration_ms(audio_path),
            audio_path=str(audio_path),
            audio_sha256=_file_sha256(audio_path),
            interviewer_boundary=threshold.interviewer_boundary,
            model_name=self._runtime.model_name,
            model_path=str(self._runtime.model_path),
            model_sha256=self._runtime.model_sha256,
            model_version=self._runtime.model_version,
            reference_feature_b64=base64.b64encode(feature).decode("ascii"),
            reference_feature_length=len(feature),
            reference_feature_sha256=(
                artifacts.reference.feature.feature_sha256
            ),
            subject_boundary=threshold.subject_boundary,
            threshold_config_version=threshold.version,
            vad_contract_version=self._runtime.vad_contract_version,
            vad_model_path=str(self._runtime.vad_path),
            vad_model_sha256=self._runtime.vad_sha256,
        )

    def _adapter_response(
        self, job_id: int, artifacts: VoiceJobArtifacts
    ) -> AdapterResponse:
        cached = self._responses.get(job_id)
        request = self._adapter_request(
            artifacts, self._audio_paths(job_id, create=False).normalized
        )
        if cached is not None and cached.input_hash == request.input_hash:
            return cached
        response = self._adapter.score(request)
        if type(response) is not AdapterResponse:
            raise DomainError(
                "VOICE_ADAPTER_RESPONSE_INVALID",
                "adapter response is invalid",
            )
        if (
            response.input_hash != request.input_hash
            or response.model_name != request.model_name
            or response.model_version != request.model_version
            or response.adapter_contract_version
            != request.adapter_contract_version
            or response.vad_contract_version != request.vad_contract_version
            or not response.segments
        ):
            raise DomainError(
                "VOICE_ADAPTER_RESPONSE_INVALID",
                "adapter response is invalid",
            )
        self._responses[job_id] = response
        return response

    def _threshold(self, manifest: StoredVoiceManifest) -> _Threshold:
        rows = tuple(
            self._conn.execute(
                "SELECT version, model_name, model_version, subject_operator, "
                "subject_boundary, interviewer_operator, interviewer_boundary, "
                "is_active FROM speaker_threshold_configs WHERE version=?",
                (manifest.snapshot.threshold_config_version,),
            )
        )
        if len(rows) != 1:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence threshold is invalid"
            )
        row = rows[0]
        if (
            row["version"] != manifest.snapshot.threshold_config_version
            or row["model_name"] != manifest.snapshot.model_name
            or row["model_version"] != manifest.snapshot.model_version
            or row["subject_operator"] != "gte"
            or row["interviewer_operator"] != "lte"
            or type(row["subject_boundary"]) is not float
            or type(row["interviewer_boundary"]) is not float
            or not isfinite(row["subject_boundary"])
            or not isfinite(row["interviewer_boundary"])
            or row["subject_boundary"] <= row["interviewer_boundary"]
            or row["is_active"] != 1
        ):
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence threshold is invalid"
            )
        return _Threshold(
            version=row["version"],
            subject_boundary=row["subject_boundary"],
            interviewer_boundary=row["interviewer_boundary"],
        )

    def _verified_artifact_hashes(
        self, artifacts: VoiceJobArtifacts
    ) -> dict[str, str]:
        job_id = artifacts.manifest.job_id
        result = {"video:validate": self._video_artifact_hash(artifacts)}
        paths = self._audio_paths(job_id, create=False)
        source_hash = self._verified_audio_output_hash(
            job_id, "audio:acquire", paths.source
        )
        if source_hash is not None:
            result["audio:acquire"] = source_hash
        normalized_hash = self._verified_audio_output_hash(
            job_id, "audio:normalize", paths.normalized
        )
        if normalized_hash is not None:
            result["audio:normalize"] = normalized_hash
        response: AdapterResponse | None = None
        if artifacts.run is None and "audio:normalize" in result:
            response = self._adapter_response(job_id, artifacts)
        if artifacts.run is not None and normalized_hash is not None:
            result["voice:vad"] = self._stored_vad_artifact_hash(
                artifacts, artifacts.run, normalized_hash
            )
            result["voice:score"] = self._stored_score_artifact_hash(
                artifacts, artifacts.run, normalized_hash
            )
            result["voice:proposal"] = artifacts.run.output_hash
        elif response is not None:
            result["voice:vad"] = self._vad_artifact_hash(artifacts, response)
            result["voice:score"] = self._score_artifact_hash(
                artifacts, response
            )
        cleanup_hash = self._cleanup_artifact_hash(job_id)
        if cleanup_hash is not None:
            result["audio:cleanup"] = cleanup_hash
        return result

    def _verified_audio_output_hash(
        self, job_id: int, unit_key: str, path: Path
    ) -> str | None:
        unit = self._jobs.unit(job_id, unit_key)
        if unit.status is not UnitStatus.SUCCESS or unit.output_hash is None:
            return None
        if path.exists():
            return _file_sha256(path)
        rows = self._artifacts_for_path(path)
        if (
            rows
            and all(item.status == "deleted" for item in rows)
        ):
            return unit.output_hash
        return None

    def _video_artifact_hash(self, artifacts: VoiceJobArtifacts) -> str:
        snapshot = artifacts.manifest.snapshot
        return _hash_payload(
            {
                "candidate_id": snapshot.candidate_id,
                "manifest_hash": artifacts.manifest.manifest_hash,
                "presence_decision_hash": snapshot.presence_decision_hash,
                "reference_feature_hash": (
                    artifacts.reference.feature.feature_sha256
                ),
                "schema": "presence-video-validation.v1",
                "video_id": snapshot.video_id,
            }
        )

    def _vad_artifact_hash(
        self, artifacts: VoiceJobArtifacts, response: AdapterResponse
    ) -> str:
        return _hash_payload(
            {
                "audio_sha256": self._adapter_request(
                    artifacts,
                    self._audio_paths(
                        artifacts.manifest.job_id, create=False
                    ).normalized,
                ).audio_sha256,
                "schema": "presence-vad-artifact.v1",
                "segments": [
                    {
                        "end_ms": item.end_ms,
                        "ordinal": item.ordinal,
                        "start_ms": item.start_ms,
                    }
                    for item in response.segments
                ],
                "vad_model_sha256": self._runtime.vad_sha256,
            }
        )

    def _score_artifact_hash(
        self, artifacts: VoiceJobArtifacts, response: AdapterResponse
    ) -> str:
        return _hash_payload(
            {
                "adapter_output_hash": response.output_hash,
                "model_sha256": self._runtime.model_sha256,
                "reference_feature_sha256": (
                    artifacts.reference.feature.feature_sha256
                ),
                "schema": "presence-score-artifact.v1",
                "segments": [
                    {
                        "evidence_hash": item.evidence_hash,
                        "ordinal": item.ordinal,
                        "raw_score": item.raw_score,
                    }
                    for item in response.segments
                ],
            }
        )

    def _stored_vad_artifact_hash(
        self,
        artifacts: VoiceJobArtifacts,
        run: StoredVoiceRun,
        normalized_sha256: str,
    ) -> str:
        return _hash_payload(
            {
                "audio_sha256": normalized_sha256,
                "schema": "presence-vad-artifact.v1",
                "segments": [
                    {
                        "end_ms": item.end_ms,
                        "ordinal": item.ordinal,
                        "start_ms": item.start_ms,
                    }
                    for item in run.segments
                ],
                "vad_model_sha256": self._runtime.vad_sha256,
            }
        )

    def _stored_score_artifact_hash(
        self,
        artifacts: VoiceJobArtifacts,
        run: StoredVoiceRun,
        normalized_sha256: str,
    ) -> str:
        return _hash_payload(
            {
                "adapter_output_hash": self._adapter_output_hash_from_run(
                    artifacts, run, normalized_sha256
                ),
                "model_sha256": self._runtime.model_sha256,
                "reference_feature_sha256": (
                    artifacts.reference.feature.feature_sha256
                ),
                "schema": "presence-score-artifact.v1",
                "segments": [
                    {
                        "evidence_hash": self._adapter_evidence_hash(
                            normalized_sha256, item
                        ),
                        "ordinal": item.ordinal,
                        "raw_score": item.raw_match_score,
                    }
                    for item in run.segments
                ],
            }
        )

    def _adapter_output_hash_from_run(
        self,
        artifacts: VoiceJobArtifacts,
        run: StoredVoiceRun,
        normalized_sha256: str,
    ) -> str:
        values: dict[str, object] = {
            "adapter_contract_version": artifacts.manifest.snapshot.adapter_version,
            "input_hash": run.input_hash,
            "model_name": artifacts.manifest.snapshot.model_name,
            "model_version": artifacts.manifest.snapshot.model_version,
            "proposal": run.proposal.value,
            "segments": [
                {
                    "end_ms": item.end_ms,
                    "evidence_hash": self._adapter_evidence_hash(
                        normalized_sha256, item
                    ),
                    "ordinal": item.ordinal,
                    "raw_score": item.raw_match_score,
                    "start_ms": item.start_ms,
                }
                for item in run.segments
            ],
            "vad_contract_version": (
                artifacts.manifest.snapshot.vad_contract_version
            ),
        }
        return _hash_payload(values)

    def _adapter_evidence_hash(
        self,
        normalized_sha256: str,
        segment: VoiceSegmentScore,
    ) -> str:
        return _hash_payload(
            {
                "audio_sha256": normalized_sha256,
                "end_ms": segment.end_ms,
                "ordinal": segment.ordinal,
                "raw_score": segment.raw_match_score,
                "start_ms": segment.start_ms,
            }
        )

    def _cleanup_audio(self, job_id: int) -> str:
        artifacts = self._job_audio_artifacts(job_id)
        if not artifacts:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "audio cleanup inventory is invalid"
            )
        failure_code: str | None = None
        for artifact in artifacts:
            result = self._retention_service.delete_audio(artifact.id)
            if type(result) is not AudioDeletionResult:
                failure_code = "VOICE_PROCESSING_FAILED"
            elif not result.deleted and failure_code is None:
                failure_code = result.error_code or "VOICE_PROCESSING_FAILED"
        if failure_code is not None:
            raise DomainError(failure_code, "audio cleanup failed")
        output_hash = self._cleanup_artifact_hash(job_id)
        if output_hash is None:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "audio cleanup could not be verified"
            )
        return output_hash

    def _cleanup_artifact_hash(self, job_id: int) -> str | None:
        paths = self._audio_paths(job_id, create=False)
        artifacts = self._job_audio_artifacts(job_id)
        if (
            not artifacts
            or any(item.status != "deleted" for item in artifacts)
            or any(os.path.lexists(path) for path in paths.all)
        ):
            return None
        return _hash_payload(
            {
                "artifacts": [
                    {
                        "deleted_at": item.deleted_at.isoformat(),
                        "id": item.id,
                        "retry_count": item.retry_count,
                    }
                    for item in artifacts
                ],
                "schema": "presence-audio-cleanup.v1",
            }
        )

    def _register_audio(self, path: Path) -> int:
        existing = tuple(
            item
            for item in self._artifacts_for_path(path)
            if item.status != "deleted"
        )
        if len(existing) == 1:
            return existing[0].id
        if existing:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "audio artifact inventory is invalid"
            )
        with transaction(self._conn):
            artifact_id = self._retention.add_audio_artifact(
                path, created_at=self._clock()
            )
            stored = self._retention.get_audio_artifact(artifact_id)
            if stored.local_path != path or stored.status != "pending":
                raise DomainError(
                    "VOICE_PROCESSING_FAILED",
                    "audio artifact registration is invalid",
                )
            return artifact_id

    def _job_audio_artifacts(self, job_id: int) -> tuple[LocalArtifact, ...]:
        paths = self._audio_paths(job_id, create=False)
        result: list[LocalArtifact] = []
        for path in paths.all:
            result.extend(self._artifacts_for_path(path))
        return tuple(sorted(result, key=lambda item: item.id))

    def _artifacts_for_path(self, path: Path) -> tuple[LocalArtifact, ...]:
        rows = tuple(
            self._conn.execute(
                "SELECT id FROM local_artifacts WHERE local_path=? ORDER BY id",
                (str(path),),
            )
        )
        return tuple(
            self._retention.get_audio_artifact(row["id"]) for row in rows
        )

    def _discard_stale_audio(self, job_id: int, *, from_unit: str) -> None:
        paths = self._audio_paths(job_id, create=False)
        targets = (
            paths.all
            if from_unit == "audio:acquire"
            else (paths.normalized,)
        )
        for path in targets:
            for artifact in self._artifacts_for_path(path):
                result = self._retention_service.delete_audio(artifact.id)
                if not result.deleted:
                    raise DomainError(
                        result.error_code or "VOICE_PROCESSING_FAILED",
                        "stale audio cleanup failed",
                    )
        if any(os.path.lexists(path) for path in targets):
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "stale audio cleanup failed"
            )

    def _audio_paths(self, job_id: int, *, create: bool) -> _AudioPaths:
        workspace = self._root / f"presence-job-{job_id:020d}"
        if create and not workspace.exists():
            workspace.mkdir(mode=0o700)
        if workspace.exists():
            captured = PrivateJobWorkspace.capture(workspace)
            workspace = captured.path
        return _AudioPaths(
            workspace=workspace,
            source=workspace / "source.media",
            part=workspace / "source.media.part",
            normalized=workspace / "normalized.wav",
        )

    def _youtube_video_id(self, manifest: StoredVoiceManifest) -> str:
        row = self._conn.execute(
            "SELECT youtube_video_id FROM videos WHERE id=?",
            (manifest.snapshot.video_id,),
        ).fetchone()
        if row is None or type(row["youtube_video_id"]) is not str:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence video identity is invalid"
            )
        return row["youtube_video_id"]

    def _next_unit_key(self, job_id: int) -> str | None:
        units = tuple(self._jobs.unit(job_id, key) for key in _UNIT_KEYS)
        running = tuple(
            unit for unit in units if unit.status is UnitStatus.RUNNING
        )
        if len(running) > 1:
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence unit state is invalid"
            )
        if running:
            return running[0].unit_key
        pending = tuple(
            unit for unit in units if unit.status is UnitStatus.PENDING
        )
        return None if not pending else pending[0].unit_key

    @staticmethod
    def _require_same_artifacts(
        before: VoiceJobArtifacts, after: VoiceJobArtifacts
    ) -> None:
        if (
            before.manifest != after.manifest
            or before.reference != after.reference
            or before.run != after.run
        ):
            raise DomainError(
                "VOICE_PROCESSING_FAILED", "presence artifacts changed"
            )


def _file_sha256(path: Path) -> str:
    try:
        if not path.is_absolute() or not path.is_file():
            raise ValueError("artifact is unavailable")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        raise DomainError(
            "VOICE_PROCESSING_FAILED", "presence artifact is unavailable"
        ) from None


def _database_wake_mutex_name(conn: sqlite3.Connection) -> str:
    rows = tuple(conn.execute("PRAGMA database_list"))
    main_rows = tuple(row for row in rows if row[1] == "main")
    if len(main_rows) != 1 or type(main_rows[0][2]) is not str:
        raise ValueError("invalid main database identity")
    database_path = Path(main_rows[0][2]).resolve(strict=True)
    if not database_path.is_file():
        raise ValueError("invalid main database identity")
    canonical_identity = os.path.normcase(str(database_path))
    digest = hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
    return f"Local\\MarketVoiceForecastLedger-PresenceWake-{digest}"


def _windows_mutex_api():
    if os.name != "nt" or _winapi is None:
        raise OSError("presence wake ownership is unavailable")
    return _winapi


def _try_acquire_windows_mutex(name: str) -> int | None:
    kernel32 = _windows_mutex_api()
    handle = kernel32.CreateMutexW(0, False, name)
    outcome = kernel32.WaitForSingleObject(handle, 0)
    if outcome in {_WAIT_OBJECT_0, _WAIT_ABANDONED}:
        return handle
    kernel32.CloseHandle(handle)
    if outcome == _WAIT_TIMEOUT:
        return None
    raise OSError("wake ownership failed")


def _release_windows_mutex(handle: int) -> None:
    kernel32 = _windows_mutex_api()
    try:
        kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


def _hash_payload(value: object) -> str:
    return sha256_text(canonical_json(value))


def _safe_failure_code(cause: Exception) -> str:
    if isinstance(cause, DomainError) and cause.code in _KNOWN_FAILURE_CODES:
        return cause.code
    return "VOICE_PROCESSING_FAILED"


def _safe_domain_error(cause: Exception) -> DomainError:
    return DomainError(_safe_failure_code(cause), "presence processing failed")


def _empty_summary() -> PresenceWorkerSummary:
    return PresenceWorkerSummary(None, 0, 0, 0, 0, None)


def _succeeded_summary(job_id: int) -> PresenceWorkerSummary:
    return PresenceWorkerSummary(job_id, 1, 0, 0, 0, None)


def _failed_summary(job_id: int | None, code: str) -> PresenceWorkerSummary:
    return PresenceWorkerSummary(job_id, 0, 1, 0, 0, code)


def _boundary_summary(
    job_id: int, status: JobStatus
) -> PresenceWorkerSummary | None:
    if status is JobStatus.PAUSED:
        return PresenceWorkerSummary(job_id, 0, 0, 1, 0, None)
    if status is JobStatus.STOPPED:
        return PresenceWorkerSummary(job_id, 0, 0, 0, 1, None)
    return None
