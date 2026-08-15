import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.analysis import (
    AnalysisRun,
    AnalysisRunJobAttempt,
    AnalysisRunSettings,
    BeginAnalysisRun,
    FrozenAnalysisInput,
    SelectedInputSegment,
)
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    cutoff_exclusive_utc,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    ASSET_MAPPING_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
    JobManifest,
    JobUnit,
    ManifestUnit,
    effective_input_hash,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository, StoredJob
from market_voice_forecast_ledger.services.job_state import JobStateService


_CODEX_BATCH_UNIT_KEY = "codex:batch:1"
_EXPECTED_UNIT_GRAPH = (
    (
        ANALYSIS_INPUT_UNIT_KEY,
        JobStage.ANALYSIS_INPUT_EXTRACTION,
        (),
    ),
    (
        _CODEX_BATCH_UNIT_KEY,
        JobStage.CODEX_ANALYSIS,
        (ANALYSIS_INPUT_UNIT_KEY,),
    ),
    (
        STATEMENT_NORMALIZATION_UNIT_KEY,
        JobStage.ASSET_MAPPING,
        (_CODEX_BATCH_UNIT_KEY,),
    ),
    (
        PERIOD_NORMALIZATION_UNIT_KEY,
        JobStage.ASSET_MAPPING,
        (STATEMENT_NORMALIZATION_UNIT_KEY,),
    ),
    (
        ASSET_MAPPING_UNIT_KEY,
        JobStage.ASSET_MAPPING,
        (STATEMENT_NORMALIZATION_UNIT_KEY, PERIOD_NORMALIZATION_UNIT_KEY),
    ),
    (
        FORECAST_PROJECTION_UNIT_KEY,
        JobStage.ASSET_MAPPING,
        (ASSET_MAPPING_UNIT_KEY, PERIOD_NORMALIZATION_UNIT_KEY),
    ),
    (
        FINAL_PROMOTION_UNIT_KEY,
        JobStage.HEATMAP_UPDATE,
        (FORECAST_PROJECTION_UNIT_KEY,),
    ),
)


class AnalysisRunService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._jobs = JobRepository(conn)
        self._job_state = JobStateService(conn, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def preview_input_contract(
        self,
        subject_id: int,
        cutoff_day: date,
        settings: AnalysisRunSettings,
    ) -> str:
        self._require_settings(settings)
        frozen_input = self._build_frozen_input(subject_id, cutoff_day, settings)
        return frozen_input.input_contract_hash

    def begin(self, command: BeginAnalysisRun) -> AnalysisRun:
        self._require_settings(command.settings)
        with transaction(self._conn):
            if self._analysis.is_job_attached(command.job_id):
                raise DomainError(
                    "ANALYSIS_JOB_ALREADY_ATTACHED",
                    "an immutable job can belong to only one analysis run",
                )
            frozen_input = self._build_frozen_input(
                command.subject_id, command.cutoff_day, command.settings
            )
            job, units = self._validated_manifest(
                command.job_id,
                frozen_input.input_contract_hash,
                command.settings,
                successor=False,
            )
            if job.source_job_id is not None:
                raise DomainError(
                    "ANALYSIS_INITIAL_JOB_REQUIRED",
                    "a new run requires an initial analysis job",
                )
            input_unit = units[0]
            if input_unit.status is not UnitStatus.RUNNING or any(
                unit.status is not UnitStatus.PENDING for unit in units[1:]
            ):
                raise DomainError(
                    "ANALYSIS_JOB_NOT_READY",
                    "only the input-freeze unit may be running when a run begins",
                )
            if job.status is not JobStatus.RUNNING:
                raise DomainError(
                    "ANALYSIS_JOB_NOT_READY",
                    "analysis input freeze requires a running job",
                )

            started_at = self._clock()
            scope_id = self._analysis.get_or_create_scope(
                command.subject_id,
                command.cutoff_day,
                cutoff_exclusive_utc(command.cutoff_day),
            )
            run_id = self._analysis.insert_run(
                scope_id, command.settings, frozen_input, started_at
            )
            self._analysis.insert_job_attempt(
                run_id,
                command.job_id,
                1,
                None,
                started_at,
            )
            self._analysis.insert_run_segments(run_id, frozen_input.segments)
            self._analysis.insert_snapshot(
                run_id,
                frozen_input,
                started_at,
                started_at + timedelta(days=365),
            )
            self._analysis.append_run_event(
                run_id,
                AnalysisRunStatus.STARTED,
                None,
                created_at=started_at,
            )
            self._job_state.complete_unit_in_transaction(
                command.job_id,
                ANALYSIS_INPUT_UNIT_KEY,
                frozen_input.input_sha256,
            )
        return self._analysis.get_run(run_id)

    def attach_successor(
        self, run_id: int, successor_job_id: int
    ) -> AnalysisRunJobAttempt:
        with transaction(self._conn):
            run = self._analysis.get_run(run_id)
            active_job_id = run.active_job_id
            active_job = self._jobs.get(active_job_id)
            if self._analysis.is_job_attached(successor_job_id):
                raise DomainError(
                    "ANALYSIS_JOB_ALREADY_ATTACHED",
                    "successor job is already attached to an analysis run",
                )
            successor = self._jobs.get(successor_job_id)
            if successor.source_job_id != active_job_id:
                raise DomainError(
                    "SUCCESSOR_NOT_ACTIVE_DESCENDANT",
                    "successor must descend from the active job attempt",
                )
            if not self._source_is_safe_for_successor(active_job):
                raise DomainError(
                    "SUCCESSOR_SOURCE_NOT_SAFE",
                    "active job is not safely stopped or input-changed failed",
                )
            if successor.kind is not JobKind.ANALYSIS_SCOPE:
                raise DomainError(
                    "SUCCESSOR_REQUIRES_NEW_RUN",
                    "only an analysis job can continue an analysis run",
                )
            try:
                _, successor_units = self._validated_manifest(
                    successor_job_id,
                    run.input_contract_hash,
                    run.settings,
                    successor=True,
                )
            except DomainError as error:
                raise DomainError(
                    "SUCCESSOR_REQUIRES_NEW_RUN",
                    "successor contract differs from the immutable run",
                ) from error

            source_units = self._jobs.list_units(active_job_id)
            source_by_key = {unit.unit_key: unit for unit in source_units}
            successor_by_key = {
                unit.unit_key: unit for unit in successor_units
            }
            durable_keys = self._durable_unit_keys(run_id)
            successful_keys = {
                unit.unit_key
                for unit in source_units
                if unit.status is UnitStatus.SUCCESS
            }
            if not durable_keys <= successful_keys:
                raise DomainError(
                    "SUCCESSOR_REQUIRES_NEW_RUN",
                    "run-owned rows do not match successful source units",
                )
            for unit_key in successful_keys:
                source_unit = source_by_key[unit_key]
                successor_unit = successor_by_key[unit_key]
                if not self._is_exact_reuse(
                    active_job_id, source_unit, successor_unit
                ):
                    raise DomainError(
                        "SUCCESSOR_REQUIRES_NEW_RUN",
                        "every durable source success must be reused unchanged",
                    )

            snapshot = self._analysis.get_snapshot(run_id)
            successor_input = successor_by_key[ANALYSIS_INPUT_UNIT_KEY]
            if successor_input.output_hash != snapshot.input_sha256:
                raise DomainError(
                    "SUCCESSOR_REQUIRES_NEW_RUN",
                    "successor must reuse the immutable input snapshot",
                )
            attached_at = self._clock()
            return self._analysis.insert_job_attempt(
                run_id,
                successor_job_id,
                self._analysis.next_attempt_ordinal(run_id),
                active_job_id,
                attached_at,
            )

    @staticmethod
    def _require_settings(settings: AnalysisRunSettings) -> None:
        if settings != AnalysisRunSettings.required():
            raise DomainError(
                "ANALYSIS_SETTINGS_MISMATCH",
                "analysis settings must equal the required M2 contract",
            )

    def _build_frozen_input(
        self,
        subject_id: int,
        cutoff_day: date,
        settings: AnalysisRunSettings,
    ) -> FrozenAnalysisInput:
        subject_kind = self._analysis.get_active_subject_kind(subject_id)
        exclusive = cutoff_exclusive_utc(cutoff_day)
        segments = self._analysis.select_input_segments(
            subject_id, exclusive, subject_kind
        )
        input_text = canonical_json(
            {
                "cutoff_day_jst": cutoff_day.isoformat(),
                "information_boundary_version": (
                    settings.information_boundary_version
                ),
                "segments": [
                    {
                        "channel_display_name": segment.channel_display_name,
                        "end_ms": segment.end_ms,
                        "published_at": utc_iso(segment.published_at),
                        "segment_id": segment.segment_id,
                        "start_ms": segment.start_ms,
                        "text": segment.text_body,
                        "title": segment.video_title,
                        "youtube_channel_id": segment.youtube_channel_id,
                        "youtube_video_id": segment.youtube_video_id,
                    }
                    for segment in segments
                ],
                "subject_id": subject_id,
            }
        )
        input_sha256 = sha256_text(input_text)
        metadata = {
            "cutoff_day_jst": cutoff_day.isoformat(),
            "cutoff_exclusive_utc": utc_iso(exclusive),
            "input_sha256": input_sha256,
            "segments": [self._segment_metadata(segment) for segment in segments],
            "settings": settings.contract_metadata(),
            "subject_id": subject_id,
            "subject_kind": subject_kind.value,
        }
        metadata_json = canonical_json(metadata)
        return FrozenAnalysisInput(
            input_text=input_text,
            metadata_json=metadata_json,
            input_sha256=input_sha256,
            input_contract_hash=sha256_text(metadata_json),
            segments=segments,
        )

    @staticmethod
    def _segment_metadata(segment: SelectedInputSegment) -> dict[str, object]:
        return {
            "assignment_evidence_hash": segment.assignment_evidence_hash,
            "assignment_kind": segment.assignment_kind.value,
            "assignment_origin": segment.assignment_origin,
            "assignment_updated_at": utc_iso(segment.assignment_updated_at),
            "assigned_subject_id": segment.assigned_subject_id,
            "channel_display_name": segment.channel_display_name,
            "end_ms": segment.end_ms,
            "policy_hash": segment.policy_hash,
            "policy_id": segment.policy_id,
            "published_at": utc_iso(segment.published_at),
            "segment_id": segment.segment_id,
            "segment_no": segment.segment_no,
            "start_ms": segment.start_ms,
            "text_sha256": segment.text_sha256,
            "title": segment.video_title,
            "video_id": segment.video_id,
            "youtube_channel_id": segment.youtube_channel_id,
            "youtube_video_id": segment.youtube_video_id,
        }

    def _validated_manifest(
        self,
        job_id: int,
        input_contract_hash: str,
        settings: AnalysisRunSettings,
        *,
        successor: bool,
    ) -> tuple[StoredJob, tuple[JobUnit, ...]]:
        job = self._jobs.get(job_id)
        if job.kind is not JobKind.ANALYSIS_SCOPE:
            raise DomainError(
                "ANALYSIS_JOB_MANIFEST_MISMATCH",
                "analysis run requires an analysis-scope job",
            )
        units = self._jobs.list_units(job_id)
        try:
            rebuilt = JobManifest.build(
                job.kind,
                tuple(
                    ManifestUnit(
                        unit.unit_key,
                        unit.stage,
                        unit.ordinal,
                        unit.declared_input_hash,
                        unit.dependency_keys,
                        unit.execution_contract_hash,
                    )
                    for unit in units
                ),
            )
        except DomainError as error:
            raise DomainError(
                "ANALYSIS_JOB_MANIFEST_MISMATCH",
                "stored analysis job manifest is invalid",
            ) from error
        if rebuilt.manifest_hash != job.manifest_hash or job.total_units != 7:
            raise DomainError(
                "ANALYSIS_JOB_MANIFEST_MISMATCH",
                "stored job differs from its immutable manifest",
            )
        actual_graph = tuple(
            (unit.unit_key, unit.stage, unit.dependency_keys) for unit in units
        )
        if actual_graph != _EXPECTED_UNIT_GRAPH:
            raise DomainError(
                "ANALYSIS_JOB_MANIFEST_MISMATCH",
                "analysis job must use the exact seven-unit graph",
            )
        if any(unit.declared_input_hash is not None for unit in units[1:]):
            raise DomainError(
                "ANALYSIS_JOB_MANIFEST_MISMATCH",
                "only input freeze may declare a root input hash",
            )
        input_unit = units[0]
        if input_unit.declared_input_hash != input_contract_hash:
            raise DomainError(
                "ANALYSIS_JOB_INPUT_MISMATCH",
                "analysis job was prepared for a different immutable input",
            )
        if (
            input_unit.external_input_hash is not None
            or input_unit.bound_input_hash
            != effective_input_hash(input_contract_hash, (), None)
        ):
            raise DomainError(
                "ANALYSIS_JOB_INPUT_MISMATCH",
                "input-freeze binding does not match the preview contract",
            )
        codex_unit = units[1]
        if (
            codex_unit.execution_contract_hash
            != settings.codex_execution_contract_hash()
        ):
            raise DomainError(
                "ANALYSIS_CODEX_CONTRACT_MISMATCH",
                "Codex unit does not match the required execution contract",
            )
        if successor:
            if _successor_job_status_invalid(job.status):
                raise DomainError(
                    "ANALYSIS_JOB_NOT_READY",
                    "successor job is not in an attachable state",
                )
        return job, units

    def _source_is_safe_for_successor(self, job: StoredJob) -> bool:
        units = self._jobs.list_units(job.id)
        if job.status is JobStatus.STOPPED:
            return not any(
                unit.status in {UnitStatus.RUNNING, UnitStatus.FAILED}
                for unit in units
            )
        if job.status is not JobStatus.FAILED:
            return False
        return (
            not any(
                unit.status in {UnitStatus.RUNNING, UnitStatus.FAILED}
                for unit in units
            )
            and any(
                unit.status is UnitStatus.PENDING
                and unit.bound_input_hash is not None
                for unit in units
            )
        )

    def _is_exact_reuse(
        self,
        source_job_id: int,
        source: JobUnit,
        successor: JobUnit,
    ) -> bool:
        if (
            successor.status is not UnitStatus.SUCCESS
            or source.output_hash is None
            or successor.output_hash != source.output_hash
            or successor.bound_input_hash != source.bound_input_hash
            or successor.external_input_hash != source.external_input_hash
            or successor.execution_contract_hash != source.execution_contract_hash
            or successor.declared_input_hash != source.declared_input_hash
            or successor.dependency_keys != source.dependency_keys
        ):
            return False
        rows = self._conn.execute(
            """
            SELECT metadata_json
            FROM job_events
            WHERE job_id=? AND unit_key=? AND event_kind='unit_reused'
            """,
            (successor.job_id, successor.unit_key),
        ).fetchall()
        return any(
            json.loads(row["metadata_json"]).get("source_job_id") == source_job_id
            for row in rows
        )

    def _durable_unit_keys(self, run_id: int) -> set[str]:
        durable: set[str] = set()
        if self._conn.execute(
            "SELECT 1 FROM analysis_input_snapshots WHERE run_id=?", (run_id,)
        ).fetchone():
            durable.add(ANALYSIS_INPUT_UNIT_KEY)
        if self._table_exists("analysis_run_outputs"):
            durable.update(
                row["unit_key"]
                for row in self._conn.execute(
                    "SELECT unit_key FROM analysis_run_outputs WHERE run_id=?",
                    (run_id,),
                )
            )
        if self._table_has_run_rows("analysis_statements", run_id):
            durable.add(STATEMENT_NORMALIZATION_UNIT_KEY)
        if self._table_exists("analysis_statement_periods") and self._table_exists(
            "analysis_statements"
        ):
            if self._conn.execute(
                """
                SELECT 1
                FROM analysis_statement_periods AS period
                JOIN analysis_statements AS statement
                    ON statement.id=period.statement_id
                WHERE statement.run_id=?
                LIMIT 1
                """,
                (run_id,),
            ).fetchone():
                durable.add(PERIOD_NORMALIZATION_UNIT_KEY)
        if self._table_has_run_rows("analysis_asset_mappings", run_id):
            durable.add(ASSET_MAPPING_UNIT_KEY)
        if self._table_has_run_rows("forecast_projection_batches", run_id):
            durable.add(FORECAST_PROJECTION_UNIT_KEY)
        return durable

    def _table_has_run_rows(self, table: str, run_id: int) -> bool:
        return self._table_exists(table) and self._conn.execute(
            f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone() is not None

    def _table_exists(self, table: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None


def _successor_job_status_invalid(status: JobStatus) -> bool:
    return status not in {
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
    }
