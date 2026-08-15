import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    ConditionKind,
    Confidence,
    DirectionKind,
    HeatmapGranularity,
    MappingReviewDecision,
    PeriodReviewDecision,
    ScopeStatus,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.heatmap import (
    HeatmapCell,
    HeatmapRow,
    HeatmapService,
    HeatmapView,
)
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
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


def _current_scope(db, specs):
    prepared = _prepare_upstream(db, tuple(specs))
    batch = _project(db, prepared)
    with transaction(db):
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )
    scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id
    return prepared, batch, scope_id


def _row(view, asset):
    return next(row for row in view.rows if row.asset is asset)


def test_heatmap_view_types_are_frozen_slotted_dataclasses():
    for view_type in (HeatmapCell, HeatmapRow, HeatmapView):
        assert view_type.__dataclass_params__.frozen is True
        assert view_type.__slots__


def test_heatmap_view_cell_uses_exact_typed_unique_lookup(db):
    _, _, scope_id = _current_scope(
        db, (StatementSpec("lookup", NEWER),)
    )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    view = service.read_scope(scope_id, HeatmapGranularity.WEEK)
    row = _row(view, Asset.NIKKEI_225)
    expected = row.cells[0]

    assert view.cell(row.subject_key, row.asset, expected.period_key) == expected
    with pytest.raises(DomainError) as missing:
        view.cell(row.subject_key, row.asset, "2099-01-01/2099-01-07")
    assert missing.value.code == "HEATMAP_CELL_NOT_FOUND"
    with pytest.raises(DomainError) as invalid:
        view.cell(row.subject_key, row.asset.value, expected.period_key)
    assert invalid.value.code == "HEATMAP_CELL_LOOKUP_INVALID"


def test_rebuild_projects_inclusive_month_across_every_intersecting_week(db):
    _, batch, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "month-span",
                NEWER,
                period_expression="2026年9月",
            ),
        ),
    )

    cell_count = HeatmapService(db).rebuild_scope(scope_id)
    week = HeatmapService(db).read_scope(scope_id, HeatmapGranularity.WEEK)
    month = HeatmapService(db).read_scope(scope_id, HeatmapGranularity.MONTH)

    assert cell_count == 6
    assert [row.asset for row in week.rows] == list(Asset)
    assert [row.asset for row in month.rows] == list(Asset)
    assert [cell.period_key for cell in _row(week, Asset.NIKKEI_225).cells] == [
        "2026-08-31/2026-09-06",
        "2026-09-07/2026-09-13",
        "2026-09-14/2026-09-20",
        "2026-09-21/2026-09-27",
        "2026-09-28/2026-10-04",
    ]
    assert [cell.period_key for cell in _row(month, Asset.NIKKEI_225).cells] == [
        "2026-09"
    ]
    stored = batch.forecasts[0]
    assert (stored.period_start, stored.period_end) == (
        date(2026, 9, 1),
        date(2026, 9, 30),
    )


def test_week_and_month_edges_are_fixed_calendar_boundaries(db):
    _, _, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "leap-month",
                NEWER,
                period_expression="2028年2月",
            ),
        ),
    )
    HeatmapService(db).rebuild_scope(scope_id)

    week_cells = _row(
        HeatmapService(db).read_scope(scope_id, HeatmapGranularity.WEEK),
        Asset.NIKKEI_225,
    ).cells
    month_cells = _row(
        HeatmapService(db).read_scope(scope_id, HeatmapGranularity.MONTH),
        Asset.NIKKEI_225,
    ).cells

    assert week_cells[0].period_key == "2028-01-31/2028-02-06"
    assert week_cells[-1].period_key == "2028-02-28/2028-03-05"
    assert month_cells[0].period_key == "2028-02"
    assert month_cells[0].slot_start == date(2028, 2, 1)
    assert month_cells[0].slot_end == date(2028, 2, 29)


def test_rebuild_is_delete_replace_deterministic_and_caller_atomic(db):
    _, _, scope_id = _current_scope(
        db,
        (StatementSpec("deterministic", NEWER),),
    )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    first = (
        asdict(service.read_scope(scope_id, HeatmapGranularity.WEEK)),
        asdict(service.read_scope(scope_id, HeatmapGranularity.MONTH)),
    )

    _, _, other_scope_id = _current_scope(
        db, (StatementSpec("interleaved-cache", NEWER),)
    )
    service.rebuild_scope(other_scope_id)

    assert service.rebuild_scope(scope_id) == 2
    second = (
        asdict(service.read_scope(scope_id, HeatmapGranularity.WEEK)),
        asdict(service.read_scope(scope_id, HeatmapGranularity.MONTH)),
    )
    assert second == first

    with pytest.raises(RuntimeError, match="caller rollback"):
        with transaction(db):
            service._rebuild_scope_in_transaction(scope_id)
            db.execute("DELETE FROM heatmap_cells WHERE scope_id=?", (scope_id,))
            raise RuntimeError("caller rollback")

    assert asdict(service.read_scope(scope_id, HeatmapGranularity.WEEK)) == first[0]


def test_empty_current_forecasts_are_a_valid_complete_empty_cache(db):
    _, _, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "review-required",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    service = HeatmapService(db)

    assert service.rebuild_scope(scope_id) == 0
    for granularity in HeatmapGranularity:
        view = service.read_scope(scope_id, granularity)
        assert len(view.rows) == 4
        assert all(row.cells == () for row in view.rows)


def test_read_cutoff_returns_four_assets_for_each_of_exactly_four_subjects(db):
    _, _, scope_id = _current_scope(db, (StatementSpec("cutoff", NEWER),))
    scope = AnalysisRepository(db).get_scope(scope_id)
    sources = SourceRepository(db)
    for ordinal in range(3):
        sources.create_subject(f"Synthetic empty subject {ordinal}", SubjectKind.PERSON)
    HeatmapService(db).rebuild_scope(scope_id)

    view = HeatmapService(db).read_cutoff(
        scope.cutoff_day_jst, HeatmapGranularity.WEEK
    )
    active = tuple(
        db.execute(
            "SELECT id, canonical_name FROM analysis_subjects "
            "WHERE is_active=1 ORDER BY id"
        )
    )

    assert len(active) == 4
    assert len(view.rows) == 16
    assert [(row.subject_id, row.asset) for row in view.rows] == [
        (subject["id"], asset) for subject in active for asset in Asset
    ]
    assert sum(bool(row.cells) for row in view.rows) == 1
    populated_subject_id = AnalysisRepository(db).get_scope(scope_id).subject_id
    populated_rows = tuple(
        row for row in view.rows if row.subject_id == populated_subject_id
    )
    missing_rows = tuple(
        row for row in view.rows if row.subject_id != populated_subject_id
    )
    assert all(row.scope_id == scope_id for row in populated_rows)
    assert all(row.scope_status is ScopeStatus.RUNNING for row in populated_rows)
    assert all(row.stale_reason is None for row in populated_rows)
    assert all(row.scope_id is None for row in missing_rows)
    assert all(row.scope_status is None for row in missing_rows)
    assert view.scope_status is None
    assert view.stale_reason is None


@pytest.mark.parametrize("subject_count", (3, 5))
def test_read_cutoff_fails_closed_for_malformed_active_subject_set(
    db, subject_count
):
    sources = SourceRepository(db)
    for ordinal in range(subject_count):
        sources.create_subject(
            f"Synthetic malformed subject {ordinal}", SubjectKind.PERSON
        )

    with pytest.raises(DomainError) as error:
        HeatmapService(db).read_cutoff(
            date(2026, 8, 14), HeatmapGranularity.MONTH
        )

    assert error.value.code == "HEATMAP_ACTIVE_SUBJECT_SET_INVALID"


def test_cache_json_is_canonical_and_contains_only_safe_classifications(db):
    _, _, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "conditional-safe",
                NEWER,
                condition_kind=ConditionKind.CONDITIONAL,
                condition_text="Synthetic public condition",
            ),
        ),
    )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)

    rows = tuple(db.execute("SELECT * FROM heatmap_cells ORDER BY id"))
    serialized = json.dumps(
        [dict(row) for row in rows], ensure_ascii=False, sort_keys=True
    )
    view_serialized = repr(asdict(service.read_scope(scope_id, HeatmapGranularity.WEEK)))

    assert rows
    for row in rows:
        assert row["directions_json"] == json.dumps(
            json.loads(row["directions_json"]),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert row["condition_texts_json"] == json.dumps(
            json.loads(row["condition_texts_json"]),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    for forbidden in (
        "Synthetic projection evidence",
        "transcript",
        "excerpt",
        "input_text",
        "audio_path",
        "prompt_body",
    ):
        assert forbidden not in serialized
        assert forbidden not in view_serialized


def test_sql_constraints_reject_noncanonical_cell_json_and_cross_scope_links(db):
    first, _, first_scope = _current_scope(
        db, (StatementSpec("constraint-a", NEWER),)
    )
    _, _, second_scope = _current_scope(
        db, (StatementSpec("constraint-b", NEWER),)
    )
    service = HeatmapService(db)
    service.rebuild_scope(first_scope)
    service.rebuild_scope(second_scope)
    cell_id = db.execute(
        "SELECT id FROM heatmap_cells WHERE scope_id=? ORDER BY id LIMIT 1",
        (second_scope,),
    ).fetchone()[0]
    foreign_forecast_id = db.execute(
        "SELECT analysis_forecast_id FROM current_forecasts WHERE scope_id=?",
        (first_scope,),
    ).fetchone()[0]
    second_header = db.execute(
        "SELECT * FROM current_result_sets WHERE scope_id=?", (second_scope,)
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE heatmap_cells SET directions_json='[\"up\", \"down\"]' "
            "WHERE id=?",
            (cell_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO heatmap_cell_forecasts(
                heatmap_cell_id, scope_id, source_run_id,
                projection_batch_id, source_forecast_id, ordinal
            ) VALUES (?, ?, ?, ?, ?, 2)
            """,
            (
                cell_id,
                second_scope,
                second_header["source_run_id"],
                second_header["projection_batch_id"],
                foreign_forecast_id,
            ),
        )
    assert first.run_id != second_header["source_run_id"]


def test_cache_tables_reject_all_raw_updates(db):
    _, _, scope_id = _current_scope(
        db, (StatementSpec("immutable-cache", NEWER),)
    )
    HeatmapService(db).rebuild_scope(scope_id)

    with pytest.raises(sqlite3.IntegrityError, match="HEATMAP_CELL_UPDATE_FORBIDDEN"):
        db.execute(
            "UPDATE heatmap_cells SET confidence=confidence WHERE scope_id=?",
            (scope_id,),
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="HEATMAP_CELL_FORECAST_UPDATE_FORBIDDEN",
    ):
        db.execute(
            "UPDATE heatmap_cell_forecasts SET ordinal=ordinal WHERE scope_id=?",
            (scope_id,),
        )


def test_reader_fails_closed_for_missing_link_and_stored_json_corruption(db):
    _, _, scope_id = _current_scope(db, (StatementSpec("corrupt", NEWER),))
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    link = db.execute(
        "SELECT heatmap_cell_id, source_forecast_id FROM heatmap_cell_forecasts "
        "WHERE scope_id=? ORDER BY heatmap_cell_id LIMIT 1",
        (scope_id,),
    ).fetchone()
    db.execute("DROP TRIGGER heatmap_cell_forecasts_no_update")
    db.execute(
        "UPDATE heatmap_cell_forecasts SET ordinal=2 "
        "WHERE heatmap_cell_id=? AND source_forecast_id=?",
        tuple(link),
    )
    with pytest.raises(DomainError) as ordinal:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert ordinal.value.code == "HEATMAP_CACHE_INVALID"

    service.rebuild_scope(scope_id)
    link = db.execute(
        "SELECT heatmap_cell_id, source_forecast_id FROM heatmap_cell_forecasts "
        "WHERE scope_id=? ORDER BY heatmap_cell_id LIMIT 1",
        (scope_id,),
    ).fetchone()
    db.execute(
        "DELETE FROM heatmap_cell_forecasts "
        "WHERE heatmap_cell_id=? AND source_forecast_id=?",
        tuple(link),
    )

    with pytest.raises(DomainError) as missing:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert missing.value.code == "HEATMAP_CACHE_INVALID"

    service.rebuild_scope(scope_id)
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("DROP TRIGGER heatmap_cells_no_update")
    db.execute(
        "UPDATE heatmap_cells SET directions_json='not-json' "
        "WHERE scope_id=? AND granularity='week'",
        (scope_id,),
    )
    with pytest.raises(DomainError) as malformed:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert malformed.value.code == "HEATMAP_CACHE_INVALID"


def test_reader_fails_closed_for_stored_run_and_batch_ownership_corruption(db):
    _, _, first_scope = _current_scope(
        db, (StatementSpec("ownership-read-a", NEWER),)
    )
    _, _, second_scope = _current_scope(
        db, (StatementSpec("ownership-read-b", NEWER),)
    )
    service = HeatmapService(db)
    service.rebuild_scope(first_scope)
    second_header = db.execute(
        "SELECT source_run_id, projection_batch_id "
        "FROM current_result_sets WHERE scope_id=?",
        (second_scope,),
    ).fetchone()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER heatmap_cells_no_update")
    db.execute("DROP TRIGGER heatmap_cell_forecasts_no_update")
    db.execute(
        "UPDATE heatmap_cells SET source_run_id=?, projection_batch_id=? "
        "WHERE scope_id=?",
        (
            second_header["source_run_id"],
            second_header["projection_batch_id"],
            first_scope,
        ),
    )
    db.execute(
        "UPDATE heatmap_cell_forecasts "
        "SET source_run_id=?, projection_batch_id=? WHERE scope_id=?",
        (
            second_header["source_run_id"],
            second_header["projection_batch_id"],
            first_scope,
        ),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as error:
        service.read_scope(first_scope, HeatmapGranularity.WEEK)

    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_rebuild_and_read_fail_closed_for_orphan_current_forecast(db):
    _, batch, scope_id = _current_scope(
        db, (StatementSpec("orphan-current", NEWER),)
    )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER analysis_forecasts_no_delete")
    db.execute(
        "DELETE FROM analysis_forecasts WHERE projection_batch_id=?",
        (batch.id,),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as rebuild_error:
        service.rebuild_scope(scope_id)
    assert rebuild_error.value.code == "HEATMAP_CACHE_INVALID"
    with pytest.raises(DomainError) as read_error:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert read_error.value.code == "HEATMAP_CACHE_INVALID"


def test_reader_fails_closed_for_orphan_cache_link_on_valid_empty_result(db):
    prepared, batch, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "orphan-cache-link",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    service = HeatmapService(db)
    assert service.rebuild_scope(scope_id) == 0
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        """
        INSERT INTO heatmap_cell_forecasts(
            heatmap_cell_id, scope_id, source_run_id,
            projection_batch_id, source_forecast_id, ordinal
        ) VALUES (999999, ?, ?, ?, 999999, 1)
        """,
        (scope_id, prepared.run_id, batch.id),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as rebuild_error:
        service.rebuild_scope(scope_id)
    assert rebuild_error.value.code == "HEATMAP_CACHE_INVALID"
    with pytest.raises(DomainError) as error:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_reader_fails_closed_for_cross_scope_current_header_owner(db):
    _, _, first_scope = _current_scope(
        db,
        (
            StatementSpec(
                "header-owner-a", NEWER, confidence=Confidence.LOW
            ),
        ),
    )
    _, _, second_scope = _current_scope(
        db,
        (
            StatementSpec(
                "header-owner-b", NEWER, confidence=Confidence.LOW
            ),
        ),
    )
    service = HeatmapService(db)
    service.rebuild_scope(first_scope)
    foreign_header = db.execute(
        "SELECT source_run_id, projection_batch_id "
        "FROM current_result_sets WHERE scope_id=?",
        (second_scope,),
    ).fetchone()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER current_result_sets_validate_update")
    db.execute(
        "UPDATE current_result_sets "
        "SET source_run_id=?, projection_batch_id=? WHERE scope_id=?",
        (
            foreign_header["source_run_id"],
            foreign_header["projection_batch_id"],
            first_scope,
        ),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as error:
        service.read_scope(first_scope, HeatmapGranularity.WEEK)
    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_reader_fails_closed_for_unapplied_latest_review_projection(db):
    prepared, initial, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "old-current-batch",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    service = HeatmapService(db)
    assert service.rebuild_scope(scope_id) == 0
    with transaction(db):
        MappingReviewService(db)._review_in_transaction(
            MappingReviewCommand(
                mapping_id=prepared.mapping_ids[0],
                decision=MappingReviewDecision.APPROVE,
                actor="user",
                reason="Synthetic unapplied heatmap review",
                corrected_asset=None,
            )
        )
        latest = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )
    assert latest.id > initial.id

    with pytest.raises(DomainError) as error:
        service.read_scope(scope_id, HeatmapGranularity.WEEK)
    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_rebuild_translates_malformed_current_forecast_shape_to_safe_error(db):
    _, batch, scope_id = _current_scope(
        db, (StatementSpec("malformed-current-forecast", NEWER),)
    )
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("DROP TRIGGER analysis_forecasts_no_update")
    db.execute(
        "UPDATE analysis_forecasts SET period_specificity='bad' "
        "WHERE projection_batch_id=?",
        (batch.id,),
    )

    with pytest.raises(DomainError) as error:
        HeatmapService(db).rebuild_scope(scope_id)

    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_rebuild_rejects_semantically_rewritten_current_forecast_period(db):
    _, batch, scope_id = _current_scope(
        db, (StatementSpec("rewritten-period", NEWER),)
    )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    db.execute("DROP TRIGGER analysis_forecasts_no_update")
    db.execute(
        """
        UPDATE analysis_forecasts
        SET period_start='2030-01-01',
            period_end='2030-01-07',
            period_specificity=3
        WHERE projection_batch_id=?
        """,
        (batch.id,),
    )

    with pytest.raises(DomainError) as error:
        service.rebuild_scope(scope_id)

    assert error.value.code == "HEATMAP_CACHE_INVALID"


def test_rebuild_rejects_foreign_counterevidence_statement_link(db):
    _, batch, scope_id = _current_scope(
        db,
        (
            StatementSpec(
                "counter-owner-older",
                OLDER,
                DirectionKind.DOWN,
                period_expression="2026年9月",
            ),
            StatementSpec(
                "counter-owner-newer",
                NEWER,
                DirectionKind.UP,
                period_expression="2026年9月",
            ),
        ),
    )
    foreign, _, _ = _current_scope(
        db, (StatementSpec("foreign-counter-owner", NEWER),)
    )
    forecast = batch.forecasts[0]
    assert forecast.counterevidence_statement_ids
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DROP TRIGGER analysis_forecast_statement_links_no_update")
    db.execute(
        """
        UPDATE analysis_forecast_statement_links
        SET statement_id=?
        WHERE forecast_id=? AND relation_kind='counterevidence'
        """,
        (foreign.statement_ids[0], forecast.id),
    )
    db.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(DomainError) as error:
        service.rebuild_scope(scope_id)

    assert error.value.code == "HEATMAP_CACHE_INVALID"


@pytest.mark.parametrize(
    "corruption",
    (
        "heatmap_eligible",
        "unknown_period",
        "period_dates",
        "selected_published_at",
        "batch_created_at",
    ),
)
def test_rebuild_rejects_noncanonical_raw_projection_storage(db, corruption):
    if corruption == "unknown_period":
        prepared = _prepare_upstream(
            db,
            (
                StatementSpec(
                    "raw-unknown-period",
                    NEWER,
                    period_expression="当面",
                ),
            ),
        )
        PeriodReviewService(db).review(
            prepared.period_ids[0],
            PeriodReviewDecision.APPROVE_UNKNOWN,
            "user",
            "Synthetic raw unknown approval",
        )
        batch = _project(db, prepared)
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )
        scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id
    else:
        _, batch, scope_id = _current_scope(
            db,
            (
                StatementSpec(
                    f"raw-{corruption}",
                    NEWER,
                    period_expression="2026年9月",
                ),
            ),
        )
    service = HeatmapService(db)
    service.rebuild_scope(scope_id)
    db.execute("PRAGMA ignore_check_constraints=ON")
    if corruption == "batch_created_at":
        db.execute("DROP TRIGGER forecast_projection_batches_no_update")
        stored = db.execute(
            "SELECT created_at FROM forecast_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()[0]
        rewritten = datetime.fromisoformat(
            stored.replace("Z", "+00:00")
        ).astimezone(timezone(timedelta(hours=9))).isoformat(
            timespec="microseconds"
        )
        db.execute(
            "UPDATE forecast_projection_batches SET created_at=? WHERE id=?",
            (rewritten, batch.id),
        )
    else:
        db.execute("DROP TRIGGER analysis_forecasts_no_update")
        if corruption == "heatmap_eligible":
            db.execute(
                "UPDATE analysis_forecasts SET heatmap_eligible=2 "
                "WHERE projection_batch_id=?",
                (batch.id,),
            )
        elif corruption == "unknown_period":
            db.execute(
                "UPDATE analysis_forecasts SET unknown_period=2 "
                "WHERE projection_batch_id=?",
                (batch.id,),
            )
        elif corruption == "period_dates":
            db.execute(
                "UPDATE analysis_forecasts "
                "SET period_start='20260901', period_end='20260930' "
                "WHERE projection_batch_id=?",
                (batch.id,),
            )
        else:
            stored = db.execute(
                "SELECT selected_published_at FROM analysis_forecasts "
                "WHERE projection_batch_id=?",
                (batch.id,),
            ).fetchone()[0]
            rewritten = datetime.fromisoformat(
                stored.replace("Z", "+00:00")
            ).astimezone(timezone(timedelta(hours=9))).isoformat(
                timespec="microseconds"
            )
            db.execute(
                "UPDATE analysis_forecasts SET selected_published_at=? "
                "WHERE projection_batch_id=?",
                (rewritten, batch.id),
            )

    with pytest.raises(DomainError) as error:
        service.rebuild_scope(scope_id)

    assert error.value.code == "HEATMAP_CACHE_INVALID"
