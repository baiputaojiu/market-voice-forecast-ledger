import json
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
    MarketCode,
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
            mappings = self._mappings.list_run_mappings(run_id)
            stored_hash = sha256_text(
                canonical_json(self._artifact_payload(mappings))
            )
            if unit.output_hash != stored_hash:
                raise DomainError(
                    "ASSET_MAPPING_OUTPUT_HASH_MISMATCH",
                    "stored asset mappings do not match the successful unit",
                )
            return mappings
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
        interviewer_by_video = self._frozen_interviewer_evidence(
            run_id, subject_kind
        )

        resolved: list[AssetMapping] = []
        for statement in statements:
            context = StatementContext(
                subject_kind=subject_kind,
                codex_asset_hints=hints[statement.id],
                adopted_subject_evidence=self._surrounding_evidence(
                    statement, statements
                ),
                interviewer_evidence=interviewer_by_video.get(
                    statement.source_video_id, ()
                ),
            )
            resolved.extend(map_statement(statement, context))
        return tuple(resolved)

    def _frozen_interviewer_evidence(
        self, run_id: int, subject_kind: SubjectKind
    ) -> dict[int, tuple[MarketEvidence, ...]]:
        try:
            metadata = json.loads(self._analysis.get_snapshot(run_id).metadata_json)
            if (
                not isinstance(metadata, dict)
                or metadata.get("subject_kind") != subject_kind.value
                or "interviewer_market_context" not in metadata
            ):
                raise ValueError
            raw_context = metadata["interviewer_market_context"]
            if not isinstance(raw_context, list):
                raise ValueError
            if subject_kind is SubjectKind.ORGANIZATION:
                if raw_context:
                    raise ValueError
                return {}

            by_video: dict[int, list[MarketEvidence]] = {}
            seen_segment_ids: set[int] = set()
            for item in raw_context:
                if not isinstance(item, dict) or set(item) != {
                    "assignment_sha256",
                    "market_codes",
                    "segment_id",
                    "text_sha256",
                    "video_id",
                }:
                    raise ValueError
                segment_id = item["segment_id"]
                video_id = item["video_id"]
                assignment_sha256 = item["assignment_sha256"]
                text_sha256 = item["text_sha256"]
                market_values = item["market_codes"]
                if (
                    not isinstance(segment_id, int)
                    or isinstance(segment_id, bool)
                    or segment_id <= 0
                    or segment_id in seen_segment_ids
                    or not isinstance(video_id, int)
                    or isinstance(video_id, bool)
                    or video_id <= 0
                    or not self._is_sha256(assignment_sha256)
                    or not self._is_sha256(text_sha256)
                    or not isinstance(market_values, list)
                ):
                    raise ValueError
                market_codes = tuple(MarketCode(value) for value in market_values)
                if (
                    any(not isinstance(value, str) for value in market_values)
                    or len(set(market_codes)) != len(market_codes)
                ):
                    raise ValueError
                seen_segment_ids.add(segment_id)
                by_video.setdefault(video_id, []).extend(
                    MarketEvidence(segment_id, market_code)
                    for market_code in market_codes
                )
            return {
                video_id: tuple(evidence)
                for video_id, evidence in by_video.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as cause:
            raise DomainError(
                "ASSET_MAPPING_INTERVIEWER_CONTEXT_INVALID",
                "frozen interviewer context is not a safe mapping input",
            ) from cause

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

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
            if any(
                hint.expression != statement.target_expression
                for hint in proposal.codex_asset_hints
            ):
                raise DomainError(
                    "ASSET_MAPPING_CODEX_HINT_EXPRESSION_MISMATCH",
                    "Codex asset hint does not match its statement target",
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
