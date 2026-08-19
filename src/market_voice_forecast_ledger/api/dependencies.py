from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable

from market_voice_forecast_ledger.api.models import (
    JobResponse,
    JobUnitResponse,
    StageProgressResponse,
    SubjectResponse,
    YouTubeSyncStatusResponse,
    YouTubeSyncUnitResponse,
)
from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.discovery import (
    DiscoverySourceKind,
    YouTubeSyncKind,
    build_youtube_sync_shape,
)
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    STAGE_ORDER,
    JobManifest,
    ManifestUnit,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.services.youtube_sync import YouTubeSyncService
from market_voice_forecast_ledger.windows.task_scheduler import (
    TaskSchedulerAdapter,
    TaskWakeAdapter,
)


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")


def initialize_database(settings: Settings) -> None:
    if type(settings) is not Settings:
        raise DomainError("SETTINGS_INVALID", "settings are invalid")
    conn = None
    try:
        conn = open_database(settings.database_path)
        apply_migrations(conn)
        bootstrap_reference_data(conn)
    except DomainError:
        raise
    except Exception:
        raise DomainError(
            "DATABASE_INITIALIZATION_FAILED", "database initialization failed"
        ) from None
    finally:
        if conn is not None:
            conn.close()


async def get_connection() -> AsyncIterator[sqlite3.Connection]:
    raise RuntimeError("application connection dependency is not configured")
    yield  # pragma: no cover


def connection_dependency(
    settings: Settings,
) -> Callable[[], AsyncIterator[sqlite3.Connection]]:
    database_path = settings.database_path

    async def request_connection() -> AsyncIterator[sqlite3.Connection]:
        conn = open_database(database_path)
        try:
            yield conn
        finally:
            conn.close()

    return request_connection


def get_settings() -> Settings:
    raise RuntimeError("application settings dependency is not configured")


def settings_dependency(settings: Settings) -> Callable[[], Settings]:
    def request_settings() -> Settings:
        return settings

    return request_settings


def get_task_wake_adapter() -> TaskWakeAdapter:
    return TaskSchedulerAdapter()


def task_wake_dependency(
    adapter: TaskWakeAdapter,
) -> Callable[[], TaskWakeAdapter]:
    if not callable(getattr(adapter, "request_start", None)):
        raise DomainError(
            "YOUTUBE_SYNC_DEPENDENCY_INVALID",
            "YouTube sync wake dependency is invalid",
        )

    def request_adapter() -> TaskWakeAdapter:
        return adapter

    return request_adapter


class PublicReadAdapter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list_subjects(self) -> tuple[SubjectResponse, ...]:
        rows = tuple(
            self._conn.execute(
                """
                SELECT
                    subject.id,
                    subject.canonical_name,
                    subject.is_active
                FROM analysis_subjects AS subject
                ORDER BY subject.id
                """
            )
        )
        subjects: list[SubjectResponse] = []
        try:
            for row in rows:
                subject_id = _positive_int(row["id"])
                display_name = _bounded_text(row["canonical_name"], 200)
                if type(row["is_active"]) is not int or row["is_active"] not in (0, 1):
                    raise ValueError("invalid active flag")
                subjects.append(
                    SubjectResponse(
                        id=subject_id,
                        key=f"subject-{subject_id}",
                        display_name=display_name,
                        is_active=bool(row["is_active"]),
                    )
                )
        except (KeyError, TypeError, ValueError) as cause:
            raise DomainError(
                "SUBJECT_STORED_INVALID", "stored subject configuration is invalid"
            ) from cause
        return tuple(subjects)

    def read_job(self, job_id: int) -> JobResponse:
        if type(job_id) is not int or job_id <= 0:
            raise DomainError("JOB_ID_INVALID", "job id is invalid")
        job = JobRepository(self._conn).get(job_id)
        rows = tuple(
            self._conn.execute(
                """
                SELECT
                    unit_key,
                    stage,
                    ordinal,
                    status,
                    declared_input_hash,
                    dependency_keys_json,
                    execution_contract_hash,
                    external_input_hash,
                    bound_input_hash,
                    output_hash,
                    attempt_count,
                    error_code
                FROM job_units
                WHERE job_id=?
                ORDER BY ordinal
                """,
                (job_id,),
            )
        )
        try:
            if (
                type(job.id) is not int
                or job.id != job_id
                or type(job.total_units) is not int
                or job.total_units <= 0
                or len(rows) != job.total_units
                or not _SAFE_TOKEN.fullmatch(job.manifest_hash)
                or type(job.kind) is not JobKind
                or type(job.status) is not JobStatus
            ):
                raise ValueError("invalid job header")
            units: list[JobUnitResponse] = []
            parsed: list[tuple[JobStage, UnitStatus]] = []
            manifest_units: list[ManifestUnit] = []
            for expected_ordinal, row in enumerate(rows, start=1):
                if row["ordinal"] != expected_ordinal:
                    raise ValueError("invalid unit ordinal")
                stage = JobStage(row["stage"])
                status = UnitStatus(row["status"])
                _require_safe_token(row["unit_key"])
                _require_optional_safe_token(row["declared_input_hash"])
                _require_safe_token(row["execution_contract_hash"])
                _require_optional_safe_token(row["external_input_hash"])
                _require_optional_safe_token(row["bound_input_hash"])
                _require_optional_safe_token(row["output_hash"])
                dependencies = json.loads(row["dependency_keys_json"])
                if type(dependencies) is not list or any(
                    type(item) is not str or not _SAFE_TOKEN.fullmatch(item)
                    for item in dependencies
                ):
                    raise ValueError("invalid unit dependencies")
                if type(row["attempt_count"]) is not int or row["attempt_count"] < 0:
                    raise ValueError("invalid attempt count")
                error_code = row["error_code"]
                if error_code is not None and (
                    type(error_code) is not str or not _SAFE_ERROR.fullmatch(error_code)
                ):
                    raise ValueError("invalid error code")
                if (status is UnitStatus.FAILED) != (error_code is not None):
                    raise ValueError("invalid unit error state")
                units.append(
                    JobUnitResponse(
                        stage=stage.value,
                        status=status.value,
                        ordinal=expected_ordinal,
                        error_code=error_code,
                    )
                )
                parsed.append((stage, status))
                manifest_units.append(
                    ManifestUnit(
                        unit_key=row["unit_key"],
                        stage=stage,
                        ordinal=expected_ordinal,
                        declared_input_hash=row["declared_input_hash"],
                        dependency_keys=tuple(dependencies),
                        execution_contract_hash=row[
                            "execution_contract_hash"
                        ],
                    )
                )
            try:
                sealed_manifest = JobManifest.build(job.kind, manifest_units)
            except DomainError as cause:
                raise ValueError("invalid sealed manifest") from cause
            if sealed_manifest.manifest_hash != job.manifest_hash:
                raise ValueError("stored manifest hash mismatch")
            if not _job_state_is_coherent(
                job.status, tuple(status for _, status in parsed)
            ):
                raise ValueError("stored job state is incoherent")
            stages = tuple(
                StageProgressResponse(
                    stage=stage.value,
                    completed=sum(
                        unit_stage is stage and status is UnitStatus.SUCCESS
                        for unit_stage, status in parsed
                    ),
                    total=sum(unit_stage is stage for unit_stage, _ in parsed),
                )
                for stage in STAGE_ORDER
            )
            completed = sum(status is UnitStatus.SUCCESS for _, status in parsed)
            return JobResponse(
                job_id=job.id,
                kind=job.kind.value,
                status=job.status.value,
                completed=completed,
                total=job.total_units,
                stages=stages,
                units=tuple(units),
            )
        except DomainError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as cause:
            raise DomainError(
                "JOB_STORED_INVALID", "stored job progress is invalid"
            ) from cause

    def read_youtube_sync_status(self, job_id: int) -> YouTubeSyncStatusResponse:
        if type(job_id) is not int or job_id <= 0:
            raise DomainError("JOB_ID_INVALID", "job id is invalid")
        try:
            job = self.read_job(job_id)
            if job.kind != JobKind.YOUTUBE_SYNC.value:
                raise DomainError(
                    "YOUTUBE_SYNC_NOT_FOUND", "YouTube sync job does not exist"
                )
            repository = DiscoveryRepository(self._conn)
            manifest = YouTubeSyncService(self._conn).get_sync_manifest(job_id)
            sync_kind = YouTubeSyncKind(manifest.sync_kind)
            profiles = tuple(
                repository.get_profile_version(profile.profile_version_id)
                for profile in manifest.profiles
            )
            manual_video_id = None
            if sync_kind is YouTubeSyncKind.MANUAL:
                if manifest.manual_request_id is None or len(profiles) != 1:
                    raise ValueError("invalid manual manifest")
                profile_id, manual_video_id = repository.manual_request_binding(
                    manifest.manual_request_id
                )
                if profiles[0].profile_id != profile_id:
                    raise ValueError("invalid manual profile binding")
            _, unit_specs = build_youtube_sync_shape(
                sync_kind=sync_kind,
                profiles=profiles,
                upper_bound=manifest.upper_bound,
                backfill_floor=manifest.backfill_floor,
                quota_contract_version=manifest.quota_contract_version,
                manual_request_id=manifest.manual_request_id,
                manual_video_id=manual_video_id,
            )
            if len(unit_specs) != job.total or len(job.units) != job.total:
                raise ValueError("invalid YouTube unit set")

            verified_artifacts = repository.verified_youtube_artifact_hashes(
                job_id
            )
            checkpoints = tuple(
                repository.get_youtube_sync_checkpoint(job_id, spec.unit_key)
                for spec in unit_specs
            )
            for spec, checkpoint, unit in zip(
                unit_specs, checkpoints, job.units, strict=True
            ):
                if (
                    checkpoint.job_id != job_id
                    or checkpoint.unit_key != spec.unit_key
                    or checkpoint.source_kind is not spec.source_kind
                    or checkpoint.source_key != spec.source_key
                    or unit.stage != spec.stage.value
                    or (unit.status == UnitStatus.SUCCESS.value)
                    != (checkpoint.completed_at is not None)
                    or (unit.status == UnitStatus.SUCCESS.value)
                    != (spec.unit_key in verified_artifacts)
                    or not set(checkpoint.unavailable_video_ids).issubset(
                        checkpoint.encountered_video_ids
                    )
                ):
                    raise ValueError("invalid YouTube checkpoint provenance")
                if spec.source_kind is DiscoverySourceKind.CROSS_CHANNEL_SEARCH:
                    repository.next_search_window(job_id, spec.unit_key)

            observed: dict[tuple[int, str, str], list[tuple[int, str]]] = {}
            for row in self._conn.execute(
                "SELECT observation.id, observation.profile_id, "
                "observation.source_kind, observation.source_key, "
                "observation.metadata_snapshot_id, video.youtube_video_id, "
                "video.current_metadata_snapshot_id "
                "FROM discovery_observations AS observation "
                "LEFT JOIN videos AS video ON video.id=observation.video_id "
                "WHERE observation.job_id=? ORDER BY observation.id",
                (job_id,),
            ):
                key = (row["profile_id"], row["source_kind"], row["source_key"])
                observation_id = row["id"]
                youtube_video_id = row["youtube_video_id"]
                if (
                    type(key[0]) is not int
                    or type(key[1]) is not str
                    or type(key[2]) is not str
                    or type(observation_id) is not int
                    or type(youtube_video_id) is not str
                    or type(row["metadata_snapshot_id"]) is not int
                    or row["current_metadata_snapshot_id"]
                    != row["metadata_snapshot_id"]
                ):
                    raise ValueError("invalid YouTube observation provenance")
                observed.setdefault(key, []).append(
                    (observation_id, youtube_video_id)
                )

            expected_keys = {
                (spec.profile_id, spec.source_kind.value, spec.source_key)
                for spec in unit_specs
            }
            if any(key not in expected_keys for key in observed):
                raise ValueError("invalid YouTube observation source")

            units: list[YouTubeSyncUnitResponse] = []
            for spec, checkpoint, unit in zip(
                unit_specs, checkpoints, job.units, strict=True
            ):
                observed_rows = observed.get(
                    (spec.profile_id, spec.source_kind.value, spec.source_key), []
                )
                observed_ids = tuple(row[0] for row in observed_rows)
                if any(
                    video_id not in checkpoint.encountered_video_ids
                    for _, video_id in observed_rows
                ):
                    raise ValueError("invalid YouTube observation binding")
                if spec.source_kind is DiscoverySourceKind.SEED_UPLOADS:
                    _, canonical_ids = repository.seed_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                elif (
                    spec.source_kind
                    is DiscoverySourceKind.CROSS_CHANNEL_SEARCH
                ):
                    _, canonical_ids = repository.search_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                elif (
                    unit.status == UnitStatus.SUCCESS.value or observed_ids
                ):
                    if manifest.manual_request_id is None:
                        raise ValueError("invalid manual observation binding")
                    _, canonical_ids = repository.manual_unit_artifact(
                        job_id=job_id,
                        unit_key=spec.unit_key,
                        manual_request_id=manifest.manual_request_id,
                        profile_version_id=spec.profile_version_id,
                        profile_id=spec.profile_id,
                        source_key=spec.source_key,
                    )
                else:
                    canonical_ids = ()
                if canonical_ids != observed_ids:
                    raise ValueError("invalid YouTube observation binding")
                units.append(
                    YouTubeSyncUnitResponse(
                        stage=unit.stage,
                        status=unit.status,
                        discovered_count=len(checkpoint.encountered_video_ids),
                        persisted_count=len(observed_ids),
                        unavailable_count=len(checkpoint.unavailable_video_ids),
                        error_code=unit.error_code,
                    )
                )
            response_units = tuple(units)
            return YouTubeSyncStatusResponse(
                job_id=job.job_id,
                status=job.status,
                completed_units=job.completed,
                total_units=job.total,
                resume_not_before_utc=(
                    None
                    if manifest.resume_not_before_utc is None
                    else utc_iso(manifest.resume_not_before_utc)
                ),
                discovered_total=sum(
                    unit.discovered_count for unit in response_units
                ),
                persisted_total=sum(unit.persisted_count for unit in response_units),
                unavailable_total=sum(
                    unit.unavailable_count for unit in response_units
                ),
                units=response_units,
            )
        except DomainError as cause:
            if cause.code in {"JOB_NOT_FOUND", "YOUTUBE_SYNC_NOT_FOUND"}:
                raise
            raise DomainError(
                "YOUTUBE_SYNC_STORED_INVALID",
                "stored YouTube sync progress is invalid",
            ) from cause
        except (KeyError, LookupError, TypeError, ValueError) as cause:
            raise DomainError(
                "YOUTUBE_SYNC_STORED_INVALID",
                "stored YouTube sync progress is invalid",
            ) from cause

    def stale_scope_count_for_segment(
        self, segment_id: int, assigned_subject_id: int | None = None
    ) -> int:
        if type(segment_id) is not int or segment_id <= 0:
            raise DomainError("SEGMENT_ID_INVALID", "segment id is invalid")
        scope_ids = AnalysisRepository(
            self._conn
        ).scope_ids_affected_by_speaker_correction(
            segment_id, assigned_subject_id
        )
        return len(scope_ids)


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("positive integer required")
    return value


def _bounded_text(value: object, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("bounded text required")
    return value


def _require_safe_token(value: object) -> None:
    if type(value) is not str or not _SAFE_TOKEN.fullmatch(value):
        raise ValueError("safe token required")


def _require_optional_safe_token(value: object) -> None:
    if value is not None:
        _require_safe_token(value)


def _job_state_is_coherent(
    status: JobStatus, unit_statuses: tuple[UnitStatus, ...]
) -> bool:
    running = sum(item is UnitStatus.RUNNING for item in unit_statuses)
    failed = sum(item is UnitStatus.FAILED for item in unit_statuses)
    if status is JobStatus.SUCCEEDED:
        return all(item is UnitStatus.SUCCESS for item in unit_statuses)
    if status in {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED}:
        return running > 0
    if status is JobStatus.FAILED:
        return failed > 0
    if status in {
        JobStatus.QUEUED,
        JobStatus.PAUSED,
        JobStatus.RETRYING,
    }:
        return running == 0 and failed == 0
    if status is JobStatus.STOPPED:
        return running == 0
    return failed == 0
