from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
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
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.api.models import (
    JobResponse,
    JobUnitResponse,
    StageProgressResponse,
    SubjectResponse,
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
