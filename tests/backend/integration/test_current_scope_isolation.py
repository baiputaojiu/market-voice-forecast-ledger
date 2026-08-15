import json
import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    AssignmentKind,
    Confidence,
    MappingReviewDecision,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.domain.jobs import (
    ASSET_MAPPING_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.asset_mapping import AssetMappingService
from market_voice_forecast_ledger.services.codex_contract import CodexContractService
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from market_voice_forecast_ledger.services.periods import PeriodService
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_analysis_input_boundaries import (
    _add_video_with_segments,
    _begin,
    _create_job_for_input,
    _save_assignment,
)
from tests.backend.integration.test_forecast_projection import (
    NEWER,
    PreparedProjection,
    StatementSpec,
    _prepare_upstream,
    _project,
)
from tests.backend.integration.test_statement_evidence import _valid_receipt


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_replacing_one_scope_does_not_change_another_scope(db):
    first = _prepare_upstream(db, (StatementSpec("first-scope", NEWER),))
    first_batch = _project(db, first)
    second = _prepare_upstream(db, (StatementSpec("second-scope", NEWER),))
    second_batch = _project(db, second)
    service = CurrentResultService(db)
    second_scope_id = AnalysisRepository(db).get_run(second.run_id).scope_id

    with transaction(db):
        service._replace_scope_rows_in_transaction(second.run_id, second_batch.id)
    other_before = service.get_scope(second_scope_id)
    with transaction(db):
        service._replace_scope_rows_in_transaction(first.run_id, first_batch.id)

    assert service.get_scope(second_scope_id) == other_before


def _replace(db, prepared, batch):
    with transaction(db):
        return CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )


def _prepare_for_existing_subject(db, subject_id, label):
    video_id, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id=label,
        published_at=datetime(2026, 8, 12, 3, tzinfo=timezone.utc),
        texts=(f"Synthetic replacement evidence {label}.",),
        channel_index=91,
    )
    _save_assignment(
        db,
        segment_id=segment_ids[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=subject_id,
        evidence_hash=f"replacement-assignment-{label}",
    )
    prepared_job = _create_job_for_input(db, subject_id)
    run = _begin(db, prepared_job)
    jobs = JobStateService(db)
    jobs.begin_unit(prepared_job.job_id, "codex:batch:1")
    CodexContractService(db).validate_and_store(
        run.id,
        "codex:batch:1",
        json.dumps(
            {
                "run_id": run.id,
                "batch_key": "codex:batch:1",
                "statements": [
                    {
                        "statement_type": "future_forecast",
                        "forecast_basis": "direct",
                        "condition_kind": "unconditional",
                        "condition_text": None,
                        "direction_kind": "down",
                        "turning_point_kind": None,
                        "target_expression": "日経平均",
                        "period_expression": "来週",
                        "codex_asset_hints": [
                            {
                                "expression": "日経平均",
                                "suggested_asset": "nikkei_225",
                                "confidence": "high",
                            }
                        ],
                        "evidence": [
                            {
                                "segment_id": segment_ids[0],
                                "excerpt": f"Synthetic replacement evidence {label}.",
                            }
                        ],
                    }
                ],
            }
        ),
        _valid_receipt(),
    )
    jobs.begin_unit(prepared_job.job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    statements = StatementService(db).normalize_and_store(run.id)
    jobs.begin_unit(prepared_job.job_id, PERIOD_NORMALIZATION_UNIT_KEY)
    periods = PeriodService(db).normalize_run(run.id)
    jobs.begin_unit(prepared_job.job_id, ASSET_MAPPING_UNIT_KEY)
    mappings = AssetMappingService(db).map_run(run.id)
    prepared = PreparedProjection(
        run.id,
        prepared_job.job_id,
        tuple(row.id for row in statements),
        tuple(row.id for row in periods),
        tuple(row.id for row in mappings),
        (video_id,),
    )
    return prepared, _project(db, prepared)


def test_multiple_runs_for_one_scope_replace_only_that_scopes_source(db):
    first = _prepare_upstream(db, (StatementSpec("same-scope-first", NEWER),))
    first_batch = _project(db, first)
    first_run = AnalysisRepository(db).get_run(first.run_id)
    scope = AnalysisRepository(db).get_scope(first_run.scope_id)
    _replace(db, first, first_batch)

    second, second_batch = _prepare_for_existing_subject(
        db, scope.subject_id, "same-scope-second"
    )
    assert AnalysisRepository(db).get_run(second.run_id).scope_id == scope.id
    before = CurrentResultService(db).get_scope(scope.id)

    after = _replace(db, second, second_batch).after

    assert before.source_run_id == first.run_id
    assert after.scope_id == scope.id
    assert after.source_run_id == second.run_id
    assert after.projection_batch_id == second_batch.id


def test_current_rows_copy_all_statements_all_mappings_and_effective_rejections(db):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                "rejected-mapping",
                NEWER,
                confidence=Confidence.LOW,
            ),
        ),
    )
    initial = _project(db, prepared)
    assert initial.forecasts == ()
    review_count_before = db.execute(
        "SELECT COUNT(*) FROM mapping_reviews"
    ).fetchone()[0]
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.REJECT,
            "user",
            "Synthetic rejection retained in current mappings",
            None,
        )
    )
    with transaction(db):
        reviewed = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )
    immutable_before = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM analysis_asset_mappings WHERE run_id=? ORDER BY id",
            (prepared.run_id,),
        )
    )

    summary = _replace(db, prepared, reviewed).after

    assert summary.statement_ids == prepared.statement_ids
    assert summary.mapping_ids == prepared.mapping_ids
    assert summary.eligible_mapping_ids == ()
    assert summary.forecast_ids == ()
    assert summary.effective_mappings[0].mapping_id == prepared.mapping_ids[0]
    assert summary.effective_mappings[0].effective_asset is Asset.NIKKEI_225
    assert summary.effective_mappings[0].effective_eligibility is False
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM analysis_asset_mappings WHERE run_id=? ORDER BY id",
            (prepared.run_id,),
        )
    ) == immutable_before
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == (
        review_count_before + 1
    )


def test_all_empty_child_sets_keep_the_validated_run_and_batch_identity(db):
    prepared = _prepare_upstream(db, ())
    batch = _project(db, prepared)

    result = _replace(db, prepared, batch)

    assert result.after.source_run_id == prepared.run_id
    assert result.after.projection_batch_id == batch.id
    assert result.after.statement_count == 0
    assert result.after.mapping_count == 0
    assert result.after.eligible_mapping_count == 0
    assert result.after.forecast_count == 0
    assert db.execute(
        "SELECT COUNT(*) FROM current_result_sets WHERE scope_id=?",
        (result.after.scope_id,),
    ).fetchone()[0] == 1


def test_raw_current_rows_reject_cross_scope_run_statement_mapping_and_batch(db):
    first = _prepare_upstream(db, (StatementSpec("constraint-first", NEWER),))
    first_batch = _project(db, first)
    second = _prepare_upstream(db, (StatementSpec("constraint-second", NEWER),))
    second_batch = _project(db, second)
    _replace(db, first, first_batch)
    first_scope = AnalysisRepository(db).get_run(first.run_id).scope_id

    statements = (
        (first_scope, second.statement_ids[0], second.run_id),
        (first_scope, second.statement_ids[0], first.run_id),
    )
    for values in statements:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO current_statements(scope_id, analysis_statement_id, source_run_id) VALUES (?, ?, ?)",
                values,
            )

    mappings = (
        (first_scope, second.mapping_ids[0], second.run_id, "nikkei_225", 1),
        (first_scope, second.mapping_ids[0], first.run_id, "nikkei_225", 1),
    )
    for values in mappings:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO current_asset_mappings(
                    scope_id, analysis_mapping_id, source_run_id,
                    effective_asset, effective_eligibility
                ) VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )

    forecasts = (
        (
            first_scope,
            second_batch.forecasts[0].id,
            second.run_id,
            second_batch.id,
        ),
        (
            first_scope,
            first_batch.forecasts[0].id,
            first.run_id,
            second_batch.id,
        ),
    )
    for values in forecasts:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO current_forecasts(
                    scope_id, analysis_forecast_id, source_run_id,
                    projection_batch_id
                ) VALUES (?, ?, ?, ?)
                """,
                values,
            )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE current_result_sets SET source_run_id=? WHERE scope_id=?",
            (second.run_id, first_scope),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO current_statements(
                scope_id, analysis_statement_id, source_run_id
            ) VALUES (?, ?, ?)
            """,
            (first_scope, first.statement_ids[0], first.run_id),
        )


def test_get_scope_fails_closed_on_a_raw_orphan_without_a_result_header(db):
    prepared = _prepare_upstream(db, (StatementSpec("raw-orphan", NEWER),))
    scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id
    db.execute("DROP TRIGGER current_statements_validate_insert")
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute(
            """
            INSERT INTO current_statements(
                scope_id, analysis_statement_id, source_run_id
            ) VALUES (?, ?, ?)
            """,
            (scope_id, prepared.statement_ids[0], prepared.run_id),
        )
    finally:
        db.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(DomainError) as error:
        CurrentResultService(db).get_scope(scope_id)

    assert error.value.code == "CURRENT_RESULT_STATE_INVALID"


@pytest.mark.parametrize("missing_table", ("current_statements", "current_asset_mappings", "current_forecasts"))
def test_get_scope_fails_closed_on_incomplete_current_state(db, missing_table):
    prepared = _prepare_upstream(db, (StatementSpec("incomplete", NEWER),))
    batch = _project(db, prepared)
    _replace(db, prepared, batch)
    scope_id = AnalysisRepository(db).get_run(prepared.run_id).scope_id
    db.execute(f"DELETE FROM {missing_table} WHERE scope_id=?", (scope_id,))

    with pytest.raises(DomainError) as error:
        CurrentResultService(db).get_scope(scope_id)

    assert error.value.code == "CURRENT_RESULT_STATE_INVALID"


def test_get_scope_fails_closed_on_orphaned_or_mixed_header_state(db):
    first = _prepare_upstream(db, (StatementSpec("mixed-first", NEWER),))
    first_batch = _project(db, first)
    second = _prepare_upstream(db, (StatementSpec("mixed-second", NEWER),))
    _project(db, second)
    _replace(db, first, first_batch)
    scope_id = AnalysisRepository(db).get_run(first.run_id).scope_id
    db.execute("DROP TRIGGER current_statements_validate_update")
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute(
            """
            UPDATE current_statements
            SET source_run_id=?, analysis_statement_id=?
            WHERE scope_id=?
            """,
            (second.run_id, second.statement_ids[0], scope_id),
        )
    finally:
        db.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(DomainError) as error:
        CurrentResultService(db).get_scope(scope_id)

    assert error.value.code == "CURRENT_RESULT_STATE_INVALID"
