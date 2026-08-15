from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    ForecastBasis,
    HeatmapGranularity,
    MappingKind,
    PeriodReviewDecision,
    ViewRelation,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.periods import PeriodReviewService
from tests.backend.integration.test_forecast_projection import (
    NEWER,
    OLDER,
    StatementSpec,
    _prepare_upstream,
    _project,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _cache(db, specs, *, approve_unknown=()):
    prepared = _prepare_upstream(db, tuple(specs))
    for period_index in approve_unknown:
        PeriodReviewService(db).review(
            prepared.period_ids[period_index],
            PeriodReviewDecision.APPROVE_UNKNOWN,
            "user",
            "Synthetic unknown-column approval",
        )
    batch = _project(db, prepared)
    with transaction(db):
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )
    scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id
    HeatmapService(db).rebuild_scope(scope_id)
    return prepared, batch, scope_id


def _cells(db, scope_id, granularity, asset=Asset.NIKKEI_225):
    view = HeatmapService(db).read_scope(scope_id, granularity)
    return next(row.cells for row in view.rows if row.asset is asset)


def test_approved_unknown_is_independent_in_both_granularities(db):
    _, batch, scope_id = _cache(
        db,
        (
            StatementSpec(
                "unknown",
                NEWER,
                period_expression="当面",
            ),
        ),
        approve_unknown=(0,),
    )

    assert batch.forecasts[0].unknown_period is True
    for granularity in HeatmapGranularity:
        cells = _cells(db, scope_id, granularity)
        assert len(cells) == 1
        assert cells[0].period_key == "unknown"
        assert cells[0].unknown_period is True
        assert cells[0].slot_start is None
        assert cells[0].slot_end is None


def test_unapproved_or_rejected_unknown_produces_no_unknown_cell(db):
    prepared = _prepare_upstream(
        db,
        (StatementSpec("unknown-rejected", NEWER, period_expression="当面"),),
    )
    PeriodReviewService(db).review(
        prepared.period_ids[0],
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic unknown rejection",
    )
    batch = _project(db, prepared)
    with transaction(db):
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )
    scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id

    assert batch.forecasts == ()
    assert HeatmapService(db).rebuild_scope(scope_id) == 0
    assert _cells(db, scope_id, HeatmapGranularity.WEEK) == ()


def test_saved_disagreement_expands_losslessly_into_publication_candidates(db):
    _, batch, scope_id = _cache(
        db,
        (
            StatementSpec("same-up", NEWER, DirectionKind.UP),
            StatementSpec("same-down", NEWER, DirectionKind.DOWN),
        ),
    )

    saved = batch.forecasts[0]
    cell = _cells(db, scope_id, HeatmapGranularity.WEEK)[0]

    assert saved.view_relation is ViewRelation.DISAGREEMENT
    assert saved.directions == (DirectionKind.UP, DirectionKind.DOWN)
    assert cell.view_relation is ViewRelation.DISAGREEMENT
    assert cell.primary_direction is saved.primary_direction
    assert cell.directions == (DirectionKind.UP, DirectionKind.DOWN)
    assert cell.source_forecast_ids == (saved.id,)
    assert cell.evidence_count == 2


def test_older_disagreement_keeps_direction_specific_evidence_in_later_slot(db):
    prepared, _, scope_id = _cache(
        db,
        (
            StatementSpec(
                "older-month-up",
                OLDER,
                DirectionKind.UP,
                period_expression="2026年9月",
            ),
            StatementSpec(
                "older-month-down",
                OLDER,
                DirectionKind.DOWN,
                period_expression="2026年9月",
            ),
            StatementSpec(
                "newer-first-week-up",
                NEWER,
                DirectionKind.UP,
                period_expression="2026年9月第1週",
            ),
        ),
    )

    september = next(
        cell
        for cell in _cells(db, scope_id, HeatmapGranularity.MONTH)
        if cell.period_key == "2026-09"
    )

    assert september.primary_direction is DirectionKind.UP
    assert september.view_relation is ViewRelation.CHANGED
    assert september.supporting_statement_ids == (
        prepared.statement_ids[0],
        prepared.statement_ids[2],
    )
    assert september.counterevidence_statement_ids == (
        prepared.statement_ids[1],
    )
    assert september.evidence_count == 2


def test_rebuild_fails_closed_when_saved_support_has_no_matching_direction(db):
    prepared, _, scope_id = _cache(
        db,
        (StatementSpec("mismatched-support", NEWER, DirectionKind.UP),),
    )
    db.execute("DROP TRIGGER analysis_statements_no_update")
    db.execute(
        "UPDATE analysis_statements SET direction_kind='down' WHERE id=?",
        (prepared.statement_ids[0],),
    )

    with pytest.raises(DomainError) as error:
        HeatmapService(db).rebuild_scope(scope_id)

    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_later_reversal_and_inherited_change_survive_slot_resolution(db):
    _, batch, scope_id = _cache(
        db,
        (
            StatementSpec("older-down", OLDER, DirectionKind.DOWN),
            StatementSpec("newer-up", NEWER, DirectionKind.UP),
        ),
    )

    assert batch.forecasts[0].view_relation is ViewRelation.CHANGED
    cell = _cells(db, scope_id, HeatmapGranularity.WEEK)[0]
    assert cell.view_relation is ViewRelation.CHANGED
    assert cell.primary_direction is DirectionKind.UP
    assert cell.directions == (DirectionKind.UP,)
    assert cell.evidence_count == 1
    assert cell.counterevidence_statement_ids == (
        batch.forecasts[0].counterevidence_statement_ids
    )


@pytest.mark.parametrize(
    "direction",
    (
        DirectionKind.FLAT,
        DirectionKind.TURNING_POINT,
        DirectionKind.UNKNOWN,
    ),
)
def test_non_directional_forecast_kinds_remain_distinct(db, direction):
    _, _, scope_id = _cache(
        db,
        (StatementSpec(f"kind-{direction.value}", NEWER, direction),),
    )

    cell = _cells(db, scope_id, HeatmapGranularity.WEEK)[0]
    assert cell.primary_direction is direction
    assert cell.directions == (direction,)
    assert cell.view_relation is ViewRelation.CURRENT


def test_overlapping_saved_periods_resolve_basis_specificity_and_confidence(db):
    same_time = datetime(2026, 8, 11, 3, tzinfo=timezone.utc)
    _, batch, scope_id = _cache(
        db,
        (
            StatementSpec(
                "month-direct",
                same_time,
                DirectionKind.UP,
                forecast_basis=ForecastBasis.DIRECT,
                period_expression="2026年9月",
                confidence=Confidence.HIGH,
            ),
            StatementSpec(
                "week-inferred",
                same_time,
                DirectionKind.STRONG_UP,
                forecast_basis=ForecastBasis.INFERRED,
                period_expression="2026年9月第1週",
                confidence=Confidence.MEDIUM,
            ),
        ),
    )
    month = next(
        cell
        for cell in _cells(db, scope_id, HeatmapGranularity.MONTH)
        if cell.period_key == "2026-09"
    )

    assert len(batch.forecasts) == 2
    assert month.primary_direction is DirectionKind.UP
    assert month.selected_forecast_basis is ForecastBasis.DIRECT
    assert month.period_specificity == 2
    assert month.confidence is Confidence.MEDIUM
    assert month.source_forecast_ids == tuple(
        sorted(forecast.id for forecast in batch.forecasts)
    )
    assert month.evidence_count == 2


def test_newest_group_uses_conservative_mapping_kind(db):
    _, _, scope_id = _cache(
        db,
        (
            StatementSpec(
                "direct-target",
                NEWER,
                period_expression="2026年9月",
                target_expression="日経平均",
            ),
            StatementSpec(
                "inferred-target",
                NEWER,
                period_expression="2026年9月第1週",
                target_expression="日本株",
            ),
        ),
    )
    month = next(
        cell
        for cell in _cells(db, scope_id, HeatmapGranularity.MONTH)
        if cell.period_key == "2026-09"
    )

    assert month.mapping_kind is MappingKind.INFERRED


def test_conditional_texts_are_canonical_and_never_mix_with_unconditional(db):
    _, _, scope_id = _cache(
        db,
        (
            StatementSpec(
                "condition-b",
                NEWER,
                period_expression="2026年9月",
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="Condition B",
            ),
            StatementSpec(
                "condition-a",
                NEWER,
                period_expression="2026年9月第1週",
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="Condition A",
            ),
            StatementSpec(
                "unconditional",
                NEWER,
                period_expression="2026年9月",
                condition_kind=ConditionKind.UNCONDITIONAL,
            ),
        ),
    )
    september = tuple(
        cell
        for cell in _cells(db, scope_id, HeatmapGranularity.MONTH)
        if cell.period_key == "2026-09"
    )

    assert len(september) == 2
    conditional = next(
        cell for cell in september if cell.condition_kind is ConditionKind.CONDITIONAL
    )
    unconditional = next(
        cell for cell in september if cell.condition_kind is ConditionKind.UNCONDITIONAL
    )
    assert conditional.condition_texts == ("Condition A", "Condition B")
    assert unconditional.condition_texts == ()
    assert set(conditional.source_forecast_ids).isdisjoint(
        unconditional.source_forecast_ids
    )


def test_repost_source_links_and_distinct_evidence_are_retained(db):
    _, batch, scope_id = _cache(
        db,
        (
            StatementSpec(
                "repost-a",
                NEWER,
                period_expression="2026年9月",
            ),
            StatementSpec(
                "repost-b",
                NEWER,
                period_expression="2026年9月第1週",
            ),
        ),
    )
    cell = next(
        item
        for item in _cells(db, scope_id, HeatmapGranularity.MONTH)
        if item.period_key == "2026-09"
    )

    assert cell.source_forecast_ids == tuple(
        sorted(forecast.id for forecast in batch.forecasts)
    )
    assert cell.supporting_statement_ids == tuple(
        sorted(prepared_id for forecast in batch.forecasts for prepared_id in forecast.supporting_statement_ids)
    )
    assert cell.evidence_count == 2
    stored_links = tuple(
        row[0]
        for row in db.execute(
            """
            SELECT source_forecast_id
            FROM heatmap_cell_forecasts
            WHERE heatmap_cell_id=(
                SELECT id FROM heatmap_cells
                WHERE scope_id=? AND granularity='month'
                    AND asset='nikkei_225' AND period_key='2026-09'
                    AND condition_kind='unconditional'
            )
            ORDER BY ordinal
            """,
            (scope_id,),
        )
    )
    assert stored_links == cell.source_forecast_ids
