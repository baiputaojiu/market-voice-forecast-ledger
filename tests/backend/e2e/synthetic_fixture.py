from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Final
from unittest.mock import patch

from fastapi.testclient import TestClient

from market_voice_forecast_ledger.api import dependencies
from market_voice_forecast_ledger.api.app import create_app
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.analysis import (
    AnalysisRunSettings,
    BeginAnalysisRun,
)
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    AssignmentKind,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    HeatmapGranularity,
    JobKind,
    JobStage,
    MappingReviewDecision,
    PeriodReviewDecision,
    StatementType,
    TurningPointKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.forecasts import (
    ForecastProjectionBatch,
    ProjectedForecast,
    ProjectionTrigger,
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
)
from market_voice_forecast_ledger.domain.mappings import AssetMapping
from market_voice_forecast_ledger.domain.periods import NormalizedPeriod
from market_voice_forecast_ledger.domain.speakers import (
    PersonalAssignmentCommand,
    ScoreRule,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.statements import (
    EvidenceLink,
    NormalizedStatement,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from market_voice_forecast_ledger.services.asset_mapping import AssetMappingService
from market_voice_forecast_ledger.services.codex_contract import (
    CODEX_BATCH_UNIT_KEY,
    AnalysisEnvelope,
    AssetHint,
    CodexContractService,
    CodexRunReceipt,
    EvidenceProposal,
    StatementProposal,
    ValidatedAnalysisOutput,
)
from market_voice_forecast_ledger.services.current_results import (
    CurrentResultService,
)
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.heatmap import (
    HeatmapCell,
    HeatmapService,
    HeatmapView,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
)
from market_voice_forecast_ledger.services.periods import PeriodService
from market_voice_forecast_ledger.services.review_application import (
    ReviewApplicationResult,
    ReviewApplicationService,
)
from market_voice_forecast_ledger.services.speaker_assignment import (
    SpeakerAssignmentService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.synthetic_collection_fixture import (
    create_synthetic_collection_candidate,
)


UTC: Final = timezone.utc
SYNTHETIC_CUTOFF: Final = date(2026, 8, 14)
SYNTHETIC_CREATED_AT: Final = datetime(
    2026, 8, 14, 14, 30, 0, 123456, tzinfo=UTC
)
SYNTHETIC_THRESHOLD: Final = SpeakerThresholdConfig(
    version="synthetic-speaker-threshold-v1",
    model_name="synthetic-speaker-model",
    model_version="1.0",
    subject_rule=ScoreRule("gte", 0.8),
    interviewer_rule=ScoreRule("lte", 0.2),
)
_SUBJECT_ROLES: Final = (
    "personal_japan",
    "organization_us",
    "conflict_history",
    "review_boundary",
)
_SUBJECT_DEFINITIONS: Final = MappingProxyType(
    {
        "personal_japan": (
            "Synthetic Personal Japan",
            1,
        ),
        "organization_us": (
            "Synthetic Organization US",
            2,
        ),
        "conflict_history": (
            "Synthetic Conflict History",
            3,
        ),
        "review_boundary": (
            "Synthetic Review Boundary",
            4,
        ),
    }
)


def _synthetic_clock() -> datetime:
    return SYNTHETIC_CREATED_AT


@dataclass(frozen=True, slots=True)
class SyntheticStatementSpec:
    label: str
    youtube_video_id: str
    published_at: datetime
    excerpt: str
    statement_type: StatementType = StatementType.FUTURE_FORECAST
    forecast_basis: ForecastBasis | None = ForecastBasis.DIRECT
    condition_kind: ConditionKind = ConditionKind.UNCONDITIONAL
    condition_text: str | None = None
    direction: DirectionKind | None = DirectionKind.UP
    turning_point_kind: TurningPointKind | None = None
    target_expression: str = "日経平均"
    period_expression: str | None = "来週"
    hints: tuple[tuple[Asset, Confidence], ...] = (
        (Asset.NIKKEI_225, Confidence.HIGH),
    )
    private_body: str | None = None

    def body(self) -> str:
        return self.private_body or (
            f"{self.excerpt} Private synthetic continuation for {self.label}."
        )


@dataclass(frozen=True, slots=True)
class SyntheticVideo:
    id: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class SyntheticSource:
    label: str
    video: SyntheticVideo
    segment_id: int
    body: str


@dataclass(frozen=True, slots=True)
class PreparedSyntheticRun:
    subject_id: int
    run_id: int
    job_id: int
    statement_ids: tuple[int, ...]
    period_ids: tuple[int, ...]
    mapping_ids: tuple[int, ...]
    video_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SyntheticRunEvidence:
    role: str
    prepared: PreparedSyntheticRun
    scope_id: int
    output: ValidatedAnalysisOutput
    statements: tuple[NormalizedStatement, ...]
    periods: tuple[NormalizedPeriod, ...]
    mappings: tuple[AssetMapping, ...]
    batch: ForecastProjectionBatch
    sources: tuple[SyntheticSource, ...]

    def statement(self, label: str) -> NormalizedStatement:
        labels = tuple(source.label for source in self.sources)
        try:
            ordinal = labels.index(label)
        except ValueError as cause:
            raise KeyError(label) from cause
        return self.statements[ordinal]

    def source(self, label: str) -> SyntheticSource:
        for source in self.sources:
            if source.label == label:
                return source
        raise KeyError(label)


@dataclass(frozen=True, slots=True)
class MappingReviewEvidence:
    decision: MappingReviewDecision
    before_confidence: Confidence
    asset: Asset
    result: ReviewApplicationResult


@dataclass(frozen=True, slots=True)
class PeriodReviewEvidence:
    decision: PeriodReviewDecision
    result: ReviewApplicationResult


@dataclass(frozen=True, slots=True)
class SyntheticFlowEvidence:
    runtime_dir: Path
    cutoff_day: date
    subject_roles: tuple[str, ...]
    role_names: tuple[tuple[str, str], ...]
    runs: tuple[SyntheticRunEvidence, ...]
    before_reviews_week: HeatmapView
    week: HeatmapView
    month: HeatmapView
    api_week: dict[str, object]
    api_month: dict[str, object]
    mapping_review_evidence: tuple[MappingReviewEvidence, ...]
    period_review_evidence: tuple[PeriodReviewEvidence, ...]
    later_source: SyntheticVideo
    later_segment_id: int
    original_video_id: int
    repost_video_id: int

    @property
    def subject_names(self) -> tuple[str, ...]:
        return tuple(name for _, name in self.role_names)

    @property
    def statements(self) -> tuple[NormalizedStatement, ...]:
        return tuple(statement for run in self.runs for statement in run.statements)

    @property
    def persisted_evidence_links(self) -> tuple[EvidenceLink, ...]:
        return tuple(
            link
            for statement in self.statements
            for link in statement.evidence_links
        )

    @property
    def receipts(self) -> tuple[CodexRunReceipt, ...]:
        return tuple(run.output.receipt for run in self.runs)

    @property
    def external_tool_calls(self) -> int:
        return sum(receipt.tool_call_count for receipt in self.receipts)

    @property
    def input_sources(self) -> tuple[SyntheticVideo, ...]:
        return tuple(source.video for run in self.runs for source in run.sources)

    @property
    def private_segment_bodies(self) -> tuple[str, ...]:
        return tuple(source.body for run in self.runs for source in run.sources) + (
            "Synthetic post-cutoff private body.",
        )

    @property
    def serialized_api(self) -> str:
        return json.dumps(
            {"week": self.api_week, "month": self.api_month},
            ensure_ascii=False,
            sort_keys=True,
        )

    def subject_name(self, role: str) -> str:
        for candidate, name in self.role_names:
            if candidate == role:
                return name
        raise KeyError(role)

    def run(self, role: str) -> SyntheticRunEvidence:
        for candidate in self.runs:
            if candidate.role == role:
                return candidate
        raise KeyError(role)

    @staticmethod
    def row(view: HeatmapView, subject_name: str, asset: Asset):
        matches = tuple(
            row
            for row in view.rows
            if row.subject_key == subject_name and row.asset is asset
        )
        if len(matches) != 1:
            raise LookupError((subject_name, asset))
        return matches[0]

    def input_segment_ids(self, role: str) -> tuple[int, ...]:
        return tuple(
            source.segment_id
            for source in self.run(role).sources
        )

    def input_video_ids(self, role: str) -> tuple[int, ...]:
        return tuple(source.video.id for source in self.run(role).sources)

    def forecast_source_video_ids(self, cell: HeatmapCell) -> set[int]:
        forecasts = {
            forecast.id: forecast
            for run in self.runs
            for forecast in run.batch.forecasts
        }
        statements = {statement.id: statement for statement in self.statements}
        result: set[int] = set()
        for forecast_id in cell.source_forecast_ids:
            forecast = forecasts[forecast_id]
            result.update(
                statements[statement_id].source_video_id
                for statement_id in forecast.supporting_statement_ids
            )
        return result

    @staticmethod
    def view_semantic_signature(view: HeatmapView) -> tuple[object, ...]:
        return tuple(
            sorted(
                (
                    row.subject_key,
                    row.asset.value,
                    tuple(
                        (
                            cell.period_key,
                            cell.unknown_period,
                            cell.condition_kind.value,
                            cell.condition_texts,
                            cell.primary_direction.value,
                            tuple(item.value for item in cell.directions),
                            cell.view_relation.value,
                            cell.selected_published_at.isoformat(
                                timespec="microseconds"
                            ),
                            cell.selected_forecast_basis.value,
                            cell.mapping_kind.value,
                            cell.confidence.value,
                            cell.evidence_count,
                            len(cell.supporting_statement_ids),
                            len(cell.counterevidence_statement_ids),
                            len(cell.source_forecast_ids),
                        )
                        for cell in row.cells
                    ),
                )
                for row in view.rows
            )
        )

    def api_semantic_signature(
        self, granularity: HeatmapGranularity
    ) -> tuple[object, ...]:
        payload = (
            self.api_week
            if granularity is HeatmapGranularity.WEEK
            else self.api_month
        )
        return tuple(
            sorted(
                (
                    row["subject_key"],
                    row["asset"],
                    tuple(
                        (
                            cell["period_key"],
                            cell["unknown_period"],
                            cell["condition_kind"],
                            tuple(cell["condition_texts"]),
                            cell["primary_direction"],
                            tuple(cell["directions"]),
                            cell["view_relation"],
                            cell["selected_published_at"].replace("Z", "+00:00"),
                            cell["selected_forecast_basis"],
                            cell["mapping_kind"],
                            cell["confidence"],
                            cell["evidence_count"],
                            len(cell["supporting_statement_ids"]),
                            len(cell["counterevidence_statement_ids"]),
                            len(cell["source_forecast_ids"]),
                        )
                        for cell in row["cells"]
                    ),
                )
                for row in payload["rows"]
            )
        )


@dataclass(frozen=True, slots=True)
class SpeakerFixture:
    subject_id: int
    wrong_subject_id: int
    inactive_subject_id: int
    segment_id: int
    video_id: int
    scope_id: int


@dataclass(frozen=True, slots=True)
class RetainedForecastFixture:
    settings: Settings
    run_id: int
    scope_id: int
    segment_id: int
    statement_id: int
    source_body: str
    transcript_hash: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class CrashPromotionFixture:
    old_run: SyntheticRunEvidence
    pending_run: SyntheticRunEvidence
    artifact_hashes: tuple[tuple[str, str], ...]

    @property
    def scope_id(self) -> int:
        return self.pending_run.scope_id

    @property
    def run_id(self) -> int:
        return self.pending_run.prepared.run_id

    @property
    def job_id(self) -> int:
        return self.pending_run.prepared.job_id

    @property
    def projection_batch_id(self) -> int:
        return self.pending_run.batch.id


class DeterministicCodexAnalysisService:
    """In-process test double that crosses the real validated output boundary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._contract = CodexContractService(conn, clock=_synthetic_clock)

    def analyze(
        self,
        run_id: int,
        proposals: tuple[StatementProposal, ...],
    ) -> ValidatedAnalysisOutput:
        envelope = AnalysisEnvelope(
            run_id=run_id,
            batch_key=CODEX_BATCH_UNIT_KEY,
            statements=proposals,
        )
        return self._contract.validate_and_store(
            run_id,
            CODEX_BATCH_UNIT_KEY,
            envelope.model_dump_json(),
            CodexRunReceipt(
                model="gpt-5.6-sol",
                reasoning_effort="max",
                tool_call_count=0,
                boundary_mode="stored_statements_only",
            ),
        )


def _channel_id(index: int) -> str:
    return f"UC{index:022d}"


def _create_subject(
    conn: sqlite3.Connection,
    name: str,
    channel_index: int,
) -> int:
    del channel_index
    return SourceRepository(conn).create_subject(name)


def _ensure_personal_threshold(conn: sqlite3.Connection) -> None:
    speakers = SpeakerRepository(conn)
    try:
        speakers.get_active_threshold_config()
    except LookupError:
        speakers.add_threshold_config(
            SYNTHETIC_THRESHOLD,
            SYNTHETIC_CREATED_AT,
            True,
        )


def _record_personal_assignment(
    conn: sqlite3.Connection,
    subject_id: int,
    segment_id: int,
    assignment_kind: AssignmentKind,
    *,
    assigned_at: datetime = SYNTHETIC_CREATED_AT,
) -> None:
    _ensure_personal_threshold(conn)
    score = {
        AssignmentKind.SUBJECT: 0.95,
        AssignmentKind.INTERVIEWER: 0.05,
        AssignmentKind.HOLD: 0.5,
    }[assignment_kind]
    assignment = SpeakerAssignmentService(
        conn,
        clock=_synthetic_clock,
    ).record_personal(
        PersonalAssignmentCommand(
            segment_id=segment_id,
            subject_id=subject_id,
            raw_match_score=score,
            model_name=SYNTHETIC_THRESHOLD.model_name,
            model_version=SYNTHETIC_THRESHOLD.model_version,
            threshold_config_version=SYNTHETIC_THRESHOLD.version,
            evidence_hash=f"synthetic-assignment-{subject_id}-{segment_id}",
            assigned_at=assigned_at,
        )
    )
    if assignment.assignment_kind is not assignment_kind:
        raise AssertionError("synthetic assignment classification drifted")


def _create_source(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    channel_index: int,
    label: str,
    youtube_video_id: str,
    published_at: datetime,
    body: str,
    assignment_kind: AssignmentKind = AssignmentKind.SUBJECT,
    expires_at: datetime | None = None,
    chunk_input_hash: str | None = None,
) -> SyntheticSource:
    del channel_index, chunk_input_hash
    fixture = create_synthetic_collection_candidate(
        conn,
        presence_state="presence_confirmed",
        assignment_kind="subject",
        subject_id=subject_id,
        youtube_video_id=youtube_video_id,
        published_at=published_at,
        text_body=body,
        transcript_created_at=SYNTHETIC_CREATED_AT,
        expires_at=expires_at,
    )
    _record_personal_assignment(
        conn,
        subject_id,
        fixture.segment_id,
        assignment_kind,
    )
    return SyntheticSource(
        label=label,
        video=SyntheticVideo(fixture.video_id, published_at),
        segment_id=fixture.segment_id,
        body=body,
    )


def _analysis_manifest(
    input_contract: str,
    settings: AnalysisRunSettings,
) -> JobManifest:
    return JobManifest.build(
        JobKind.ANALYSIS_SCOPE,
        (
            ManifestUnit(
                ANALYSIS_INPUT_UNIT_KEY,
                JobStage.ANALYSIS_INPUT_EXTRACTION,
                1,
                input_contract,
                (),
                "analysis-input-freeze-v1",
            ),
            ManifestUnit(
                CODEX_BATCH_UNIT_KEY,
                JobStage.CODEX_ANALYSIS,
                2,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                settings.codex_execution_contract_hash(),
            ),
            ManifestUnit(
                STATEMENT_NORMALIZATION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                3,
                None,
                (CODEX_BATCH_UNIT_KEY,),
                "statement-normalization-v1",
            ),
            ManifestUnit(
                PERIOD_NORMALIZATION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                4,
                None,
                (STATEMENT_NORMALIZATION_UNIT_KEY,),
                "period-normalization-v1",
            ),
            ManifestUnit(
                ASSET_MAPPING_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                5,
                None,
                (
                    STATEMENT_NORMALIZATION_UNIT_KEY,
                    PERIOD_NORMALIZATION_UNIT_KEY,
                ),
                "asset-mapping-v1",
            ),
            ManifestUnit(
                FORECAST_PROJECTION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                6,
                None,
                (ASSET_MAPPING_UNIT_KEY, PERIOD_NORMALIZATION_UNIT_KEY),
                "forecast-projection-v1",
            ),
            ManifestUnit(
                FINAL_PROMOTION_UNIT_KEY,
                JobStage.HEATMAP_UPDATE,
                7,
                None,
                (FORECAST_PROJECTION_UNIT_KEY,),
                "final-promotion-v1",
            ),
        ),
    )


def _proposal(
    spec: SyntheticStatementSpec,
    source: SyntheticSource,
) -> StatementProposal:
    return StatementProposal(
        statement_type=spec.statement_type,
        forecast_basis=spec.forecast_basis,
        condition_kind=spec.condition_kind,
        condition_text=spec.condition_text,
        direction_kind=spec.direction,
        turning_point_kind=spec.turning_point_kind,
        target_expression=spec.target_expression,
        period_expression=spec.period_expression,
        codex_asset_hints=tuple(
            AssetHint(
                expression=spec.target_expression,
                suggested_asset=asset,
                confidence=confidence,
            )
            for asset, confidence in spec.hints
        ),
        evidence=(
            EvidenceProposal(
                segment_id=source.segment_id,
                excerpt=spec.excerpt,
            ),
        ),
    )


def _execute_analysis(
    conn: sqlite3.Connection,
    *,
    role: str,
    subject_id: int,
    cutoff_day: date,
    specs: tuple[SyntheticStatementSpec, ...],
    sources: tuple[SyntheticSource, ...],
    promote: bool = True,
) -> SyntheticRunEvidence:
    settings = AnalysisRunSettings.required()
    analysis = AnalysisRunService(conn, clock=_synthetic_clock)
    input_contract = analysis.preview_input_contract(
        subject_id,
        cutoff_day,
        settings,
    )
    jobs = JobStateService(conn, clock=_synthetic_clock)
    job_id = jobs.create(_analysis_manifest(input_contract, settings))
    jobs.begin_unit(job_id, ANALYSIS_INPUT_UNIT_KEY)
    run = analysis.begin(
        BeginAnalysisRun(
            subject_id=subject_id,
            cutoff_day=cutoff_day,
            job_id=job_id,
            settings=settings,
        )
    )

    jobs.begin_unit(job_id, CODEX_BATCH_UNIT_KEY)
    output = DeterministicCodexAnalysisService(conn).analyze(
        run.id,
        tuple(
            _proposal(spec, source)
            for spec, source in zip(specs, sources, strict=True)
        ),
    )
    jobs.begin_unit(job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    statements = StatementService(
        conn,
        clock=_synthetic_clock,
    ).normalize_and_store(run.id)
    jobs.begin_unit(job_id, PERIOD_NORMALIZATION_UNIT_KEY)
    periods = PeriodService(conn, clock=_synthetic_clock).normalize_run(run.id)
    jobs.begin_unit(job_id, ASSET_MAPPING_UNIT_KEY)
    mappings = AssetMappingService(conn).map_run(run.id)
    projection = ForecastProjectionService(conn, clock=_synthetic_clock)
    jobs.begin_unit(
        job_id,
        FORECAST_PROJECTION_UNIT_KEY,
        projection.effective_review_state_hash(run.id),
    )
    batch = projection.project_run(run.id, ProjectionTrigger.INITIAL)
    if promote:
        jobs.begin_unit(job_id, FINAL_PROMOTION_UNIT_KEY)
        current = CurrentResultService(
            conn,
            clock=_synthetic_clock,
        ).promote_completed_run(
            run.id,
            batch.id,
        )
        scope_id = current.scope_id
    else:
        scope_id = run.scope_id

    return SyntheticRunEvidence(
        role=role,
        prepared=PreparedSyntheticRun(
            subject_id=subject_id,
            run_id=run.id,
            job_id=job_id,
            statement_ids=tuple(statement.id for statement in statements),
            period_ids=tuple(period.id for period in periods),
            mapping_ids=tuple(mapping.id for mapping in mappings),
            video_ids=tuple(source.video.id for source in sources),
        ),
        scope_id=scope_id,
        output=output,
        statements=statements,
        periods=periods,
        mappings=mappings,
        batch=batch,
        sources=sources,
    )


def _create_and_execute_subject(
    conn: sqlite3.Connection,
    *,
    role: str,
    subject_id: int,
    channel_index: int,
    cutoff_day: date,
    specs: tuple[SyntheticStatementSpec, ...],
    promote: bool = True,
) -> SyntheticRunEvidence:
    sources = tuple(
        _create_source(
            conn,
            subject_id=subject_id,
            channel_index=channel_index,
            label=spec.label,
            youtube_video_id=spec.youtube_video_id,
            published_at=spec.published_at,
            body=spec.body(),
        )
        for spec in specs
    )
    return _execute_analysis(
        conn,
        role=role,
        subject_id=subject_id,
        cutoff_day=cutoff_day,
        specs=specs,
        sources=sources,
        promote=promote,
    )


def _e2e_specs(role: str) -> tuple[SyntheticStatementSpec, ...]:
    common = {
        "personal_japan": (
            SyntheticStatementSpec(
                label="japan-market",
                youtube_video_id="synjp000001",
                published_at=datetime(2026, 8, 10, 3, tzinfo=UTC),
                excerpt="Synthetic Japanese equity evidence.",
                direction=DirectionKind.UP,
                target_expression="日本株",
                period_expression="2026年9月",
                hints=(
                    (Asset.NIKKEI_225, Confidence.HIGH),
                    (Asset.TOPIX, Confidence.HIGH),
                ),
            ),
            SyntheticStatementSpec(
                label="unconditional-layer",
                youtube_video_id="synjp000002",
                published_at=datetime(2026, 8, 11, 3, tzinfo=UTC),
                excerpt="Synthetic unconditional evidence.",
                direction=DirectionKind.DOWN,
                target_expression="日経平均",
                period_expression="2026年10月",
            ),
            SyntheticStatementSpec(
                label="conditional-layer",
                youtube_video_id="synjp000003",
                published_at=datetime(2026, 8, 11, 4, tzinfo=UTC),
                excerpt="Synthetic conditional evidence.",
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="Synthetic policy threshold remains satisfied",
                direction=DirectionKind.UP,
                target_expression="日経平均",
                period_expression="2026年10月",
            ),
            SyntheticStatementSpec(
                label="current-analysis",
                youtube_video_id="synjp000004",
                published_at=datetime(2026, 8, 11, 5, tzinfo=UTC),
                excerpt="Synthetic current analysis evidence.",
                statement_type=StatementType.CURRENT_ANALYSIS,
                forecast_basis=None,
                direction=None,
                target_expression="Synthetic current context",
                period_expression=None,
                hints=(),
            ),
            SyntheticStatementSpec(
                label="past-result",
                youtube_video_id="synjp000005",
                published_at=datetime(2026, 8, 11, 6, tzinfo=UTC),
                excerpt="Synthetic past result evidence.",
                statement_type=StatementType.PAST_RESULT_ANALYSIS,
                forecast_basis=None,
                direction=None,
                target_expression="Synthetic historical context",
                period_expression=None,
                hints=(),
            ),
            SyntheticStatementSpec(
                label="general-statement",
                youtube_video_id="synjp000006",
                published_at=datetime(2026, 8, 11, 7, tzinfo=UTC),
                excerpt="Synthetic general statement evidence.",
                statement_type=StatementType.GENERAL_STATEMENT,
                forecast_basis=None,
                direction=None,
                target_expression="Synthetic general context",
                period_expression=None,
                hints=(),
            ),
        ),
        "organization_us": (
            SyntheticStatementSpec(
                label="us-market",
                youtube_video_id="synus000001",
                published_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
                excerpt="Synthetic United States equity evidence.",
                direction=DirectionKind.STRONG_UP,
                target_expression="米国株",
                period_expression="2026年9月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="turning-point",
                youtube_video_id="synus000002",
                published_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
                excerpt="Synthetic turning point evidence.",
                direction=DirectionKind.TURNING_POINT,
                turning_point_kind=TurningPointKind.BOTTOM,
                target_expression="S&P 500",
                period_expression="2026年11月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="flat",
                youtube_video_id="synus000003",
                published_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
                excerpt="Synthetic flat evidence.",
                direction=DirectionKind.FLAT,
                target_expression="S&P 500",
                period_expression="2026年12月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
        ),
        "conflict_history": (
            SyntheticStatementSpec(
                label="same-publication-up",
                youtube_video_id="syncf000001",
                published_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
                excerpt="Synthetic same-time upward evidence.",
                direction=DirectionKind.UP,
                target_expression="S&P 500",
                period_expression="2026年9月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="same-publication-down",
                youtube_video_id="syncf000002",
                published_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
                excerpt="Synthetic same-time downward evidence.",
                direction=DirectionKind.DOWN,
                target_expression="S&P 500",
                period_expression="2026年9月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="older-down",
                youtube_video_id="syncf000003",
                published_at=datetime(2026, 8, 8, 11, tzinfo=UTC),
                excerpt="Synthetic older downward evidence.",
                direction=DirectionKind.DOWN,
                target_expression="S&P 500",
                period_expression="2026年10月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="newer-up",
                youtube_video_id="syncf000004",
                published_at=datetime(2026, 8, 12, 11, tzinfo=UTC),
                excerpt="Synthetic newer upward evidence.",
                direction=DirectionKind.UP,
                target_expression="S&P 500",
                period_expression="2026年10月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="original",
                youtube_video_id="syncf000005",
                published_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
                excerpt="Synthetic original evidence.",
                direction=DirectionKind.UP,
                target_expression="S&P 500",
                period_expression="2027年2月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="repost",
                youtube_video_id="syncf000006",
                published_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
                excerpt="Synthetic repost evidence.",
                direction=DirectionKind.UP,
                target_expression="S&P 500",
                period_expression="2027年2月第1週",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
        ),
        "review_boundary": (
            SyntheticStatementSpec(
                label="low-japan",
                youtube_video_id="synrv000001",
                published_at=datetime(2026, 8, 11, 13, tzinfo=UTC),
                excerpt="Synthetic low-confidence Japanese evidence.",
                direction=DirectionKind.UP,
                target_expression="日本株",
                period_expression="2027年1月",
                hints=(
                    (Asset.NIKKEI_225, Confidence.LOW),
                    (Asset.TOPIX, Confidence.LOW),
                ),
            ),
            SyntheticStatementSpec(
                label="unresolved-us",
                youtube_video_id="synrv000002",
                published_at=datetime(2026, 8, 11, 14, tzinfo=UTC),
                excerpt="Synthetic unresolved generic equity evidence.",
                direction=DirectionKind.DOWN,
                target_expression="株式市場",
                period_expression="2027年2月",
                hints=((Asset.SP500, Confidence.HIGH),),
            ),
            SyntheticStatementSpec(
                label="unknown-period",
                youtube_video_id="synrv000003",
                published_at=datetime(2026, 8, 11, 14, 30, tzinfo=UTC),
                excerpt="Synthetic unknown-period evidence.",
                direction=DirectionKind.UNKNOWN,
                target_expression="日経平均",
                period_expression="当面",
                hints=((Asset.NIKKEI_225, Confidence.HIGH),),
            ),
        ),
    }
    try:
        return common[role]
    except KeyError as cause:
        raise ValueError(f"unknown synthetic subject role: {role}") from cause


class SyntheticLedgerFixture:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        subject_order: tuple[str, ...] = _SUBJECT_ROLES,
    ) -> None:
        if (
            len(subject_order) != len(_SUBJECT_ROLES)
            or set(subject_order) != set(_SUBJECT_ROLES)
        ):
            raise ValueError("subject_order must contain every synthetic role once")
        self.runtime_dir = Path(runtime_dir)
        self.settings = Settings.for_data_dir(self.runtime_dir)
        self.subject_order = subject_order
        self._conn: sqlite3.Connection | None = None
        self._subject_ids: dict[str, int] = {}
        self._completed = False

    def __enter__(self) -> SyntheticLedgerFixture:
        if self._conn is not None:
            raise RuntimeError("synthetic ledger fixture is already open")
        self._conn = open_database(self.settings.database_path)
        apply_migrations(self._conn)
        for role in self.subject_order:
            name, channel_index = _SUBJECT_DEFINITIONS[role]
            self._subject_ids[role] = _create_subject(
                self._conn,
                name,
                channel_index,
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("synthetic ledger fixture is not open")
        return self._conn

    def run_complete_flow(self) -> SyntheticFlowEvidence:
        if self._completed:
            raise RuntimeError("synthetic flow can run only once")
        self._completed = True
        conn = self.connection
        runs_by_role: dict[str, SyntheticRunEvidence] = {}
        later_source: SyntheticSource | None = None

        for role in self.subject_order:
            name, channel_index = _SUBJECT_DEFINITIONS[role]
            subject_id = self._subject_ids[role]
            specs = _e2e_specs(role)
            if role == "personal_japan":
                sources = tuple(
                    _create_source(
                        conn,
                        subject_id=subject_id,
                        channel_index=channel_index,
                        label=spec.label,
                        youtube_video_id=spec.youtube_video_id,
                        published_at=spec.published_at,
                        body=spec.body(),
                    )
                    for spec in specs
                )
                later_source = _create_source(
                    conn,
                    subject_id=subject_id,
                    channel_index=channel_index,
                    label="post-cutoff",
                    youtube_video_id="synjp999999",
                    published_at=datetime(2026, 8, 15, 3, tzinfo=UTC),
                    body="Synthetic post-cutoff private body.",
                )
                runs_by_role[role] = _execute_analysis(
                    conn,
                    role=role,
                    subject_id=subject_id,
                    cutoff_day=SYNTHETIC_CUTOFF,
                    specs=specs,
                    sources=sources,
                )
            else:
                runs_by_role[role] = _create_and_execute_subject(
                    conn,
                    role=role,
                    subject_id=subject_id,
                    channel_index=channel_index,
                    cutoff_day=SYNTHETIC_CUTOFF,
                    specs=specs,
                )

        if later_source is None:
            raise AssertionError("post-cutoff source was not created")

        heatmaps = HeatmapService(conn)
        before_reviews_week = heatmaps.read_cutoff(
            SYNTHETIC_CUTOFF,
            HeatmapGranularity.WEEK,
        )
        review_run = runs_by_role["review_boundary"]
        review_service = ReviewApplicationService(
            conn,
            clock=_synthetic_clock,
        )
        mapping_evidence: list[MappingReviewEvidence] = []
        for mapping in review_run.mappings:
            if not mapping.review_required:
                continue
            if mapping.id is None:
                raise AssertionError("stored mapping is missing its id")
            decision = MappingReviewDecision.APPROVE
            result = review_service.apply_mapping(
                MappingReviewCommand(
                    mapping_id=mapping.id,
                    decision=decision,
                    actor="user",
                    reason=(
                        f"Synthetic explicit {mapping.final_confidence.value} "
                        "mapping approval"
                    ),
                    corrected_asset=None,
                )
            )
            mapping_evidence.append(
                MappingReviewEvidence(
                    decision=decision,
                    before_confidence=mapping.final_confidence,
                    asset=mapping.asset,
                    result=result,
                )
            )

        unknown_statement_id = review_run.statement("unknown-period").id
        unknown_period = next(
            period
            for statement, period in zip(
                review_run.statements,
                review_run.periods,
                strict=True,
            )
            if statement.id == unknown_statement_id
        )
        if unknown_period.id is None:
            raise AssertionError("stored period is missing its id")
        period_decision = PeriodReviewDecision.APPROVE_UNKNOWN
        period_result = review_service.apply_period(
            unknown_period.id,
            period_decision,
            "user",
            "Synthetic explicit unknown-period approval",
        )
        period_evidence = (
            PeriodReviewEvidence(period_decision, period_result),
        )

        week = heatmaps.read_cutoff(
            SYNTHETIC_CUTOFF,
            HeatmapGranularity.WEEK,
        )
        month = heatmaps.read_cutoff(
            SYNTHETIC_CUTOFF,
            HeatmapGranularity.MONTH,
        )
        api_week = self._api_heatmap(HeatmapGranularity.WEEK)
        api_month = self._api_heatmap(HeatmapGranularity.MONTH)
        runs = tuple(runs_by_role[role] for role in _SUBJECT_ROLES)
        conflict = runs_by_role["conflict_history"]
        return SyntheticFlowEvidence(
            runtime_dir=self.runtime_dir,
            cutoff_day=SYNTHETIC_CUTOFF,
            subject_roles=_SUBJECT_ROLES,
            role_names=tuple(
                (role, _SUBJECT_DEFINITIONS[role][0])
                for role in _SUBJECT_ROLES
            ),
            runs=runs,
            before_reviews_week=before_reviews_week,
            week=week,
            month=month,
            api_week=api_week,
            api_month=api_month,
            mapping_review_evidence=tuple(mapping_evidence),
            period_review_evidence=period_evidence,
            later_source=later_source.video,
            later_segment_id=later_source.segment_id,
            original_video_id=conflict.source("original").video.id,
            repost_video_id=conflict.source("repost").video.id,
        )

    def _api_heatmap(
        self,
        granularity: HeatmapGranularity,
    ) -> dict[str, object]:
        # create_app always performs initialization. Suppress only production
        # reference bootstrap so this test database remains exactly synthetic.
        with patch.object(
            dependencies,
            "bootstrap_reference_data",
            lambda conn: None,
        ):
            app = create_app(self.settings)
        with TestClient(app) as client:
            response = client.get(
                "/api/heatmaps",
                params={
                    "cutoff": SYNTHETIC_CUTOFF.isoformat(),
                    "granularity": granularity.value,
                },
            )
        if response.status_code != 200:
            raise AssertionError(
                f"synthetic API read failed safely: {response.status_code}"
            )
        return response.json()


def create_accepted_low_mapping_fixture(
    conn: sqlite3.Connection,
    label: str = "review",
    *,
    additional_active_subjects: int = 0,
) -> tuple[PreparedSyntheticRun, ForecastProjectionBatch, int]:
    if type(additional_active_subjects) is not int or not (
        0 <= additional_active_subjects <= 3
    ):
        raise ValueError("additional_active_subjects must be from zero to three")
    subject_id = _create_subject(
        conn,
        "Synthetic Low Mapping Subject",
        81,
    )
    for ordinal in range(additional_active_subjects):
        _create_subject(
            conn,
            f"Synthetic Empty Heatmap Subject {ordinal + 1}",
            90 + ordinal,
        )
    spec = SyntheticStatementSpec(
        label="low-mapping",
        youtube_video_id="synlow00001",
        published_at=datetime(2026, 8, 11, 3, tzinfo=UTC),
        excerpt=label,
        direction=DirectionKind.UP,
        target_expression="日経平均",
        period_expression="来週",
        hints=((Asset.NIKKEI_225, Confidence.LOW),),
        private_body=(
            f"{label} Synthetic projection evidence. "
            "Private synthetic low-mapping continuation."
        ),
    )
    run = _create_and_execute_subject(
        conn,
        role="accepted-low-mapping",
        subject_id=subject_id,
        channel_index=81,
        cutoff_day=SYNTHETIC_CUTOFF,
        specs=(spec,),
    )
    if run.batch.forecasts:
        raise AssertionError("low mapping entered projection without review")
    return run.prepared, run.batch, run.scope_id


def create_accepted_unknown_period_fixture(
    conn: sqlite3.Connection,
    label: str = "unknown-review",
) -> tuple[PreparedSyntheticRun, ForecastProjectionBatch, int]:
    subject_id = _create_subject(
        conn,
        "Synthetic Unknown Period Subject",
        82,
    )
    spec = SyntheticStatementSpec(
        label="unknown-period",
        youtube_video_id="synunk00001",
        published_at=datetime(2026, 8, 11, 3, tzinfo=UTC),
        excerpt=label,
        direction=DirectionKind.UP,
        target_expression="日経平均",
        period_expression="当面",
        hints=((Asset.NIKKEI_225, Confidence.HIGH),),
        private_body=f"{label} Private synthetic unknown-period continuation.",
    )
    run = _create_and_execute_subject(
        conn,
        role="accepted-unknown-period",
        subject_id=subject_id,
        channel_index=82,
        cutoff_day=SYNTHETIC_CUTOFF,
        specs=(spec,),
    )
    if run.batch.forecasts:
        raise AssertionError("unknown period entered projection without review")
    return run.prepared, run.batch, run.scope_id


def _deactivate_negative_control_subject(
    conn: sqlite3.Connection,
    subject_id: int,
) -> None:
    """Only approved construction SQL: create an inactive rejection control."""

    updated = conn.execute(
        "UPDATE analysis_subjects SET is_active=0 WHERE id=? AND is_active=1",
        (subject_id,),
    )
    if updated.rowcount != 1:
        raise AssertionError("inactive negative-control transition was not exact")


def create_speaker_correction_fixture(
    conn: sqlite3.Connection,
    initial_kind: AssignmentKind = AssignmentKind.SUBJECT,
) -> SpeakerFixture:
    sources = SourceRepository(conn)
    subject_id = _create_subject(
        conn,
        "Synthetic Corrected Person",
        100,
    )
    wrong_subject_id = sources.create_subject(
        "Synthetic Unrelated Person",
    )
    inactive_subject_id = sources.create_subject(
        "Synthetic Inactive Person",
    )
    _deactivate_negative_control_subject(conn, inactive_subject_id)

    source = _create_source(
        conn,
        subject_id=subject_id,
        channel_index=100,
        label="speaker-correction",
        youtube_video_id="synspk00001",
        published_at=datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=UTC),
        body="Private synthetic transcript must never enter audit.",
        assignment_kind=initial_kind,
        expires_at=datetime(2027, 8, 15, 1, 2, 3, 456789, tzinfo=UTC),
        chunk_input_hash="private-input-hash-path-synthetic-sentinel",
    )
    if initial_kind is AssignmentKind.SUBJECT:
        specs = (
            SyntheticStatementSpec(
                label="speaker-correction",
                youtube_video_id="synspk00001",
                published_at=source.video.published_at,
                excerpt="Private synthetic transcript",
                direction=DirectionKind.UP,
                target_expression="日経平均",
                period_expression="来週",
                private_body=source.body,
            ),
        )
        run_sources = (source,)
    else:
        specs = ()
        run_sources = ()
    run = _execute_analysis(
        conn,
        role="speaker-correction",
        subject_id=subject_id,
        cutoff_day=date(2026, 8, 15),
        specs=specs,
        sources=run_sources,
    )
    return SpeakerFixture(
        subject_id=subject_id,
        wrong_subject_id=wrong_subject_id,
        inactive_subject_id=inactive_subject_id,
        segment_id=source.segment_id,
        video_id=source.video.id,
        scope_id=run.scope_id,
    )


def create_retained_forecast_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> RetainedForecastFixture:
    subject_id = _create_subject(
        conn,
        "Synthetic Retention Subject",
        83,
    )
    source_body = (
        "Synthetic retention evidence. Private retained transcript continuation."
    )
    spec = SyntheticStatementSpec(
        label="retained-forecast",
        youtube_video_id="synret00001",
        published_at=datetime(2026, 8, 11, 3, tzinfo=UTC),
        excerpt="Synthetic retention evidence.",
        direction=DirectionKind.UP,
        target_expression="日経平均",
        period_expression="来週",
        private_body=source_body,
    )
    run = _create_and_execute_subject(
        conn,
        role="retained-forecast",
        subject_id=subject_id,
        channel_index=83,
        cutoff_day=SYNTHETIC_CUTOFF,
        specs=(spec,),
    )
    source = run.source("retained-forecast")
    segment = SpeakerRepository(conn).get_segment(source.segment_id)
    snapshot = AnalysisRepository(conn).get_snapshot(run.prepared.run_id)
    if segment.text_body is None:
        raise AssertionError("retained fixture unexpectedly lacks source text")
    return RetainedForecastFixture(
        settings=Settings.for_data_dir(Path(tmp_path) / "runtime"),
        run_id=run.prepared.run_id,
        scope_id=run.scope_id,
        segment_id=segment.id,
        statement_id=run.prepared.statement_ids[0],
        source_body=segment.text_body,
        transcript_hash=segment.text_sha256,
        input_hash=snapshot.input_sha256,
    )


def create_crash_promotion_fixture(
    conn: sqlite3.Connection,
) -> CrashPromotionFixture:
    subject_id = _create_subject(
        conn,
        "Synthetic Crash Recovery Subject",
        84,
    )
    old_run = _create_and_execute_subject(
        conn,
        role="crash-old-current",
        subject_id=subject_id,
        channel_index=84,
        cutoff_day=SYNTHETIC_CUTOFF,
        specs=(
            SyntheticStatementSpec(
                label="old-current",
                youtube_video_id="syncrs00001",
                published_at=datetime(2026, 8, 9, 3, tzinfo=UTC),
                excerpt="Synthetic old current evidence.",
                direction=DirectionKind.UP,
                target_expression="日経平均",
                period_expression="2026年9月",
            ),
        ),
    )
    pending_run = _create_and_execute_subject(
        conn,
        role="crash-pending-current",
        subject_id=subject_id,
        channel_index=84,
        cutoff_day=SYNTHETIC_CUTOFF,
        specs=(
            SyntheticStatementSpec(
                label="pending-current",
                youtube_video_id="syncrs00002",
                published_at=datetime(2026, 8, 12, 3, tzinfo=UTC),
                excerpt="Synthetic pending current evidence.",
                direction=DirectionKind.DOWN,
                target_expression="日経平均",
                period_expression="2026年10月",
            ),
        ),
        promote=False,
    )
    if old_run.scope_id != pending_run.scope_id:
        raise AssertionError("crash fixture did not reuse the cutoff scope")
    jobs = JobStateService(conn, clock=_synthetic_clock)
    artifact_hashes: list[tuple[str, str]] = []
    for unit_key in (
        ANALYSIS_INPUT_UNIT_KEY,
        CODEX_BATCH_UNIT_KEY,
        STATEMENT_NORMALIZATION_UNIT_KEY,
        PERIOD_NORMALIZATION_UNIT_KEY,
        ASSET_MAPPING_UNIT_KEY,
        FORECAST_PROJECTION_UNIT_KEY,
    ):
        output_hash = jobs.unit(pending_run.prepared.job_id, unit_key).output_hash
        if output_hash is None:
            raise AssertionError(f"synthetic upstream unit lacks output: {unit_key}")
        artifact_hashes.append((unit_key, output_hash))
    return CrashPromotionFixture(
        old_run=old_run,
        pending_run=pending_run,
        artifact_hashes=tuple(artifact_hashes),
    )
