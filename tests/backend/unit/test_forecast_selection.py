from datetime import datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.domain.enums import (
    Confidence,
    DirectionKind,
    ForecastBasis,
    MappingKind,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.forecasts import (
    ForecastCandidate,
    PublicationCandidate,
    resolve_publication_groups,
    select_current,
)


UTC = timezone.utc
JST = timezone(timedelta(hours=9))
OLDER = datetime(2026, 8, 10, 3, tzinfo=UTC)
NEWER = datetime(2026, 8, 11, 3, tzinfo=UTC)


def _publication(
    statement_id: int,
    direction: DirectionKind,
    *,
    published_at: datetime = NEWER,
    basis: ForecastBasis = ForecastBasis.DIRECT,
    specificity: int = 3,
    mapping_kind: MappingKind = MappingKind.DIRECT,
    confidence: Confidence = Confidence.HIGH,
    inherited: ViewRelation = ViewRelation.CURRENT,
    counterevidence: tuple[int, ...] = (),
    source_forecast_ids: tuple[int, ...] = (),
    video_id: str | None = None,
) -> PublicationCandidate:
    video_id = video_id or f"video-{statement_id:03d}"
    return PublicationCandidate(
        published_at=published_at,
        direction=direction,
        forecast_basis=basis,
        period_specificity=specificity,
        mapping_kind=mapping_kind,
        confidence=confidence,
        inherited_view_relation=inherited,
        evidence_statement_ids=(statement_id,),
        inherited_counterevidence_statement_ids=counterevidence,
        source_forecast_ids=source_forecast_ids,
        stable_order_key=f"{video_id}:{statement_id:020d}",
    )


def _forecast(
    statement_id: int,
    direction: DirectionKind,
    *,
    published_at: datetime = NEWER,
    basis: ForecastBasis = ForecastBasis.DIRECT,
    specificity: int = 3,
    mapping_kind: MappingKind = MappingKind.DIRECT,
    confidence: Confidence = Confidence.HIGH,
    video_id: str | None = None,
) -> ForecastCandidate:
    return ForecastCandidate(
        statement_id=statement_id,
        youtube_video_id=video_id or f"video-{statement_id:03d}",
        published_at=published_at,
        direction=direction,
        forecast_basis=basis,
        period_specificity=specificity,
        mapping_kind=mapping_kind,
        confidence=confidence,
    )


@pytest.mark.parametrize(
    ("upward", "downward"),
    [
        (DirectionKind.UP, DirectionKind.DOWN),
        (DirectionKind.UP, DirectionKind.STRONG_DOWN),
        (DirectionKind.STRONG_UP, DirectionKind.DOWN),
        (DirectionKind.STRONG_UP, DirectionKind.STRONG_DOWN),
    ],
)
def test_same_publication_in_all_opposite_family_combinations_is_disagreement(
    upward: DirectionKind, downward: DirectionKind
) -> None:
    result = resolve_publication_groups(
        (
            _publication(1, upward, basis=ForecastBasis.INFERRED),
            _publication(
                2,
                downward,
                basis=ForecastBasis.DIRECT,
                specificity=0,
                mapping_kind=MappingKind.INFERRED,
                confidence=Confidence.LOW,
            ),
        )
    )

    assert result.view_relation is ViewRelation.DISAGREEMENT
    assert set(result.directions) == {upward, downward}
    assert result.primary_direction is downward
    assert result.supporting_statement_ids == (1, 2)
    assert result.evidence_count == 2
    assert result.mapping_kind is MappingKind.INFERRED
    assert result.confidence is Confidence.LOW


def test_equal_instants_with_different_offsets_share_one_publication_group() -> None:
    result = resolve_publication_groups(
        (
            _publication(
                1,
                DirectionKind.UP,
                published_at=datetime(2026, 8, 11, 12, tzinfo=JST),
            ),
            _publication(2, DirectionKind.DOWN, published_at=NEWER),
        )
    )

    assert result.selected_published_at == NEWER
    assert result.view_relation is ViewRelation.DISAGREEMENT


def test_different_exact_instants_with_opposite_families_are_a_change() -> None:
    result = select_current(
        (
            _forecast(1, DirectionKind.DOWN, published_at=OLDER),
            _forecast(2, DirectionKind.UP, published_at=NEWER),
        )
    )

    assert result.primary_direction is DirectionKind.UP
    assert result.view_relation is ViewRelation.CHANGED
    assert result.supporting_statement_ids == (2,)
    assert result.counterevidence_statement_ids == (1,)


def test_one_microsecond_difference_is_not_one_publication_group() -> None:
    result = resolve_publication_groups(
        (
            _publication(1, DirectionKind.DOWN, published_at=NEWER),
            _publication(
                2,
                DirectionKind.UP,
                published_at=NEWER + timedelta(microseconds=1),
            ),
        )
    )

    assert result.view_relation is ViewRelation.CHANGED
    assert result.selected_published_at == NEWER + timedelta(microseconds=1)


def test_later_same_direction_repost_keeps_change_and_increases_evidence() -> None:
    result = select_current(
        (
            _forecast(1, DirectionKind.DOWN, published_at=OLDER),
            _forecast(
                2,
                DirectionKind.UP,
                published_at=OLDER + timedelta(hours=1),
            ),
            _forecast(3, DirectionKind.STRONG_UP, published_at=NEWER),
        )
    )

    assert result.primary_direction is DirectionKind.STRONG_UP
    assert result.view_relation is ViewRelation.CHANGED
    assert result.supporting_statement_ids == (2, 3)
    assert result.counterevidence_statement_ids == (1,)
    assert result.evidence_count == 2


@pytest.mark.parametrize(
    "neutral",
    [
        DirectionKind.FLAT,
        DirectionKind.TURNING_POINT,
        DirectionKind.UNKNOWN,
    ],
)
def test_neutral_states_remain_distinct_and_do_not_become_change(
    neutral: DirectionKind,
) -> None:
    result = resolve_publication_groups(
        (
            _publication(1, DirectionKind.DOWN, published_at=OLDER),
            _publication(2, neutral, published_at=NEWER),
        )
    )

    assert result.primary_direction is neutral
    assert result.directions == (neutral,)
    assert result.view_relation is ViewRelation.CURRENT
    assert result.supporting_statement_ids == (2,)
    assert result.counterevidence_statement_ids == (1,)


def test_representative_rank_is_basis_then_specificity_then_stable_key() -> None:
    result = resolve_publication_groups(
        (
            _publication(
                1,
                DirectionKind.STRONG_UP,
                basis=ForecastBasis.INFERRED,
                specificity=3,
                video_id="a",
            ),
            _publication(
                2,
                DirectionKind.UP,
                basis=ForecastBasis.DIRECT,
                specificity=1,
                video_id="z",
            ),
            _publication(
                3,
                DirectionKind.STRONG_UP,
                basis=ForecastBasis.DIRECT,
                specificity=3,
                video_id="b",
            ),
            _publication(
                4,
                DirectionKind.UP,
                basis=ForecastBasis.DIRECT,
                specificity=3,
                video_id="a",
            ),
        )
    )

    assert result.primary_direction is DirectionKind.UP
    assert result.selected_forecast_basis is ForecastBasis.DIRECT
    assert result.period_specificity == 3
    assert result.stable_selection_key == f"a:{4:020d}"
    assert result.evidence_count == 4
    assert result.view_relation is ViewRelation.CURRENT
    assert set(result.directions) == {
        DirectionKind.UP,
        DirectionKind.STRONG_UP,
    }


def test_newest_disagreement_takes_precedence_over_older_change() -> None:
    result = resolve_publication_groups(
        (
            _publication(1, DirectionKind.DOWN, published_at=OLDER),
            _publication(2, DirectionKind.UP, published_at=NEWER),
            _publication(3, DirectionKind.DOWN, published_at=NEWER),
        )
    )

    assert result.view_relation is ViewRelation.DISAGREEMENT
    assert set(result.directions) == {DirectionKind.UP, DirectionKind.DOWN}
    assert result.supporting_statement_ids == (1, 2, 3)


def test_evidence_count_is_distinct_even_when_adapters_repeat_one_statement() -> None:
    duplicated = PublicationCandidate(
        published_at=NEWER,
        direction=DirectionKind.UP,
        forecast_basis=ForecastBasis.DIRECT,
        period_specificity=3,
        mapping_kind=MappingKind.DIRECT,
        confidence=Confidence.HIGH,
        inherited_view_relation=ViewRelation.CURRENT,
        evidence_statement_ids=(4, 4, 5),
        inherited_counterevidence_statement_ids=(),
        source_forecast_ids=(9,),
        stable_order_key="duplicate:00000000000000000004",
    )

    result = resolve_publication_groups((duplicated,))

    assert result.supporting_statement_ids == (4, 5)
    assert result.evidence_count == 2


def test_newest_group_uses_conservative_mapping_and_confidence() -> None:
    result = resolve_publication_groups(
        (
            _publication(1, DirectionKind.UP, published_at=OLDER),
            _publication(
                2,
                DirectionKind.UP,
                mapping_kind=MappingKind.DIRECT,
                confidence=Confidence.HIGH,
            ),
            _publication(
                3,
                DirectionKind.STRONG_UP,
                mapping_kind=MappingKind.INFERRED,
                confidence=Confidence.LOW,
            ),
        )
    )

    assert result.mapping_kind is MappingKind.INFERRED
    assert result.confidence is Confidence.LOW
    assert result.evidence_count == 3


def test_inherited_change_counterevidence_and_sources_survive_reprojection() -> None:
    result = resolve_publication_groups(
        (
            _publication(
                10,
                DirectionKind.UP,
                inherited=ViewRelation.CHANGED,
                counterevidence=(2, 3),
                source_forecast_ids=(9, 7),
            ),
            _publication(
                11,
                DirectionKind.UP,
                source_forecast_ids=(8,),
            ),
        )
    )

    assert result.view_relation is ViewRelation.CHANGED
    assert result.supporting_statement_ids == (10, 11)
    assert result.counterevidence_statement_ids == (2, 3)
    assert result.source_forecast_ids == (7, 8, 9)
    assert result.evidence_count == 2


def test_older_supporting_inherited_change_survives_later_same_family() -> None:
    result = resolve_publication_groups(
        (
            _publication(
                10,
                DirectionKind.UP,
                published_at=OLDER,
                inherited=ViewRelation.CHANGED,
            ),
            _publication(11, DirectionKind.STRONG_UP, published_at=NEWER),
        )
    )

    assert result.view_relation is ViewRelation.CHANGED
    assert result.supporting_statement_ids == (10, 11)
    assert result.counterevidence_statement_ids == ()


def test_older_nonmatching_neutral_inherited_change_does_not_propagate() -> None:
    result = resolve_publication_groups(
        (
            _publication(
                10,
                DirectionKind.FLAT,
                published_at=OLDER,
                inherited=ViewRelation.CHANGED,
            ),
            _publication(11, DirectionKind.UP, published_at=NEWER),
        )
    )

    assert result.view_relation is ViewRelation.CURRENT
    assert result.supporting_statement_ids == (11,)
    assert result.counterevidence_statement_ids == (10,)
