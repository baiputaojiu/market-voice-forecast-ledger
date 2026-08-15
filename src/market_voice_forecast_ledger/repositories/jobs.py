import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from market_voice_forecast_ledger.domain.enums import (
    EligibilityStatus,
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobManifest, JobUnit


_SAFE_EVENT_METADATA_KEYS: Final = frozenset(
    {
        "attempt_no",
        "error_code",
        "from_status",
        "output_hash",
        "reason",
        "result_status",
        "source_job_id",
        "to_status",
    }
)


@dataclass(frozen=True, slots=True)
class StoredJob:
    id: int
    source_job_id: int | None
    kind: JobKind
    manifest_hash: str
    total_units: int
    status: JobStatus


class JobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(
        self,
        manifest: JobManifest,
        *,
        source_job_id: int | None,
        created_at: datetime,
    ) -> int:
        self._require_transaction()
        timestamp = utc_iso(created_at)
        cursor = self._conn.execute(
            """
            INSERT INTO jobs(
                source_job_id,
                job_kind,
                manifest_hash,
                total_units,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_job_id,
                manifest.kind.value,
                manifest.manifest_hash,
                len(manifest.units),
                JobStatus.QUEUED.value,
                timestamp,
                timestamp,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("job insert did not return an id")
        job_id = cursor.lastrowid
        self._conn.executemany(
            """
            INSERT INTO job_units(
                job_id,
                unit_key,
                stage,
                ordinal,
                declared_input_hash,
                dependency_keys_json,
                execution_contract_hash,
                status,
                attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                (
                    job_id,
                    unit.unit_key,
                    unit.stage.value,
                    unit.ordinal,
                    unit.declared_input_hash,
                    canonical_json(list(unit.dependency_keys)),
                    unit.execution_contract_hash,
                    UnitStatus.PENDING.value,
                )
                for unit in manifest.units
            ),
        )
        self._append_event(
            job_id,
            None,
            "job_created",
            {"source_job_id": source_job_id},
            created_at,
        )
        return job_id

    def get(self, job_id: int) -> StoredJob:
        row = self._conn.execute(
            """
            SELECT
                id,
                source_job_id,
                job_kind,
                manifest_hash,
                total_units,
                status
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "job does not exist")
        return StoredJob(
            id=row["id"],
            source_job_id=row["source_job_id"],
            kind=JobKind(row["job_kind"]),
            manifest_hash=row["manifest_hash"],
            total_units=row["total_units"],
            status=JobStatus(row["status"]),
        )

    def create_sealed_video_pipeline_bindings(
        self, job_id: int, eligibility_ids: tuple[int, ...]
    ) -> None:
        self._require_transaction()
        self._conn.execute(
            """
            INSERT INTO video_pipeline_job_binding_sets(
                job_id, expected_binding_count, is_sealed
            ) VALUES (?, ?, 0)
            """,
            (job_id, len(eligibility_ids)),
        )
        self._conn.executemany(
            """
            INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id)
            VALUES (?, ?)
            """,
            ((job_id, eligibility_id) for eligibility_id in eligibility_ids),
        )
        cursor = self._conn.execute(
            """
            UPDATE video_pipeline_job_binding_sets
            SET is_sealed=1
            WHERE job_id=? AND is_sealed=0
            """,
            (job_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("video-pipeline binding set was not sealed")

    def copy_video_pipeline_bindings(
        self, source_job_id: int, successor_job_id: int
    ) -> None:
        self._require_transaction()
        eligibility_ids = self.list_video_pipeline_binding_ids(source_job_id)
        if eligibility_ids:
            self.create_sealed_video_pipeline_bindings(
                successor_job_id, eligibility_ids
            )

    def list_video_pipeline_binding_ids(self, job_id: int) -> tuple[int, ...]:
        binding_set = self._conn.execute(
            """
            SELECT
                binding_set.expected_binding_count,
                binding_set.is_sealed,
                job.job_kind,
                (
                    SELECT COUNT(*)
                    FROM video_pipeline_job_bindings AS member
                    WHERE member.job_id=binding_set.job_id
                ) AS member_count,
                (
                    SELECT COUNT(DISTINCT eligibility.video_id)
                    FROM video_pipeline_job_bindings AS member
                    JOIN subject_video_eligibility AS eligibility
                        ON eligibility.id=member.eligibility_id
                    WHERE member.job_id=binding_set.job_id
                ) AS video_count
            FROM video_pipeline_job_binding_sets AS binding_set
            JOIN jobs AS job ON job.id=binding_set.job_id
            WHERE binding_set.job_id=?
            """,
            (job_id,),
        ).fetchone()
        if binding_set is None:
            return ()
        if (
            binding_set["job_kind"] != JobKind.VIDEO_PIPELINE.value
            or binding_set["is_sealed"] != 1
            or binding_set["expected_binding_count"]
            != binding_set["member_count"]
            or binding_set["member_count"] <= 0
            or binding_set["video_count"] != 1
        ):
            raise DomainError(
                "VIDEO_PIPELINE_BINDINGS_INVALID",
                "video-pipeline binding set is incomplete or unsealed",
            )
        rows = self._conn.execute(
            """
            SELECT eligibility_id
            FROM video_pipeline_job_bindings
            WHERE job_id=?
            ORDER BY eligibility_id
            """,
            (job_id,),
        ).fetchall()
        return tuple(row["eligibility_id"] for row in rows)

    def has_current_eligible_video_binding(self, job_id: int) -> bool:
        if not self.list_video_pipeline_binding_ids(job_id):
            return False
        return self._conn.execute(
            """
            SELECT 1
            FROM video_pipeline_job_bindings AS binding
            JOIN subject_video_eligibility AS eligibility
                ON eligibility.id=binding.eligibility_id
            JOIN subject_channel_policies AS policy
                ON policy.id=eligibility.policy_id
                AND policy.subject_id=eligibility.subject_id
                AND policy.policy_hash=eligibility.policy_hash
            WHERE binding.job_id=?
                AND eligibility.status=?
            LIMIT 1
            """,
            (job_id, EligibilityStatus.ELIGIBLE.value),
        ).fetchone() is not None

    def list_video_job_ids_for_eligibility(
        self, eligibility_id: int
    ) -> tuple[int, ...]:
        rows = self._conn.execute(
            """
            SELECT binding.job_id
            FROM video_pipeline_job_bindings AS binding
            JOIN video_pipeline_job_binding_sets AS binding_set
                ON binding_set.job_id=binding.job_id
            JOIN jobs AS job ON job.id=binding.job_id
            WHERE binding.eligibility_id=?
                AND job.job_kind=?
                AND binding_set.is_sealed=1
                AND binding_set.expected_binding_count=(
                    SELECT COUNT(*)
                    FROM video_pipeline_job_bindings AS member
                    WHERE member.job_id=binding.job_id
                )
            ORDER BY binding.job_id
            """,
            (eligibility_id, JobKind.VIDEO_PIPELINE.value),
        ).fetchall()
        return tuple(row["job_id"] for row in rows)

    def get_unit(self, job_id: int, unit_key: str) -> JobUnit:
        row = self._conn.execute(
            """
            SELECT
                job_id,
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
                attempt_count
            FROM job_units
            WHERE job_id = ? AND unit_key = ?
            """,
            (job_id, unit_key),
        ).fetchone()
        if row is None:
            raise DomainError("JOB_UNIT_NOT_FOUND", "job unit does not exist")
        return _unit_from_row(row)

    def list_units(self, job_id: int) -> tuple[JobUnit, ...]:
        self.get(job_id)
        rows = self._conn.execute(
            """
            SELECT
                job_id,
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
                attempt_count
            FROM job_units
            WHERE job_id = ?
            ORDER BY ordinal
            """,
            (job_id,),
        )
        return tuple(_unit_from_row(row) for row in rows)

    def transition_job(
        self,
        job_id: int,
        from_status: JobStatus,
        to_status: JobStatus,
        changed_at: datetime,
    ) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (to_status.value, utc_iso(changed_at), job_id, from_status.value),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_STATE_CONFLICT", "job status changed concurrently"
            )
        self._append_event(
            job_id,
            None,
            "job_status_changed",
            {"from_status": from_status.value, "to_status": to_status.value},
            changed_at,
        )

    def record_unit_input_changed(
        self, unit: JobUnit, changed_at: datetime
    ) -> None:
        self._require_transaction()
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_input_changed",
            {
                "attempt_no": unit.attempt_count,
                "error_code": "UNIT_INPUT_CHANGED",
            },
            changed_at,
        )

    def has_current_unit_input_changed_proof(self, job_id: int) -> bool:
        row = self._conn.execute(
            """
            SELECT
                event.event_kind,
                event.metadata_json,
                unit.status AS unit_status,
                unit.bound_input_hash,
                unit.attempt_count,
                attempt.id AS attempt_id
            FROM job_events AS event
            LEFT JOIN job_units AS unit
                ON unit.job_id=event.job_id
                AND unit.unit_key=event.unit_key
            LEFT JOIN job_unit_attempts AS attempt
                ON attempt.job_id=unit.job_id
                AND attempt.unit_key=unit.unit_key
                AND attempt.attempt_no=unit.attempt_count
            WHERE event.job_id=?
            ORDER BY event.id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["event_kind"] != "unit_input_changed":
            return False
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            row["unit_status"] == UnitStatus.PENDING.value
            and row["bound_input_hash"] is not None
            and row["attempt_id"] is not None
            and metadata
            == {
                "attempt_no": row["attempt_count"],
                "error_code": "UNIT_INPUT_CHANGED",
            }
        )

    def running_unit_count(self, job_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM job_units WHERE job_id=? AND status=?",
            (job_id, UnitStatus.RUNNING.value),
        ).fetchone()[0]

    def start_unit(
        self,
        unit: JobUnit,
        *,
        external_input_hash: str | None,
        bound_input_hash: str,
        started_at: datetime,
    ) -> None:
        self._require_transaction()
        attempt_no = unit.attempt_count + 1
        cursor = self._conn.execute(
            """
            UPDATE job_units
            SET
                external_input_hash = ?,
                bound_input_hash = ?,
                output_hash = NULL,
                status = ?,
                attempt_count = ?,
                error_code = NULL,
                started_at = ?,
                finished_at = NULL
            WHERE job_id = ? AND unit_key = ? AND status = ?
            """,
            (
                external_input_hash,
                bound_input_hash,
                UnitStatus.RUNNING.value,
                attempt_no,
                utc_iso(started_at),
                unit.job_id,
                unit.unit_key,
                UnitStatus.PENDING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_UNIT_STATE_CONFLICT", "job unit status changed concurrently"
            )
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_started",
            {"attempt_no": attempt_no},
            started_at,
        )

    def complete_unit(
        self, unit: JobUnit, output_hash: str, finished_at: datetime
    ) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE job_units
            SET
                output_hash = ?,
                status = ?,
                error_code = NULL,
                finished_at = ?
            WHERE job_id = ? AND unit_key = ? AND status = ?
            """,
            (
                output_hash,
                UnitStatus.SUCCESS.value,
                utc_iso(finished_at),
                unit.job_id,
                unit.unit_key,
                UnitStatus.RUNNING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_UNIT_STATE_CONFLICT", "job unit status changed concurrently"
            )
        self._insert_attempt(
            unit,
            result_status="success",
            output_hash=output_hash,
            error_code=None,
            finished_at=finished_at,
        )
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_succeeded",
            {
                "attempt_no": unit.attempt_count,
                "output_hash": output_hash,
                "result_status": "success",
            },
            finished_at,
        )

    def fail_unit(
        self, unit: JobUnit, error_code: str, finished_at: datetime
    ) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE job_units
            SET
                output_hash = NULL,
                status = ?,
                error_code = ?,
                finished_at = ?
            WHERE job_id = ? AND unit_key = ? AND status = ?
            """,
            (
                UnitStatus.FAILED.value,
                error_code,
                utc_iso(finished_at),
                unit.job_id,
                unit.unit_key,
                UnitStatus.RUNNING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_UNIT_STATE_CONFLICT", "job unit status changed concurrently"
            )
        self._insert_attempt(
            unit,
            result_status="failed",
            output_hash=None,
            error_code=error_code,
            finished_at=finished_at,
        )
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_failed",
            {
                "attempt_no": unit.attempt_count,
                "error_code": error_code,
                "result_status": "failed",
            },
            finished_at,
        )

    def reset_unit(
        self, unit: JobUnit, *, reason: str, reset_at: datetime
    ) -> None:
        self._require_transaction()
        if unit.status is UnitStatus.RUNNING:
            self._insert_attempt(
                unit,
                result_status="interrupted",
                output_hash=None,
                error_code=None,
                finished_at=reset_at,
            )
        cursor = self._conn.execute(
            """
            UPDATE job_units
            SET
                output_hash = NULL,
                status = ?,
                error_code = NULL,
                started_at = NULL,
                finished_at = NULL
            WHERE job_id = ? AND unit_key = ? AND status = ?
            """,
            (
                UnitStatus.PENDING.value,
                unit.job_id,
                unit.unit_key,
                unit.status.value,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_UNIT_STATE_CONFLICT", "job unit status changed concurrently"
            )
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_reset",
            {"attempt_no": unit.attempt_count, "reason": reason},
            reset_at,
        )

    def reuse_unit(
        self,
        unit: JobUnit,
        *,
        source_job_id: int,
        external_input_hash: str | None,
        bound_input_hash: str,
        output_hash: str,
        reused_at: datetime,
    ) -> None:
        self._require_transaction()
        cursor = self._conn.execute(
            """
            UPDATE job_units
            SET
                external_input_hash = ?,
                bound_input_hash = ?,
                output_hash = ?,
                status = ?,
                error_code = NULL,
                started_at = NULL,
                finished_at = ?
            WHERE job_id = ? AND unit_key = ? AND status = ?
            """,
            (
                external_input_hash,
                bound_input_hash,
                output_hash,
                UnitStatus.SUCCESS.value,
                utc_iso(reused_at),
                unit.job_id,
                unit.unit_key,
                UnitStatus.PENDING.value,
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                "JOB_UNIT_STATE_CONFLICT", "job unit status changed concurrently"
            )
        self._append_event(
            unit.job_id,
            unit.unit_key,
            "unit_reused",
            {"output_hash": output_hash, "source_job_id": source_job_id},
            reused_at,
        )

    def _insert_attempt(
        self,
        unit: JobUnit,
        *,
        result_status: str,
        output_hash: str | None,
        error_code: str | None,
        finished_at: datetime,
    ) -> None:
        started_at = self._conn.execute(
            "SELECT started_at FROM job_units WHERE job_id=? AND unit_key=?",
            (unit.job_id, unit.unit_key),
        ).fetchone()["started_at"]
        if started_at is None:
            raise DomainError(
                "INVALID_ATTEMPT_METADATA", "unit attempt has no start timestamp"
            )
        self._conn.execute(
            """
            INSERT INTO job_unit_attempts(
                job_id,
                unit_key,
                attempt_no,
                result_status,
                output_hash,
                error_code,
                started_at,
                finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit.job_id,
                unit.unit_key,
                unit.attempt_count,
                result_status,
                output_hash,
                error_code,
                started_at,
                utc_iso(finished_at),
            ),
        )

    def _append_event(
        self,
        job_id: int,
        unit_key: str | None,
        event_kind: str,
        metadata: dict[str, str | int | None],
        created_at: datetime,
    ) -> None:
        self._require_transaction()
        if not metadata.keys() <= _SAFE_EVENT_METADATA_KEYS:
            raise DomainError(
                "UNSAFE_JOB_EVENT_METADATA", "job event metadata key is not allowed"
            )
        if any(
            not isinstance(value, (str, int, type(None)))
            for value in metadata.values()
        ):
            raise DomainError(
                "UNSAFE_JOB_EVENT_METADATA", "job event metadata value is not safe"
            )
        self._conn.execute(
            """
            INSERT INTO job_events(
                job_id,
                unit_key,
                event_kind,
                metadata_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                unit_key,
                event_kind,
                canonical_json(metadata),
                utc_iso(created_at),
            ),
        )

    def _require_transaction(self) -> None:
        if not self._conn.in_transaction:
            raise DomainError(
                "JOB_TRANSACTION_REQUIRED",
                "job state mutation requires an active caller transaction",
            )


def _unit_from_row(row: sqlite3.Row) -> JobUnit:
    return JobUnit(
        job_id=row["job_id"],
        unit_key=row["unit_key"],
        stage=JobStage(row["stage"]),
        ordinal=row["ordinal"],
        status=UnitStatus(row["status"]),
        declared_input_hash=row["declared_input_hash"],
        dependency_keys=tuple(json.loads(row["dependency_keys_json"])),
        execution_contract_hash=row["execution_contract_hash"],
        external_input_hash=row["external_input_hash"],
        bound_input_hash=row["bound_input_hash"],
        output_hash=row["output_hash"],
        attempt_count=row["attempt_count"],
    )
