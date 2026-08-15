import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    JobStage,
    StatementType,
    TurningPointKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import JobUnit
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.job_state import JobStateService


CODEX_BATCH_UNIT_KEY: Final = "codex:batch:1"
_REQUIRED_MODEL: Final = "gpt-5.6-sol"
_REQUIRED_REASONING: Final = "max"
_REQUIRED_BOUNDARY: Final = "stored_statements_only"


class EvidenceProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    segment_id: int
    excerpt: str = Field(min_length=1, max_length=300)


class AssetHint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expression: str
    suggested_asset: Asset
    confidence: Confidence


class StatementProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    statement_type: StatementType
    forecast_basis: ForecastBasis | None
    condition_kind: ConditionKind
    condition_text: str | None
    direction_kind: DirectionKind | None
    turning_point_kind: TurningPointKind | None
    target_expression: str
    period_expression: str | None
    codex_asset_hints: tuple[AssetHint, ...] = ()
    evidence: tuple[EvidenceProposal, ...] = Field(min_length=1)


class AnalysisEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: int
    batch_key: str
    statements: tuple[StatementProposal, ...]


@dataclass(frozen=True, slots=True)
class CodexRunReceipt:
    model: str
    reasoning_effort: str
    tool_call_count: int
    boundary_mode: str


@dataclass(frozen=True, slots=True)
class ValidatedAnalysisOutput:
    id: int
    run_id: int
    job_id: int
    unit_key: str
    batch_ordinal: int
    canonical_output_json: str
    output_sha256: str
    receipt: CodexRunReceipt
    envelope: AnalysisEnvelope
    created_at: datetime


class CodexContractService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._job_state = JobStateService(conn, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def validate_and_store(
        self,
        run_id: int,
        unit_key: str,
        output_json: str,
        receipt: CodexRunReceipt,
    ) -> ValidatedAnalysisOutput:
        run = self._analysis.get_run(run_id)
        try:
            unit = self._require_running_unit(run.active_job_id, unit_key)
            self._validate_receipt(receipt)
            envelope = self._parse_envelope(output_json)
            self._validate_envelope(run_id, unit_key, envelope)
            self._validate_evidence_segments(run_id, envelope)
        except DomainError as error:
            self._record_failure(run_id, run.active_job_id, error.code)
            raise

        canonical_output = canonical_json(envelope.model_dump(mode="json"))
        output_sha256 = sha256_text(canonical_output)
        created_at = self._clock()
        try:
            with transaction(self._conn):
                current_run = self._analysis.get_run(run_id)
                if current_run.active_job_id != run.active_job_id:
                    raise DomainError(
                        "CODEX_UNIT_NOT_OWNED",
                        "Codex output must belong to the active run attempt",
                    )
                current_unit = self._require_running_unit(
                    current_run.active_job_id, unit_key
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO analysis_run_outputs(
                        run_id,
                        job_id,
                        unit_key,
                        batch_ordinal,
                        canonical_output_json,
                        output_sha256,
                        receipt_model,
                        receipt_reasoning_effort,
                        receipt_tool_call_count,
                        receipt_boundary_mode,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        current_run.active_job_id,
                        unit_key,
                        current_unit.ordinal,
                        canonical_output,
                        output_sha256,
                        receipt.model,
                        receipt.reasoning_effort,
                        receipt.tool_call_count,
                        receipt.boundary_mode,
                        utc_iso(created_at),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("analysis output insert returned no id")
                output_id = cursor.lastrowid
                self._analysis.append_run_event(
                    run_id,
                    AnalysisRunStatus.TRANSPORT_VALIDATED,
                    None,
                    created_at=created_at,
                )
                self._job_state.complete_unit_in_transaction(
                    current_run.active_job_id, unit_key, output_sha256
                )
        except DomainError as error:
            self._record_failure(run_id, run.active_job_id, error.code)
            raise
        except sqlite3.DatabaseError as cause:
            error = DomainError(
                "CODEX_OUTPUT_STORAGE_FAILED",
                "validated Codex output could not be stored",
            )
            self._record_failure(run_id, run.active_job_id, error.code)
            raise error from cause

        return ValidatedAnalysisOutput(
            id=output_id,
            run_id=run_id,
            job_id=run.active_job_id,
            unit_key=unit_key,
            batch_ordinal=unit.ordinal,
            canonical_output_json=canonical_output,
            output_sha256=output_sha256,
            receipt=receipt,
            envelope=envelope,
            created_at=created_at,
        )

    def _require_running_unit(self, job_id: int, unit_key: str) -> JobUnit:
        if unit_key != CODEX_BATCH_UNIT_KEY:
            raise DomainError(
                "CODEX_BATCH_KEY_MISMATCH",
                "Codex output requires the exact manifest batch key",
            )
        try:
            unit = self._job_state.unit(job_id, unit_key)
        except DomainError as cause:
            if cause.code != "JOB_UNIT_NOT_FOUND":
                raise
            raise DomainError(
                "CODEX_UNIT_NOT_OWNED",
                "Codex unit does not belong to the active run attempt",
            ) from cause
        if unit.stage is not JobStage.CODEX_ANALYSIS:
            raise DomainError(
                "CODEX_UNIT_STAGE_MISMATCH",
                "Codex output requires a Codex analysis unit",
            )
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "CODEX_UNIT_NOT_RUNNING",
                "Codex output requires a running unit",
            )
        return unit

    @staticmethod
    def _validate_receipt(receipt: CodexRunReceipt) -> None:
        if not isinstance(receipt, CodexRunReceipt):
            raise DomainError(
                "CODEX_RECEIPT_INVALID", "Codex receipt has an invalid shape"
            )
        if receipt.model != _REQUIRED_MODEL:
            raise DomainError(
                "CODEX_MODEL_MISMATCH", "Codex model does not match the contract"
            )
        if receipt.reasoning_effort != _REQUIRED_REASONING:
            raise DomainError(
                "CODEX_REASONING_MISMATCH",
                "Codex reasoning effort does not match the contract",
            )
        if type(receipt.tool_call_count) is not int or receipt.tool_call_count != 0:
            raise DomainError(
                "CODEX_TOOL_CALL_DETECTED",
                "Codex output used a prohibited external tool",
            )
        if receipt.boundary_mode != _REQUIRED_BOUNDARY:
            raise DomainError(
                "CODEX_BOUNDARY_MISMATCH",
                "Codex information boundary does not match the contract",
            )

    @staticmethod
    def _parse_envelope(output_json: str) -> AnalysisEnvelope:
        if not isinstance(output_json, str):
            raise DomainError(
                "CODEX_OUTPUT_SCHEMA_INVALID",
                "Codex output does not satisfy the required schema",
            )
        try:
            return AnalysisEnvelope.model_validate_json(output_json)
        except ValidationError as cause:
            raise DomainError(
                "CODEX_OUTPUT_SCHEMA_INVALID",
                "Codex output does not satisfy the required schema",
            ) from cause

    @staticmethod
    def _validate_envelope(
        run_id: int, unit_key: str, envelope: AnalysisEnvelope
    ) -> None:
        if envelope.run_id != run_id:
            raise DomainError(
                "CODEX_RUN_ID_MISMATCH",
                "Codex output belongs to a different analysis run",
            )
        if (
            envelope.batch_key != CODEX_BATCH_UNIT_KEY
            or envelope.batch_key != unit_key
        ):
            raise DomainError(
                "CODEX_BATCH_KEY_MISMATCH",
                "Codex output requires the exact manifest batch key",
            )

    def _validate_evidence_segments(
        self, run_id: int, envelope: AnalysisEnvelope
    ) -> None:
        allowed = {
            segment.segment_id
            for segment in self._analysis.get_input_segments(run_id)
        }
        if any(
            evidence.segment_id not in allowed
            for statement in envelope.statements
            for evidence in statement.evidence
        ):
            raise DomainError(
                "CODEX_EVIDENCE_SEGMENT_NOT_IN_RUN",
                "Codex evidence must belong to the immutable run input",
            )

    def _record_failure(
        self, run_id: int, active_job_id: int, error_code: str
    ) -> None:
        with transaction(self._conn):
            self._analysis.append_run_event(
                run_id,
                AnalysisRunStatus.FAILED,
                error_code,
                created_at=self._clock(),
            )
            unit = self._job_state.unit(active_job_id, CODEX_BATCH_UNIT_KEY)
            if (
                unit.stage is JobStage.CODEX_ANALYSIS
                and unit.status is UnitStatus.RUNNING
            ):
                self._job_state.fail_unit_in_transaction(
                    active_job_id, CODEX_BATCH_UNIT_KEY, error_code
                )
