import sqlite3

from pydantic import ValidationError

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import SubjectKind, UnitStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ASSET_MAPPING_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.domain.mappings import (
    AssetMapping,
    MarketEvidence,
    StatementContext,
    expression_market_codes,
    map_statement,
)
from market_voice_forecast_ledger.domain.statements import NormalizedStatement
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.mappings import MappingRepository
from market_voice_forecast_ledger.repositories.statements import (
    StatementRepository,
)
from market_voice_forecast_ledger.services.codex_contract import (
    AnalysisEnvelope,
    AssetHint,
)
from market_voice_forecast_ledger.services.job_state import JobStateService


class AssetMappingService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._statements = StatementRepository(conn)
        self._mappings = MappingRepository(conn)
        self._job_state = JobStateService(conn)

    def map_run(self, run_id: int) -> tuple[AssetMapping, ...]:
        run = self._analysis.get_run(run_id)
        unit = self._job_state.unit(
            run.active_job_id, ASSET_MAPPING_UNIT_KEY
        )
        if unit.status is UnitStatus.SUCCESS:
            return self._mappings.list_run_mappings(run_id)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "ASSET_MAPPING_UNIT_NOT_RUNNING",
                "asset mapping requires a running unit",
            )

        try:
            with transaction(self._conn):
                current_run = self._analysis.get_run(run_id)
                if current_run.active_job_id != run.active_job_id:
                    raise DomainError(
                        "ASSET_MAPPING_UNIT_NOT_OWNED",
                        "mapping unit must belong to the active run attempt",
                    )
                current_unit = self._job_state.unit(
                    current_run.active_job_id, ASSET_MAPPING_UNIT_KEY
                )
                if current_unit.status is not UnitStatus.RUNNING:
                    raise DomainError(
                        "ASSET_MAPPING_UNIT_NOT_RUNNING",
                        "asset mapping requires a running unit",
                    )
                self._require_upstream_success(current_run.active_job_id)

                mappings = self._resolve_mappings(run_id)
                for mapping in mappings:
                    self._mappings.insert(mapping)
                output_hash = sha256_text(
                    canonical_json(self._artifact_payload(mappings))
                )
                self._job_state.complete_unit_in_transaction(
                    current_run.active_job_id,
                    ASSET_MAPPING_UNIT_KEY,
                    output_hash,
                )
        except DomainError as error:
            self._record_failure(run.active_job_id, error.code)
            raise
        except (
            ValidationError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as cause:
            error = DomainError(
                "ASSET_MAPPING_RULE_FAILED",
                "asset mapping rules could not be applied",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause
        except (sqlite3.DatabaseError, RuntimeError) as cause:
            error = DomainError(
                "ASSET_MAPPING_STORAGE_FAILED",
                "asset mappings could not be stored",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause

        return self._mappings.list_run_mappings(run_id)

    def _require_upstream_success(self, job_id: int) -> None:
        requirements = (
            (
                STATEMENT_NORMALIZATION_UNIT_KEY,
                "STATEMENT_NORMALIZATION_NOT_SUCCESSFUL",
            ),
            (
                PERIOD_NORMALIZATION_UNIT_KEY,
                "PERIOD_NORMALIZATION_NOT_SUCCESSFUL",
            ),
        )
        for unit_key, error_code in requirements:
            unit = self._job_state.unit(job_id, unit_key)
            if unit.status is not UnitStatus.SUCCESS or unit.output_hash is None:
                raise DomainError(
                    error_code,
                    "asset mapping requires successful upstream units",
                )

    def _resolve_mappings(self, run_id: int) -> tuple[AssetMapping, ...]:
        run = self._analysis.get_run(run_id)
        scope = self._analysis.get_scope(run.scope_id)
        subject_kind = self._analysis.get_active_subject_kind(scope.subject_id)
        statements = self._statements.list_run_statements(run_id)
        hints = self._hints_by_statement(run_id, statements)

        resolved: list[AssetMapping] = []
        for statement in statements:
            context = StatementContext(
                subject_kind=subject_kind,
                codex_asset_hints=hints[statement.id],
                adopted_subject_evidence=self._surrounding_evidence(
                    statement, statements
                ),
            )
            resolved.extend(map_statement(statement, context))
        return tuple(resolved)

    def _hints_by_statement(
        self,
        run_id: int,
        statements: tuple[NormalizedStatement, ...],
    ) -> dict[int, tuple[AssetHint, ...]]:
        rows = self._conn.execute(
            """
            SELECT batch_ordinal, canonical_output_json
            FROM analysis_run_outputs
            WHERE run_id=?
            ORDER BY batch_ordinal
            """,
            (run_id,),
        ).fetchall()
        proposals = {}
        for row in rows:
            envelope = AnalysisEnvelope.model_validate_json(
                row["canonical_output_json"]
            )
            if envelope.run_id != run_id:
                raise DomainError(
                    "ASSET_MAPPING_CODEX_OUTPUT_INVALID",
                    "stored Codex output belongs to another run",
                )
            for proposal_ordinal, proposal in enumerate(
                envelope.statements, start=1
            ):
                key = (row["batch_ordinal"], proposal_ordinal)
                if key in proposals:
                    raise DomainError(
                        "ASSET_MAPPING_CODEX_OUTPUT_INVALID",
                        "stored Codex statement identity is duplicated",
                    )
                proposals[key] = proposal

        result: dict[int, tuple[AssetHint, ...]] = {}
        for statement in statements:
            proposal = proposals.get(
                (statement.batch_ordinal, statement.proposal_ordinal)
            )
            if (
                proposal is None
                or proposal.target_expression != statement.target_expression
            ):
                raise DomainError(
                    "ASSET_MAPPING_CODEX_STATEMENT_MISMATCH",
                    "normalized statement does not match its Codex proposal",
                )
            result[statement.id] = proposal.codex_asset_hints
        if len(proposals) != len(statements):
            raise DomainError(
                "ASSET_MAPPING_CODEX_STATEMENT_MISMATCH",
                "Codex and normalized statement counts differ",
            )
        return result

    @staticmethod
    def _surrounding_evidence(
        statement: NormalizedStatement,
        statements: tuple[NormalizedStatement, ...],
    ) -> tuple[MarketEvidence, ...]:
        evidence: list[MarketEvidence] = []
        for surrounding in statements:
            if (
                surrounding.id == statement.id
                or surrounding.source_video_id != statement.source_video_id
            ):
                continue
            for market_code in expression_market_codes(
                surrounding.target_expression
            ):
                evidence.extend(
                    MarketEvidence(link.segment_id, market_code)
                    for link in surrounding.evidence_links
                )
        return tuple(evidence)

    @staticmethod
    def _artifact_payload(
        mappings: tuple[AssetMapping, ...]
    ) -> list[dict[str, object]]:
        return [
            {
                "asset": mapping.asset.value,
                "codex_confidence": mapping.codex_confidence.value,
                "confidence_disagrees": mapping.confidence_disagrees,
                "conversion_reason": mapping.reason_code,
                "final_confidence": mapping.final_confidence.value,
                "mapping_kind": mapping.mapping_kind.value,
                "original_expression": mapping.original_expression,
                "rule_confidence": mapping.rule_confidence.value,
                "rule_evidence": [
                    row.to_safe_dict() for row in mapping.rule_evidence
                ],
                "run_id": mapping.run_id,
                "source_video_id": mapping.source_video_id,
                "statement_id": mapping.statement_id,
            }
            for mapping in mappings
        ]

    def _record_failure(self, job_id: int, error_code: str) -> None:
        unit = self._job_state.unit(job_id, ASSET_MAPPING_UNIT_KEY)
        if unit.status is UnitStatus.RUNNING:
            self._job_state.fail_unit(
                job_id, ASSET_MAPPING_UNIT_KEY, error_code
            )
