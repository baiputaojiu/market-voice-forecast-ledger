import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import ValidationError

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.analysis import RunSegment
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    JobStage,
    StatementType,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.domain.speakers import (
    SpeakerAssignment,
    TranscriptSegment,
)
from market_voice_forecast_ledger.domain.statements import (
    EvidenceLink,
    NormalizedStatement,
    validate_statement,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.statements import (
    StatementRepository,
)
from market_voice_forecast_ledger.services.codex_contract import (
    AnalysisEnvelope,
    StatementProposal,
)
from market_voice_forecast_ledger.services.job_state import JobStateService


@dataclass(frozen=True, slots=True)
class _ResolvedEvidence:
    ordinal: int
    run_segment_id: int
    segment_id: int
    excerpt: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class _ResolvedStatement:
    ordinal: int
    batch_ordinal: int
    proposal_ordinal: int
    source_video_id: int
    proposal: StatementProposal
    heatmap_candidate: bool
    evidence: tuple[_ResolvedEvidence, ...]


class StatementService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._analysis = AnalysisRepository(conn)
        self._speakers = SpeakerRepository(conn)
        self._statements = StatementRepository(conn)
        self._job_state = JobStateService(conn, clock=clock)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def normalize_and_store(
        self, run_id: int
    ) -> tuple[NormalizedStatement, ...]:
        run = self._analysis.get_run(run_id)
        unit = self._job_state.unit(
            run.active_job_id, STATEMENT_NORMALIZATION_UNIT_KEY
        )
        if unit.status is UnitStatus.SUCCESS:
            return self._statements.list_run_statements(run_id)
        if unit.status is not UnitStatus.RUNNING:
            raise DomainError(
                "STATEMENT_NORMALIZATION_UNIT_NOT_RUNNING",
                "statement normalization requires a running unit",
            )

        try:
            with transaction(self._conn):
                current_run = self._analysis.get_run(run_id)
                if current_run.active_job_id != run.active_job_id:
                    raise DomainError(
                        "STATEMENT_NORMALIZATION_UNIT_NOT_OWNED",
                        "statement unit must belong to the active run attempt",
                    )
                current_unit = self._job_state.unit(
                    current_run.active_job_id,
                    STATEMENT_NORMALIZATION_UNIT_KEY,
                )
                if current_unit.status is not UnitStatus.RUNNING:
                    raise DomainError(
                        "STATEMENT_NORMALIZATION_UNIT_NOT_RUNNING",
                        "statement normalization requires a running unit",
                    )

                resolved = self._resolve_statements(
                    run_id, current_run.active_job_id
                )
                created_at = self._clock()
                for item in resolved:
                    statement_id = self._statements.insert_statement(
                        run_id=run_id,
                        ordinal=item.ordinal,
                        batch_ordinal=item.batch_ordinal,
                        proposal_ordinal=item.proposal_ordinal,
                        source_video_id=item.source_video_id,
                        statement_type=item.proposal.statement_type,
                        forecast_basis=item.proposal.forecast_basis,
                        condition_kind=item.proposal.condition_kind,
                        condition_text=item.proposal.condition_text,
                        direction_kind=item.proposal.direction_kind,
                        turning_point_kind=item.proposal.turning_point_kind,
                        target_expression=item.proposal.target_expression,
                        period_expression=item.proposal.period_expression,
                        heatmap_candidate=item.heatmap_candidate,
                        created_at=created_at,
                    )
                    self._statements.insert_evidence_links(
                        tuple(
                            EvidenceLink(
                                statement_id=statement_id,
                                ordinal=evidence.ordinal,
                                run_segment_id=evidence.run_segment_id,
                                segment_id=evidence.segment_id,
                                excerpt=evidence.excerpt,
                                start_ms=evidence.start_ms,
                                end_ms=evidence.end_ms,
                            )
                            for evidence in item.evidence
                        )
                    )
                output_hash = sha256_text(
                    canonical_json(self._artifact_payload(resolved))
                )
                self._job_state.complete_unit_in_transaction(
                    current_run.active_job_id,
                    STATEMENT_NORMALIZATION_UNIT_KEY,
                    output_hash,
                )
        except DomainError as error:
            self._record_failure(run.active_job_id, error.code)
            raise
        except (sqlite3.DatabaseError, RuntimeError) as cause:
            error = DomainError(
                "STATEMENT_STORAGE_FAILED",
                "normalized statements could not be stored",
            )
            self._record_failure(run.active_job_id, error.code)
            raise error from cause

        return self._statements.list_run_statements(run_id)

    def _resolve_statements(
        self, run_id: int, job_id: int
    ) -> tuple[_ResolvedStatement, ...]:
        run = self._analysis.get_run(run_id)
        scope = self._analysis.get_scope(run.scope_id)
        subject_kind = self._analysis.get_active_subject_kind(scope.subject_id)
        ordered_envelopes = self._ordered_envelopes(run_id, job_id)
        evidence_segment_ids = tuple(
            dict.fromkeys(
                evidence.segment_id
                for _, envelope in ordered_envelopes
                for proposal in envelope.statements
                for evidence in proposal.evidence
            )
        )
        run_segments = self._analysis.get_input_segments(run_id)
        by_segment_id = {segment.segment_id: segment for segment in run_segments}
        current_segments = {
            segment_id: self._current_segment(segment_id)
            for segment_id in evidence_segment_ids
            if segment_id in by_segment_id
        }
        assignments = self._load_assignments(evidence_segment_ids)

        resolved: list[_ResolvedStatement] = []
        statement_ordinal = 0
        for batch_ordinal, envelope in ordered_envelopes:
            for proposal_ordinal, proposal in enumerate(
                envelope.statements, start=1
            ):
                validate_statement(proposal)
                statement_ordinal += 1
                evidence = self._resolve_evidence(
                    proposal,
                    by_segment_id,
                    current_segments,
                    assignments,
                    subject_kind,
                    scope.subject_id,
                )
                resolved.append(
                    _ResolvedStatement(
                        ordinal=statement_ordinal,
                        batch_ordinal=batch_ordinal,
                        proposal_ordinal=proposal_ordinal,
                        source_video_id=by_segment_id[
                            proposal.evidence[0].segment_id
                        ].video_id,
                        proposal=proposal,
                        heatmap_candidate=(
                            proposal.statement_type
                            is StatementType.FUTURE_FORECAST
                        ),
                        evidence=evidence,
                    )
                )
        return tuple(resolved)

    def _load_assignments(
        self, segment_ids: tuple[int, ...]
    ) -> dict[int, SpeakerAssignment]:
        variable_limit = self._conn.getlimit(
            sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
        )
        assignments: dict[int, SpeakerAssignment] = {}
        for offset in range(0, len(segment_ids), variable_limit):
            chunk = segment_ids[offset : offset + variable_limit]
            assignments.update(
                (assignment.segment_id, assignment)
                for assignment in self._speakers.list_assignments(chunk)
            )
        return assignments

    def _ordered_envelopes(
        self, run_id: int, job_id: int
    ) -> tuple[tuple[int, AnalysisEnvelope], ...]:
        codex_units = self._conn.execute(
            """
            SELECT unit_key, ordinal, status, output_hash
            FROM job_units
            WHERE job_id=? AND stage=?
            ORDER BY ordinal
            """,
            (job_id, JobStage.CODEX_ANALYSIS.value),
        ).fetchall()
        if not codex_units:
            raise DomainError(
                "CODEX_BATCH_MANIFEST_INVALID",
                "analysis manifest has no Codex batch",
            )

        envelopes: list[tuple[int, AnalysisEnvelope]] = []
        for unit in codex_units:
            if UnitStatus(unit["status"]) is not UnitStatus.SUCCESS:
                raise DomainError(
                    "CODEX_BATCH_NOT_SUCCESS",
                    "every Codex batch must be successful",
                )
            output_rows = self._conn.execute(
                """
                SELECT *
                FROM analysis_run_outputs
                WHERE run_id=? AND unit_key=?
                ORDER BY id
                """,
                (run_id, unit["unit_key"]),
            ).fetchall()
            if len(output_rows) != 1:
                raise DomainError(
                    "CODEX_BATCH_OUTPUT_COUNT_INVALID",
                    "each Codex batch requires exactly one immutable output",
                )
            output = output_rows[0]
            if unit["output_hash"] != output["output_sha256"]:
                raise DomainError(
                    "CODEX_BATCH_OUTPUT_HASH_MISMATCH",
                    "successful Codex unit does not match its immutable output",
                )
            try:
                envelope = AnalysisEnvelope.model_validate_json(
                    output["canonical_output_json"]
                )
            except ValidationError as cause:
                raise DomainError(
                    "CODEX_BATCH_OUTPUT_INVALID",
                    "stored Codex output no longer satisfies its schema",
                ) from cause
            expected_canonical = canonical_json(envelope.model_dump(mode="json"))
            if (
                output["batch_ordinal"] != unit["ordinal"]
                or output["canonical_output_json"] != expected_canonical
                or output["output_sha256"] != sha256_text(expected_canonical)
                or envelope.run_id != run_id
                or envelope.batch_key != unit["unit_key"]
            ):
                raise DomainError(
                    "CODEX_BATCH_OUTPUT_INVALID",
                    "stored Codex output does not match its manifest batch",
                )
            envelopes.append((unit["ordinal"], envelope))
        return tuple(envelopes)

    def _resolve_evidence(
        self,
        proposal: StatementProposal,
        run_segments: dict[int, RunSegment],
        current_segments: dict[int, TranscriptSegment],
        assignments: dict[int, SpeakerAssignment],
        subject_kind: SubjectKind,
        subject_id: int,
    ) -> tuple[_ResolvedEvidence, ...]:
        seen: set[int] = set()
        resolved: list[_ResolvedEvidence] = []
        for ordinal, evidence in enumerate(proposal.evidence, start=1):
            if evidence.segment_id in seen:
                raise DomainError(
                    "DUPLICATE_EVIDENCE_SEGMENT",
                    "statement evidence segments must be unique",
                )
            seen.add(evidence.segment_id)
            run_segment = run_segments.get(evidence.segment_id)
            if run_segment is None:
                raise DomainError(
                    "EVIDENCE_SEGMENT_NOT_IN_RUN",
                    "statement evidence must belong to the immutable run",
                )
            segment = current_segments[evidence.segment_id]
            assignment = assignments.get(evidence.segment_id)
            self._validate_assignment(
                run_segment,
                assignment,
                subject_kind,
                subject_id,
            )
            if segment.text_body is None:
                raise DomainError(
                    "EVIDENCE_SOURCE_TEXT_UNAVAILABLE",
                    "statement evidence requires retained transcript text",
                )
            if len(evidence.excerpt) > 300:
                raise DomainError(
                    "EVIDENCE_EXCERPT_TOO_LONG",
                    "statement evidence exceeds 300 Unicode code points",
                )
            if not evidence.excerpt or evidence.excerpt not in segment.text_body:
                raise DomainError(
                    "EVIDENCE_NOT_CONTIGUOUS_SOURCE_TEXT",
                    "statement evidence must be contiguous source text",
                )
            resolved.append(
                _ResolvedEvidence(
                    ordinal=ordinal,
                    run_segment_id=run_segment.id,
                    segment_id=evidence.segment_id,
                    excerpt=evidence.excerpt,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _validate_assignment(
        run_segment: RunSegment,
        assignment: SpeakerAssignment | None,
        subject_kind: SubjectKind,
        subject_id: int,
    ) -> None:
        frozen_subject_matches = (
            run_segment.assignment_kind is AssignmentKind.SUBJECT
            and run_segment.assigned_subject_id == subject_id
        )
        current_subject_matches = (
            assignment is not None
            and assignment.assignment_kind is AssignmentKind.SUBJECT
            and assignment.assigned_subject_id == subject_id
        )
        if subject_kind is SubjectKind.PERSON:
            if not frozen_subject_matches or not current_subject_matches:
                raise DomainError(
                    "EVIDENCE_SUBJECT_ASSIGNMENT_REQUIRED",
                    "personal evidence must remain assigned to the run subject",
                )
            return
        if (
            not frozen_subject_matches
            or not current_subject_matches
            or assignment is None
            or assignment.assignment_origin
            is not AssignmentOrigin.CHANNEL_ORGANIZATION
        ):
            raise DomainError(
                "EVIDENCE_ORGANIZATION_ASSIGNMENT_REQUIRED",
                "organization evidence must remain channel assigned",
            )

    def _current_segment(self, segment_id: int) -> TranscriptSegment:
        try:
            return self._speakers.get_segment(segment_id)
        except LookupError as cause:
            raise DomainError(
                "EVIDENCE_SEGMENT_NOT_FOUND",
                "statement evidence segment no longer exists",
            ) from cause

    @staticmethod
    def _artifact_payload(
        statements: tuple[_ResolvedStatement, ...]
    ) -> list[dict[str, object]]:
        return [
            {
                "ordinal": statement.ordinal,
                "batch_ordinal": statement.batch_ordinal,
                "proposal_ordinal": statement.proposal_ordinal,
                "source_video_id": statement.source_video_id,
                "statement_type": statement.proposal.statement_type.value,
                "forecast_basis": (
                    None
                    if statement.proposal.forecast_basis is None
                    else statement.proposal.forecast_basis.value
                ),
                "condition_kind": statement.proposal.condition_kind.value,
                "condition_text": statement.proposal.condition_text,
                "direction_kind": (
                    None
                    if statement.proposal.direction_kind is None
                    else statement.proposal.direction_kind.value
                ),
                "turning_point_kind": (
                    None
                    if statement.proposal.turning_point_kind is None
                    else statement.proposal.turning_point_kind.value
                ),
                "target_expression": statement.proposal.target_expression,
                "period_expression": statement.proposal.period_expression,
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
                    for evidence in statement.evidence
                ],
            }
            for statement in statements
        ]

    def _record_failure(self, job_id: int, error_code: str) -> None:
        unit = self._job_state.unit(job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
        if unit.status is UnitStatus.RUNNING:
            self._job_state.fail_unit(
                job_id, STATEMENT_NORMALIZATION_UNIT_KEY, error_code
            )
