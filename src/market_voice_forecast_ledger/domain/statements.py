from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from market_voice_forecast_ledger.domain.enums import (
    ConditionKind,
    DirectionKind,
    ForecastBasis,
    StatementType,
    TurningPointKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError

if TYPE_CHECKING:
    from market_voice_forecast_ledger.services.codex_contract import (
        StatementProposal,
    )


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    statement_id: int
    ordinal: int
    run_segment_id: int
    segment_id: int
    excerpt: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class NormalizedStatement:
    id: int
    run_id: int
    ordinal: int
    batch_ordinal: int
    proposal_ordinal: int
    source_video_id: int
    statement_type: StatementType
    forecast_basis: ForecastBasis | None
    condition_kind: ConditionKind
    condition_text: str | None
    direction_kind: DirectionKind | None
    turning_point_kind: TurningPointKind | None
    target_expression: str
    period_expression: str | None
    heatmap_candidate: bool
    evidence_links: tuple[EvidenceLink, ...]
    created_at: datetime

    @property
    def is_heatmap_candidate(self) -> bool:
        return self.heatmap_candidate


def validate_statement(proposal: "StatementProposal") -> None:
    if (
        proposal.statement_type is StatementType.FUTURE_FORECAST
        and proposal.forecast_basis is None
    ):
        raise DomainError(
            "FORECAST_BASIS_REQUIRED", "future forecast requires a basis"
        )
    if (
        proposal.statement_type is StatementType.FUTURE_FORECAST
        and proposal.direction_kind is None
    ):
        raise DomainError(
            "FORECAST_DIRECTION_REQUIRED",
            "future forecast requires a direction",
        )
    if (
        proposal.statement_type is not StatementType.FUTURE_FORECAST
        and proposal.forecast_basis is not None
    ):
        raise DomainError(
            "FORECAST_BASIS_NOT_ALLOWED",
            "non-forecast cannot have a forecast basis",
        )
    if (
        proposal.condition_kind is ConditionKind.CONDITIONAL
        and not proposal.condition_text
    ):
        raise DomainError(
            "CONDITION_TEXT_REQUIRED", "conditional forecast requires text"
        )
    if (
        proposal.direction_kind is DirectionKind.TURNING_POINT
        and proposal.turning_point_kind is None
    ):
        raise DomainError(
            "TURNING_POINT_KIND_REQUIRED", "turning point subtype required"
        )
