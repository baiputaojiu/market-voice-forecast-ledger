import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError

from market_voice_forecast_ledger.domain.analysis import AnalysisRun
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    Asset,
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import (
    ForecastProjectionBatch,
    ProjectionTrigger,
    select_current,
)
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    ASSET_MAPPING_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
    JobManifest,
    ManifestUnit,
    effective_input_hash,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.forecasts import ForecastRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.repositories.mappings import MappingRepository
from market_voice_forecast_ledger.repositories.periods import PeriodRepository
from market_voice_forecast_ledger.repositories.statements import StatementRepository
from market_voice_forecast_ledger.services.asset_mapping import AssetMappingService
from market_voice_forecast_ledger.services.codex_contract import AnalysisEnvelope
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewService,
)


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
_RUNNABLE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.RETRYING,
}


@dataclass(frozen=True, slots=True)
class CurrentMappingReference:
    mapping_id: int
    effective_asset: Asset
    effective_eligibility: bool


@dataclass(frozen=True, slots=True)
class CurrentResultSummary:
    scope_id: int
    source_run_id: int | None
    projection_batch_id: int | None
    statement_count: int
    mapping_count: int
    eligible_mapping_count: int
    forecast_count: int
    statement_ids: tuple[int, ...]
    mapping_ids: tuple[int, ...]
    eligible_mapping_ids: tuple[int, ...]
    forecast_ids: tuple[int, ...]
    effective_mappings: tuple[CurrentMappingReference, ...]


@dataclass(frozen=True, slots=True)
class CurrentResultDelta:
    before: CurrentResultSummary
    after: CurrentResultSummary


class CurrentResultService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._jobs = JobRepository(conn)
        self._job_state = JobStateService(conn)
        self._statements = StatementRepository(conn)
        self._periods = PeriodRepository(conn)
        self._mappings = MappingRepository(conn)
        self._mapping_reviews = MappingReviewService(conn)
        self._forecasts = ForecastRepository(conn)

    def get_scope(self, scope_id: int) -> CurrentResultSummary:
        self._analysis.get_scope(scope_id)
        return self._summarize_scope(scope_id)

    def _replace_scope_rows_in_transaction(
        self, run_id: int, projection_batch_id: int
    ) -> CurrentResultDelta:
        if not self._conn.in_transaction:
            raise DomainError(
                "CURRENT_REPLACEMENT_TRANSACTION_REQUIRED",
                "current rows may change only inside a caller-owned transaction",
            )
        run = self._validate_complete_projection(run_id, projection_batch_id)
        before = self._summarize_scope(
            run.scope_id, require_current_semantics=False
        )
        self._delete_scope_rows(run.scope_id)
        self._insert_result_set(run.scope_id, run_id, projection_batch_id)
        self._copy_statements(run_id, run.scope_id)
        self._copy_mappings(run_id, run.scope_id)
        self._copy_forecasts(projection_batch_id, run_id, run.scope_id)
        after = self._summarize_scope(run.scope_id)
        return CurrentResultDelta(before, after)

    def _validate_complete_projection(
        self, run_id: int, projection_batch_id: int
    ) -> AnalysisRun:
        run = self._analysis.get_run(run_id)
        active_attempt = self._validate_attempt_chain(run)
        units = self._validate_active_manifest(run)
        transport_created_at = self._validate_run_event(run_id)
        self._validate_input_artifact(run, units[0])
        self._validate_codex_artifacts(
            run, units[1], transport_created_at
        )
        self._validate_statement_artifact(run, units[2])
        self._validate_period_artifact(run, units[3])
        self._validate_mapping_artifact(run, units[4])
        batch = self._validate_projection_artifact(
            run, projection_batch_id, units[5]
        )
        self._validate_final_unit(units[6])
        if active_attempt.attempt_ordinal > 1:
            self._validate_successor_reuse(
                active_attempt.source_job_id,
                run.active_job_id,
                units[:-1],
            )
        self._validate_batch_contents(run, batch)
        return run

    def _validate_attempt_chain(self, run: AnalysisRun):
        attempts = self._analysis.list_job_attempts(run.id)
        if (
            not attempts
            or tuple(item.attempt_ordinal for item in attempts)
            != tuple(range(1, len(attempts) + 1))
            or attempts[-1].job_id != run.active_job_id
        ):
            self._validation_failed("analysis run has an invalid active attempt")
        previous_job_id = None
        for attempt in attempts:
            job = self._jobs.get(attempt.job_id)
            if (
                job.kind is not JobKind.ANALYSIS_SCOPE
                or attempt.source_job_id != previous_job_id
                or job.source_job_id != previous_job_id
            ):
                self._validation_failed(
                    "analysis attempt ancestry does not match the active job"
                )
            previous_job_id = attempt.job_id
        if self._jobs.get(run.active_job_id).status not in _RUNNABLE_JOB_STATUSES:
            self._validation_failed("active analysis job is not promotable")
        return attempts[-1]

    def _validate_active_manifest(self, run: AnalysisRun):
        job = self._jobs.get(run.active_job_id)
        units = self._jobs.list_units(run.active_job_id)
        if len(units) != len(_EXPECTED_UNIT_GRAPH):
            self._validation_failed("analysis manifest does not have seven units")
        actual_graph = tuple(
            (unit.unit_key, unit.stage, unit.dependency_keys) for unit in units
        )
        if actual_graph != _EXPECTED_UNIT_GRAPH or tuple(
            unit.ordinal for unit in units
        ) != tuple(range(1, len(units) + 1)):
            self._validation_failed("analysis manifest graph is not exact")
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
        if rebuilt.manifest_hash != job.manifest_hash:
            self._validation_failed("stored analysis manifest hash is invalid")
        if (
            units[0].declared_input_hash != run.input_contract_hash
            or any(unit.declared_input_hash is not None for unit in units[1:])
            or units[1].execution_contract_hash
            != run.settings.codex_execution_contract_hash()
        ):
            self._validation_failed("analysis manifest input contract is invalid")
        self._job_state.require_upstream_success(
            run.active_job_id, FINAL_PROMOTION_UNIT_KEY
        )
        return units

    def _validate_run_event(self, run_id: int) -> str:
        rows = self._conn.execute(
            """
            SELECT status, safe_error_code, created_at
            FROM analysis_run_events
            WHERE run_id=?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        if (
            len(rows) < 2
            or rows[0]["status"] != AnalysisRunStatus.STARTED.value
            or rows[0]["safe_error_code"] is not None
            or rows[-1]["status"]
            != AnalysisRunStatus.TRANSPORT_VALIDATED.value
            or rows[-1]["safe_error_code"] is not None
            or any(
                row["status"] != AnalysisRunStatus.FAILED.value
                or not isinstance(row["safe_error_code"], str)
                or not row["safe_error_code"]
                for row in rows[1:-1]
            )
        ):
            self._validation_failed(
                "analysis run event history is not promotable"
            )
        return rows[-1]["created_at"]

    def _validate_input_artifact(self, run: AnalysisRun, unit) -> None:
        snapshot = self._analysis.get_snapshot(run.id)
        if (
            unit.status is not UnitStatus.SUCCESS
            or unit.output_hash is None
            or unit.output_hash != snapshot.input_sha256
            or snapshot.input_sha256 != run.input_hash
            or sha256_text(snapshot.metadata_json) != run.input_contract_hash
            or (
                snapshot.input_text is not None
                and sha256_text(snapshot.input_text) != snapshot.input_sha256
            )
        ):
            self._validation_failed("immutable analysis input artifact is invalid")

    def _validate_codex_artifacts(
        self, run: AnalysisRun, unit, transport_created_at: str
    ) -> None:
        rows = self._conn.execute(
            """
            SELECT *
            FROM analysis_run_outputs
            WHERE run_id=?
            ORDER BY batch_ordinal, id
            """,
            (run.id,),
        ).fetchall()
        if len(rows) != 1:
            self._validation_failed(
                "each manifest Codex batch requires exactly one output"
            )
        row = rows[0]
        attached_job_ids = {
            attempt.job_id for attempt in self._analysis.list_job_attempts(run.id)
        }
        try:
            envelope = AnalysisEnvelope.model_validate_json(
                row["canonical_output_json"]
            )
        except ValidationError as cause:
            raise DomainError(
                "CURRENT_REPLACEMENT_VALIDATION_FAILED",
                "stored Codex output does not satisfy its schema",
            ) from cause
        canonical = canonical_json(envelope.model_dump(mode="json"))
        origin_unit = self._jobs.get_unit(row["job_id"], row["unit_key"])
        if (
            unit.status is not UnitStatus.SUCCESS
            or unit.output_hash is None
            or row["job_id"] not in attached_job_ids
            or row["unit_key"] != _CODEX_BATCH_UNIT_KEY
            or row["batch_ordinal"] != unit.ordinal
            or row["canonical_output_json"] != canonical
            or row["output_sha256"] != sha256_text(canonical)
            or row["output_sha256"] != unit.output_hash
            or envelope.run_id != run.id
            or envelope.batch_key != unit.unit_key
            or row["receipt_model"] != run.model
            or row["receipt_reasoning_effort"] != run.reasoning_effort
            or row["receipt_tool_call_count"] != 0
            or row["receipt_boundary_mode"] != "stored_statements_only"
            or row["created_at"] != transport_created_at
            or origin_unit.status is not UnitStatus.SUCCESS
            or origin_unit.output_hash != row["output_sha256"]
        ):
            self._validation_failed("stored Codex output is not sealed to its unit")
        if not self._success_attempt_history_is_exact(
            origin_unit, row["output_sha256"]
        ):
            self._validation_failed(
                "stored Codex output has invalid success-attempt provenance"
            )

    def _validate_statement_artifact(self, run: AnalysisRun, unit) -> None:
        statements = self._statements.list_run_statements(run.id)
        payload = [
            {
                "ordinal": statement.ordinal,
                "batch_ordinal": statement.batch_ordinal,
                "proposal_ordinal": statement.proposal_ordinal,
                "source_video_id": statement.source_video_id,
                "statement_type": statement.statement_type.value,
                "forecast_basis": (
                    None
                    if statement.forecast_basis is None
                    else statement.forecast_basis.value
                ),
                "condition_kind": statement.condition_kind.value,
                "condition_text": statement.condition_text,
                "direction_kind": (
                    None
                    if statement.direction_kind is None
                    else statement.direction_kind.value
                ),
                "turning_point_kind": (
                    None
                    if statement.turning_point_kind is None
                    else statement.turning_point_kind.value
                ),
                "target_expression": statement.target_expression,
                "period_expression": statement.period_expression,
                "heatmap_candidate": statement.heatmap_candidate,
                "evidence": [
                    {
                        "ordinal": evidence.ordinal,
                        "run_segment_id": evidence.run_segment_id,
                        "segment_id": evidence.segment_id,
                        "excerpt": evidence.excerpt,
                        "start_ms": evidence.start_ms,
                        "end_ms": evidence.end_ms,
                    }
                    for evidence in statement.evidence_links
                ],
            }
            for statement in statements
        ]
        self._require_unit_artifact_hash(unit, payload, "statement/evidence")

    def _validate_period_artifact(self, run: AnalysisRun, unit) -> None:
        periods = self._periods.list_run_periods(run.id)
        statement_ids = tuple(
            statement.id for statement in self._statements.list_run_statements(run.id)
        )
        if tuple(period.statement_id for period in periods) != statement_ids:
            self._validation_failed(
                "period artifact does not cover every immutable statement"
            )
        payload = [
            {
                "statement_id": period.statement_id,
                "source_expression": period.source_expression,
                "start_date": (
                    None if period.start_date is None else period.start_date.isoformat()
                ),
                "end_date": (
                    None if period.end_date is None else period.end_date.isoformat()
                ),
                "time_basis": (
                    None if period.time_basis is None else period.time_basis.value
                ),
                "basis_published_at": (
                    None
                    if period.basis_published_at is None
                    else utc_iso(period.basis_published_at)
                ),
                "is_unknown": period.is_unknown,
            }
            for period in periods
        ]
        self._require_unit_artifact_hash(unit, payload, "period")

    def _validate_mapping_artifact(self, run: AnalysisRun, unit) -> None:
        mappings = self._mappings.list_run_mappings(run.id)
        payload = AssetMappingService._artifact_payload(mappings)
        self._require_unit_artifact_hash(unit, payload, "asset mapping")

    def _validate_projection_artifact(
        self, run: AnalysisRun, projection_batch_id: int, unit
    ) -> ForecastProjectionBatch:
        initial = self._forecasts.initial_batch(run.id)
        initial_hash = self._forecasts.batch_artifact_hash(initial.id)
        if (
            unit.status is not UnitStatus.SUCCESS
            or unit.output_hash is None
            or unit.output_hash != initial_hash
        ):
            self._validation_failed(
                "initial projection artifact does not match its successful unit"
            )
        batch = self._forecasts.get_batch(projection_batch_id)
        if batch.run_id != run.id:
            self._validation_failed("projection batch belongs to another run")
        latest_batch_id = self._conn.execute(
            "SELECT MAX(id) FROM forecast_projection_batches WHERE run_id=?",
            (run.id,),
        ).fetchone()[0]
        if latest_batch_id != batch.id:
            self._validation_failed("projection batch is not the newest run batch")
        review_state = ForecastProjectionService(self._conn)._review_state(run.id)
        if (
            batch.latest_mapping_review_id
            != review_state.latest_mapping_review_id
            or batch.latest_period_review_id
            != review_state.latest_period_review_id
        ):
            self._validation_failed(
                "projection batch does not name the effective review heads"
            )
        if batch.trigger_kind is ProjectionTrigger.INITIAL:
            if batch.id != initial.id:
                self._validation_failed("named initial projection is not unique")
        elif batch.trigger_kind not in {
            ProjectionTrigger.MAPPING_REVIEW,
            ProjectionTrigger.PERIOD_REVIEW,
        }:
            self._validation_failed("projection trigger is invalid")
        if not self._review_batch_lineage_is_valid(batch):
            self._validation_failed(
                "projection trigger does not match its review-head advance"
            )
        return batch

    def _review_batch_lineage_is_valid(
        self, batch: ForecastProjectionBatch
    ) -> bool:
        previous = self._conn.execute(
            """
            SELECT latest_mapping_review_id, latest_period_review_id
            FROM forecast_projection_batches
            WHERE run_id=? AND id<?
            ORDER BY id DESC
            LIMIT 1
            """,
            (batch.run_id, batch.id),
        ).fetchone()
        if batch.trigger_kind is ProjectionTrigger.INITIAL:
            return previous is None
        if previous is None:
            return False

        mapping_advanced = self._review_head_advanced(
            previous["latest_mapping_review_id"],
            batch.latest_mapping_review_id,
        )
        period_advanced = self._review_head_advanced(
            previous["latest_period_review_id"],
            batch.latest_period_review_id,
        )
        mapping_unchanged = (
            previous["latest_mapping_review_id"]
            == batch.latest_mapping_review_id
        )
        period_unchanged = (
            previous["latest_period_review_id"]
            == batch.latest_period_review_id
        )
        return (
            batch.trigger_kind is ProjectionTrigger.MAPPING_REVIEW
            and mapping_advanced
            and period_unchanged
        ) or (
            batch.trigger_kind is ProjectionTrigger.PERIOD_REVIEW
            and period_advanced
            and mapping_unchanged
        )

    @staticmethod
    def _review_head_advanced(before: int | None, after: int | None) -> bool:
        return after is not None and (before is None or after > before)

    def _validate_final_unit(self, unit) -> None:
        if (
            unit.status not in {UnitStatus.PENDING, UnitStatus.RUNNING}
            or unit.output_hash is not None
        ):
            self._validation_failed(
                "final promotion unit must be pending or running"
            )
        dependency = self._jobs.get_unit(
            unit.job_id, FORECAST_PROJECTION_UNIT_KEY
        )
        expected = effective_input_hash(
            unit.declared_input_hash,
            (dependency.output_hash,),
            None,
        )
        if (
            unit.external_input_hash is not None
            or (
                unit.bound_input_hash is not None
                and unit.bound_input_hash != expected
            )
            or (
                unit.status is UnitStatus.RUNNING
                and unit.bound_input_hash is None
            )
        ):
            self._validation_failed("final promotion unit input is invalid")

    def _validate_successor_reuse(
        self, source_job_id: int | None, active_job_id: int, units
    ) -> None:
        if source_job_id is None:
            self._validation_failed("successor attempt has no source job")
        for unit in units:
            source = self._jobs.get_unit(source_job_id, unit.unit_key)
            rows = self._conn.execute(
                """
                SELECT metadata_json
                FROM job_events
                WHERE job_id=? AND unit_key=? AND event_kind='unit_reused'
                ORDER BY id
                """,
                (active_job_id, unit.unit_key),
            ).fetchall()
            attempt_row_count = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM job_unit_attempts
                WHERE job_id=? AND unit_key=?
                """,
                (active_job_id, unit.unit_key),
            ).fetchone()[0]
            if source.status is UnitStatus.SUCCESS:
                if (
                    len(rows) != 1
                    or not self._reuse_event_matches(
                        rows[0]["metadata_json"],
                        source_job_id,
                        unit.output_hash,
                    )
                    or unit.attempt_count != 0
                    or attempt_row_count != 0
                    or source.stage is not unit.stage
                    or source.ordinal != unit.ordinal
                    or source.declared_input_hash != unit.declared_input_hash
                    or source.dependency_keys != unit.dependency_keys
                    or source.execution_contract_hash
                    != unit.execution_contract_hash
                    or source.external_input_hash != unit.external_input_hash
                    or source.bound_input_hash != unit.bound_input_hash
                    or source.output_hash != unit.output_hash
                ):
                    self._validation_failed(
                        "active successor reuse provenance is invalid"
                    )
                continue
            if rows or not self._success_attempt_history_is_exact(
                unit, unit.output_hash, allow_prior_success=True
            ):
                self._validation_failed(
                    "active successor unit has no valid success provenance"
                )

    def _validate_batch_contents(
        self, run: AnalysisRun, batch: ForecastProjectionBatch
    ) -> None:
        scope = self._analysis.get_scope(run.scope_id)
        groups = ForecastProjectionService(self._conn)._eligible_groups(
            run.id, scope.subject_id
        )
        if len(groups) != len(batch.forecasts):
            self._validation_failed("projection batch is structurally incomplete")
        for group, forecast in zip(groups, batch.forecasts, strict=True):
            resolved = select_current(group.candidates)
            expected = (
                run.id,
                scope.subject_id,
                group.asset,
                resolved.mapping_kind,
                group.period_start,
                group.period_end,
                group.unknown_period,
                group.condition_kind,
                group.condition_text,
                resolved.view_relation,
                resolved.primary_direction,
                resolved.directions,
                resolved.confidence,
                resolved.evidence_count,
                resolved.selected_published_at,
                resolved.selected_forecast_basis,
                resolved.period_specificity,
                resolved.stable_selection_key,
                True,
                None,
                resolved.supporting_statement_ids,
                resolved.counterevidence_statement_ids,
            )
            actual = (
                forecast.run_id,
                forecast.subject_id,
                forecast.asset,
                forecast.mapping_kind,
                forecast.period_start,
                forecast.period_end,
                forecast.unknown_period,
                forecast.condition_kind,
                forecast.condition_text,
                forecast.view_relation,
                forecast.primary_direction,
                forecast.directions,
                forecast.confidence,
                forecast.evidence_count,
                forecast.selected_published_at,
                forecast.selected_forecast_basis,
                forecast.period_specificity,
                forecast.stable_selection_key,
                forecast.heatmap_eligible,
                forecast.exclusion_reason,
                forecast.supporting_statement_ids,
                forecast.counterevidence_statement_ids,
            )
            if actual != expected:
                self._validation_failed(
                    "projection batch content does not match effective inputs"
                )

    @staticmethod
    def _reuse_event_matches(
        value: str, source_job_id: int, output_hash: str | None
    ) -> bool:
        return value == canonical_json(
            {"output_hash": output_hash, "source_job_id": source_job_id}
        )

    def _success_attempt_history_is_exact(
        self,
        unit,
        expected_output_hash: str,
        *,
        allow_prior_success: bool = False,
    ) -> bool:
        rows = self._conn.execute(
            """
            SELECT attempt_no, result_status, output_hash
            FROM job_unit_attempts
            WHERE job_id=? AND unit_key=?
            ORDER BY attempt_no
            """,
            (unit.job_id, unit.unit_key),
        ).fetchall()
        return (
            unit.status is UnitStatus.SUCCESS
            and unit.output_hash == expected_output_hash
            and unit.attempt_count > 0
            and len(rows) == unit.attempt_count
            and tuple(row["attempt_no"] for row in rows)
            == tuple(range(1, unit.attempt_count + 1))
            and all(
                (
                    row["result_status"] in {"failed", "interrupted"}
                    and row["output_hash"] is None
                )
                or (
                    allow_prior_success
                    and row["result_status"] == "success"
                    and row["output_hash"] is not None
                )
                for row in rows[:-1]
            )
            and rows[-1]["result_status"] == "success"
            and rows[-1]["output_hash"] == expected_output_hash
        )

    def _require_unit_artifact_hash(self, unit, payload, label: str) -> None:
        artifact_hash = sha256_text(canonical_json(payload))
        if (
            unit.status is not UnitStatus.SUCCESS
            or unit.output_hash is None
            or unit.output_hash != artifact_hash
        ):
            self._validation_failed(
                f"stored {label} artifact does not match its successful unit"
            )

    def _summarize_scope(
        self, scope_id: int, *, require_current_semantics: bool = True
    ) -> CurrentResultSummary:
        self._analysis.get_scope(scope_id)
        headers = self._conn.execute(
            "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)
        ).fetchall()
        child_count = sum(
            self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE scope_id=?", (scope_id,)
            ).fetchone()[0]
            for table in (
                "current_statements",
                "current_asset_mappings",
                "current_forecasts",
            )
        )
        if not headers:
            if child_count:
                self._state_invalid("current scope has orphan child rows")
            return CurrentResultSummary(
                scope_id,
                None,
                None,
                0,
                0,
                0,
                0,
                (),
                (),
                (),
                (),
                (),
            )
        if len(headers) != 1:
            self._state_invalid("current scope has multiple result headers")
        header = headers[0]
        source_run_id = header["source_run_id"]
        projection_batch_id = header["projection_batch_id"]
        owner = self._conn.execute(
            """
            SELECT 1
            FROM analysis_runs AS run
            JOIN forecast_projection_batches AS batch
                ON batch.id=? AND batch.run_id=run.id
            WHERE run.id=? AND run.scope_id=?
            """,
            (projection_batch_id, source_run_id, scope_id),
        ).fetchone()
        if owner is None:
            self._state_invalid("current result header ownership is invalid")
        if require_current_semantics:
            self._validate_current_header_semantics(
                source_run_id, projection_batch_id
            )

        statement_rows = self._conn.execute(
            """
            SELECT analysis_statement_id, source_run_id
            FROM current_statements
            WHERE scope_id=?
            ORDER BY analysis_statement_id
            """,
            (scope_id,),
        ).fetchall()
        mapping_rows = self._conn.execute(
            """
            SELECT analysis_mapping_id, source_run_id,
                   effective_asset, effective_eligibility
            FROM current_asset_mappings
            WHERE scope_id=?
            ORDER BY analysis_mapping_id
            """,
            (scope_id,),
        ).fetchall()
        forecast_rows = self._conn.execute(
            """
            SELECT analysis_forecast_id, source_run_id, projection_batch_id
            FROM current_forecasts
            WHERE scope_id=?
            ORDER BY analysis_forecast_id
            """,
            (scope_id,),
        ).fetchall()
        if any(row["source_run_id"] != source_run_id for row in statement_rows):
            self._state_invalid("current statements mix source runs")
        if any(row["source_run_id"] != source_run_id for row in mapping_rows):
            self._state_invalid("current mappings mix source runs")
        if any(
            row["source_run_id"] != source_run_id
            or row["projection_batch_id"] != projection_batch_id
            for row in forecast_rows
        ):
            self._state_invalid("current forecasts mix run or batch ownership")

        statement_ids = tuple(
            row["analysis_statement_id"] for row in statement_rows
        )
        mapping_ids = tuple(row["analysis_mapping_id"] for row in mapping_rows)
        forecast_ids = tuple(row["analysis_forecast_id"] for row in forecast_rows)
        expected_statement_ids = tuple(
            row["id"]
            for row in self._conn.execute(
                "SELECT id FROM analysis_statements WHERE run_id=? ORDER BY id",
                (source_run_id,),
            )
        )
        expected_mapping_ids = tuple(
            row["id"]
            for row in self._conn.execute(
                "SELECT id FROM analysis_asset_mappings WHERE run_id=? ORDER BY id",
                (source_run_id,),
            )
        )
        expected_forecast_ids = tuple(
            row["id"]
            for row in self._conn.execute(
                """
                SELECT id
                FROM analysis_forecasts
                WHERE projection_batch_id=? AND run_id=?
                ORDER BY id
                """,
                (projection_batch_id, source_run_id),
            )
        )
        if (
            statement_ids != expected_statement_ids
            or mapping_ids != expected_mapping_ids
            or forecast_ids != expected_forecast_ids
        ):
            self._state_invalid("current scope is incomplete or contains extra rows")
        try:
            effective_mappings = tuple(
                CurrentMappingReference(
                    row["analysis_mapping_id"],
                    Asset(row["effective_asset"]),
                    self._stored_bool(row["effective_eligibility"]),
                )
                for row in mapping_rows
            )
        except ValueError as cause:
            raise DomainError(
                "CURRENT_RESULT_STATE_INVALID",
                "current mapping references are invalid",
            ) from cause
        if require_current_semantics:
            try:
                expected_effective_mappings = []
                for item in effective_mappings:
                    effective = self._mapping_reviews.effective(
                        item.mapping_id
                    )
                    expected_effective_mappings.append(
                        CurrentMappingReference(
                            item.mapping_id,
                            effective.asset,
                            effective.heatmap_eligible,
                        )
                    )
            except (DomainError, TypeError, ValueError) as cause:
                raise DomainError(
                    "CURRENT_RESULT_STATE_INVALID",
                    "current mapping review state is invalid",
                ) from cause
            if effective_mappings != tuple(expected_effective_mappings):
                self._state_invalid(
                    "current mapping references do not match effective reviews"
                )
        eligible_mapping_ids = tuple(
            item.mapping_id
            for item in effective_mappings
            if item.effective_eligibility
        )
        return CurrentResultSummary(
            scope_id=scope_id,
            source_run_id=source_run_id,
            projection_batch_id=projection_batch_id,
            statement_count=len(statement_ids),
            mapping_count=len(mapping_ids),
            eligible_mapping_count=len(eligible_mapping_ids),
            forecast_count=len(forecast_ids),
            statement_ids=statement_ids,
            mapping_ids=mapping_ids,
            eligible_mapping_ids=eligible_mapping_ids,
            forecast_ids=forecast_ids,
            effective_mappings=effective_mappings,
        )

    def _validate_current_header_semantics(
        self, source_run_id: int, projection_batch_id: int
    ) -> None:
        try:
            batch = self._forecasts.get_batch(projection_batch_id)
            latest_batch_id = self._conn.execute(
                """
                SELECT MAX(id)
                FROM forecast_projection_batches
                WHERE run_id=?
                """,
                (source_run_id,),
            ).fetchone()[0]
            review_state = ForecastProjectionService(
                self._conn
            )._review_state(source_run_id)
            lineage_is_valid = self._review_batch_lineage_is_valid(batch)
        except (
            DomainError,
            KeyError,
            LookupError,
            TypeError,
            ValueError,
        ) as cause:
            raise DomainError(
                "CURRENT_RESULT_STATE_INVALID",
                "current result header semantics are invalid",
            ) from cause
        if (
            batch.run_id != source_run_id
            or latest_batch_id != projection_batch_id
            or batch.latest_mapping_review_id
            != review_state.latest_mapping_review_id
            or batch.latest_period_review_id
            != review_state.latest_period_review_id
            or not lineage_is_valid
        ):
            self._state_invalid(
                "current result header is stale or has invalid lineage"
            )

    def _delete_scope_rows(self, scope_id: int) -> None:
        self._conn.execute("DELETE FROM current_forecasts WHERE scope_id=?", (scope_id,))
        self._conn.execute(
            "DELETE FROM current_asset_mappings WHERE scope_id=?", (scope_id,)
        )
        self._conn.execute("DELETE FROM current_statements WHERE scope_id=?", (scope_id,))
        self._conn.execute("DELETE FROM current_result_sets WHERE scope_id=?", (scope_id,))

    def _insert_result_set(
        self, scope_id: int, run_id: int, projection_batch_id: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO current_result_sets(
                scope_id, source_run_id, projection_batch_id
            ) VALUES (?, ?, ?)
            """,
            (scope_id, run_id, projection_batch_id),
        )

    def _copy_statements(self, run_id: int, scope_id: int) -> None:
        self._conn.execute(
            """
            INSERT INTO current_statements(
                scope_id, analysis_statement_id, source_run_id
            )
            SELECT ?, id, run_id
            FROM analysis_statements
            WHERE run_id=?
            ORDER BY id
            """,
            (scope_id, run_id),
        )

    def _copy_mappings(self, run_id: int, scope_id: int) -> None:
        rows = []
        for mapping in self._mappings.list_run_mappings(run_id):
            effective = self._mapping_reviews.effective(mapping.id)
            rows.append(
                (
                    scope_id,
                    mapping.id,
                    run_id,
                    effective.asset.value,
                    int(effective.heatmap_eligible),
                )
            )
        self._conn.executemany(
            """
            INSERT INTO current_asset_mappings(
                scope_id,
                analysis_mapping_id,
                source_run_id,
                effective_asset,
                effective_eligibility
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _copy_forecasts(
        self, projection_batch_id: int, run_id: int, scope_id: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO current_forecasts(
                scope_id,
                analysis_forecast_id,
                source_run_id,
                projection_batch_id
            )
            SELECT ?, id, run_id, projection_batch_id
            FROM analysis_forecasts
            WHERE projection_batch_id=? AND run_id=?
            ORDER BY id
            """,
            (scope_id, projection_batch_id, run_id),
        )

    @staticmethod
    def _stored_bool(value: object) -> bool:
        if value not in (0, 1) or isinstance(value, bool):
            raise ValueError("stored boolean is invalid")
        return bool(value)

    @staticmethod
    def _validation_failed(message: str) -> None:
        raise DomainError("CURRENT_REPLACEMENT_VALIDATION_FAILED", message)

    @staticmethod
    def _state_invalid(message: str) -> None:
        raise DomainError("CURRENT_RESULT_STATE_INVALID", message)
