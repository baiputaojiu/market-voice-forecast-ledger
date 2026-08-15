from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum

from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    MappingKind,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.mappings import CONFIDENCE_ORDER


class ProjectionTrigger(StrEnum):
    INITIAL = "initial"
    MAPPING_REVIEW = "mapping_review"
    PERIOD_REVIEW = "period_review"


@dataclass(frozen=True, slots=True)
class ForecastCandidate:
    statement_id: int
    youtube_video_id: str
    published_at: datetime
    direction: DirectionKind
    forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class PublicationCandidate:
    published_at: datetime
    direction: DirectionKind
    forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence
    inherited_view_relation: ViewRelation
    evidence_statement_ids: tuple[int, ...]
    inherited_counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]
    stable_order_key: str


@dataclass(frozen=True, slots=True)
class ResolvedPublicationGroup:
    primary_direction: DirectionKind
    directions: tuple[DirectionKind, ...]
    view_relation: ViewRelation
    selected_published_at: datetime
    selected_forecast_basis: ForecastBasis
    period_specificity: int
    mapping_kind: MappingKind
    confidence: Confidence
    supporting_statement_ids: tuple[int, ...]
    counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...]
    evidence_count: int
    stable_selection_key: str


@dataclass(frozen=True, slots=True)
class ForecastDirectionEvidence:
    statement_id: int
    relation_kind: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class ProjectedForecast:
    id: int | None
    projection_batch_id: int
    run_id: int
    subject_id: int
    asset: Asset
    mapping_kind: MappingKind
    period_start: date | None
    period_end: date | None
    unknown_period: bool
    condition_kind: ConditionKind
    condition_text: str | None
    view_relation: ViewRelation
    primary_direction: DirectionKind
    directions: tuple[DirectionKind, ...]
    confidence: Confidence
    evidence_count: int
    selected_published_at: datetime
    selected_forecast_basis: ForecastBasis
    period_specificity: int
    stable_selection_key: str
    heatmap_eligible: bool
    exclusion_reason: str | None
    supporting_statement_ids: tuple[int, ...]
    counterevidence_statement_ids: tuple[int, ...]
    source_forecast_ids: tuple[int, ...] = ()

    @property
    def resolved_group(self) -> ResolvedPublicationGroup:
        return ResolvedPublicationGroup(
            primary_direction=self.primary_direction,
            directions=self.directions,
            view_relation=self.view_relation,
            selected_published_at=self.selected_published_at,
            selected_forecast_basis=self.selected_forecast_basis,
            period_specificity=self.period_specificity,
            mapping_kind=self.mapping_kind,
            confidence=self.confidence,
            supporting_statement_ids=self.supporting_statement_ids,
            counterevidence_statement_ids=self.counterevidence_statement_ids,
            source_forecast_ids=self.source_forecast_ids,
            evidence_count=self.evidence_count,
            stable_selection_key=self.stable_selection_key,
        )


@dataclass(frozen=True, slots=True)
class ForecastProjectionBatch:
    id: int
    run_id: int
    trigger_kind: ProjectionTrigger
    latest_mapping_review_id: int | None
    latest_period_review_id: int | None
    created_at: datetime
    forecasts: tuple[ProjectedForecast, ...]


_DIRECTION_ORDER = {
    direction: ordinal for ordinal, direction in enumerate(DirectionKind)
}
_UPWARD = frozenset({DirectionKind.UP, DirectionKind.STRONG_UP})
_DOWNWARD = frozenset({DirectionKind.DOWN, DirectionKind.STRONG_DOWN})


def select_current(
    candidates: Sequence[ForecastCandidate],
) -> ResolvedPublicationGroup:
    return resolve_publication_groups(
        tuple(
            PublicationCandidate(
                published_at=candidate.published_at,
                direction=candidate.direction,
                forecast_basis=candidate.forecast_basis,
                period_specificity=candidate.period_specificity,
                mapping_kind=candidate.mapping_kind,
                confidence=candidate.confidence,
                inherited_view_relation=ViewRelation.CURRENT,
                evidence_statement_ids=(candidate.statement_id,),
                inherited_counterevidence_statement_ids=(),
                source_forecast_ids=(),
                stable_order_key=(
                    f"{candidate.youtube_video_id}:{candidate.statement_id:020d}"
                ),
            )
            for candidate in candidates
        )
    )


def resolve_publication_groups(
    candidates: Sequence[PublicationCandidate],
) -> ResolvedPublicationGroup:
    if not candidates:
        raise DomainError(
            "FORECAST_CANDIDATES_REQUIRED",
            "forecast selection requires at least one candidate",
        )

    groups: dict[datetime, list[PublicationCandidate]] = {}
    for candidate in candidates:
        if (
            candidate.published_at.tzinfo is None
            or candidate.published_at.utcoffset() is None
        ):
            raise DomainError(
                "FORECAST_PUBLICATION_TIME_INVALID",
                "forecast publication time must be timezone-aware",
            )
        normalized = candidate.published_at.astimezone(timezone.utc)
        groups.setdefault(normalized, []).append(candidate)

    newest_at = max(groups)
    newest = tuple(groups[newest_at])
    representative = min(newest, key=_representative_rank)
    newest_directions = tuple(
        sorted(
            {candidate.direction for candidate in newest},
            key=_DIRECTION_ORDER.__getitem__,
        )
    )
    newest_families = {
        family
        for candidate in newest
        if (family := _direction_family(candidate.direction)) is not None
    }
    selected_direction_set = set(newest_directions)
    selected_family_set = newest_families
    disagreement = newest_families == {"up", "down"}

    if disagreement:
        view_relation = ViewRelation.DISAGREEMENT
    else:
        changed = any(
            candidate.inherited_view_relation is ViewRelation.CHANGED
            and (
                candidate.direction in selected_direction_set
                or (
                    (family := _direction_family(candidate.direction))
                    is not None
                    and family in selected_family_set
                )
            )
            for group in groups.values()
            for candidate in group
        )
        if len(newest_families) == 1:
            newest_family = next(iter(newest_families))
            changed = changed or any(
                published_at < newest_at
                and any(
                    (family := _direction_family(candidate.direction))
                    is not None
                    and family != newest_family
                    for candidate in group
                )
                for published_at, group in groups.items()
            )
        view_relation = (
            ViewRelation.CHANGED if changed else ViewRelation.CURRENT
        )

    supporting: set[int] = set()
    counterevidence: set[int] = set()
    source_forecast_ids: set[int] = set()
    for group in groups.values():
        for candidate in group:
            family = _direction_family(candidate.direction)
            supports = (
                candidate.direction in selected_direction_set
                or (family is not None and family in selected_family_set)
            )
            target = supporting if supports else counterevidence
            target.update(candidate.evidence_statement_ids)
            counterevidence.update(
                candidate.inherited_counterevidence_statement_ids
            )
            source_forecast_ids.update(candidate.source_forecast_ids)
    counterevidence.difference_update(supporting)

    current_mapping_kind = (
        MappingKind.INFERRED
        if any(
            candidate.mapping_kind is MappingKind.INFERRED
            for candidate in newest
        )
        else MappingKind.DIRECT
    )
    current_confidence = min(
        (candidate.confidence for candidate in newest),
        key=CONFIDENCE_ORDER.__getitem__,
    )
    ordered_supporting = tuple(sorted(supporting))
    return ResolvedPublicationGroup(
        primary_direction=representative.direction,
        directions=newest_directions,
        view_relation=view_relation,
        selected_published_at=newest_at,
        selected_forecast_basis=representative.forecast_basis,
        period_specificity=representative.period_specificity,
        mapping_kind=current_mapping_kind,
        confidence=current_confidence,
        supporting_statement_ids=ordered_supporting,
        counterevidence_statement_ids=tuple(sorted(counterevidence)),
        source_forecast_ids=tuple(sorted(source_forecast_ids)),
        evidence_count=len(ordered_supporting),
        stable_selection_key=representative.stable_order_key,
    )


def _representative_rank(
    candidate: PublicationCandidate,
) -> tuple[int, int, str]:
    return (
        0 if candidate.forecast_basis is ForecastBasis.DIRECT else 1,
        -candidate.period_specificity,
        candidate.stable_order_key,
    )


def _direction_family(direction: DirectionKind) -> str | None:
    if direction in _UPWARD:
        return "up"
    if direction in _DOWNWARD:
        return "down"
    return None
