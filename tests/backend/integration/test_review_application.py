import json
import sqlite3
from dataclasses import asdict

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Asset,
    Confidence,
    HeatmapGranularity,
    JobStatus,
    MappingReviewDecision,
    PeriodReviewDecision,
    ScopeStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.domain.jobs import FINAL_PROMOTION_UNIT_KEY
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from market_voice_forecast_ledger.services.periods import PeriodReviewService
from market_voice_forecast_ledger.services.review_application import (
    ReviewApplicationResult,
    ReviewApplicationService,
)
from tests.backend.e2e.synthetic_fixture import (
    create_accepted_low_mapping_fixture,
    create_accepted_unknown_period_fixture,
)
from tests.backend.integration.test_atomic_result_replacement import (
    _completed_run,
)
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
    _create_job_for_input,
)
from tests.backend.integration.test_forecast_projection import (
    NEWER,
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


def _mapping_command(mapping_id, decision, *, corrected_asset=None, reason=None):
    return MappingReviewCommand(
        mapping_id=mapping_id,
        decision=decision,
        actor="user",
        reason=reason or f"Synthetic {decision.value} application",
        corrected_asset=corrected_asset,
    )


def _job_terminal(db, prepared):
    return (
        JobStateService(db).status(prepared.job_id),
        JobStateService(db).unit(
            prepared.job_id, FINAL_PROMOTION_UNIT_KEY
        ),
        tuple(
            tuple(row)
            for row in db.execute(
                "SELECT status, safe_error_code, created_at "
                "FROM analysis_run_events WHERE run_id=? ORDER BY id",
                (prepared.run_id,),
            )
        ),
    )


def _review_state(db, prepared, scope_id):
    tables = (
        "mapping_reviews",
        "period_reviews",
        "forecast_projection_batches",
        "analysis_forecasts",
        "analysis_forecast_statement_links",
        "current_result_sets",
        "current_statements",
        "current_asset_mappings",
        "current_forecasts",
        "heatmap_cells",
        "heatmap_cell_forecasts",
        "audit_events",
    )
    rows = tuple(
        (
            table,
            tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")),
        )
        for table in tables
    )
    return rows, _job_terminal(db, prepared), AnalysisRepository(db).get_scope(scope_id)


def test_review_result_type_is_frozen_and_slotted():
    assert ReviewApplicationResult.__dataclass_params__.frozen is True
    assert ReviewApplicationResult.__slots__
    assert tuple(ReviewApplicationResult.__dataclass_fields__) == (
        "applied_to_current",
        "current_summary",
        "rebuilt_cell_count",
    )


def test_current_mapping_review_must_use_atomic_application_path(db):
    prepared, _, _ = create_accepted_low_mapping_fixture(db, "mapping-guard")
    command = _mapping_command(
        prepared.mapping_ids[0], MappingReviewDecision.APPROVE
    )

    with pytest.raises(DomainError) as error:
        MappingReviewService(db).review(command)

    assert error.value.code == "REVIEW_APPLICATION_REQUIRED"
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_mapping_approval_reprojects_current_and_rebuilds_cache_atomically(db):
    prepared, initial, scope_id = create_accepted_low_mapping_fixture(
        db, "mapping-approve"
    )
    before_terminal = _job_terminal(db, prepared)

    result = ReviewApplicationService(db).apply_mapping(
        _mapping_command(
            prepared.mapping_ids[0], MappingReviewDecision.APPROVE
        )
    )

    assert result.applied_to_current is True
    assert result.current_summary is not None
    assert result.current_summary.scope_id == scope_id
    assert result.current_summary.forecast_count == 1
    assert result.rebuilt_cell_count == 2
    assert result.current_summary.projection_batch_id > initial.id
    batch = db.execute(
        "SELECT * FROM forecast_projection_batches WHERE id=?",
        (result.current_summary.projection_batch_id,),
    ).fetchone()
    assert batch["trigger_kind"] == ProjectionTrigger.MAPPING_REVIEW.value
    assert batch["latest_mapping_review_id"] is not None
    assert batch["latest_period_review_id"] is None
    assert _job_terminal(db, prepared) == before_terminal
    assert before_terminal[0] is JobStatus.SUCCEEDED
    assert before_terminal[1].status is UnitStatus.SUCCESS
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events "
        "WHERE run_id=? AND status='accepted'",
        (prepared.run_id,),
    ).fetchone()[0] == 1
    cells = HeatmapService(db).read_scope(
        scope_id, HeatmapGranularity.WEEK
    ).rows[0].cells
    assert len(cells) == 1
    assert cells[0].asset is Asset.NIKKEI_225


def test_mapping_correction_rejection_and_consecutive_reviews_advance_one_head(db):
    prepared, _, scope_id = create_accepted_low_mapping_fixture(
        db, "mapping-sequence"
    )
    service = ReviewApplicationService(db)
    approved = service.apply_mapping(
        _mapping_command(prepared.mapping_ids[0], MappingReviewDecision.APPROVE)
    )
    rejected = service.apply_mapping(
        _mapping_command(prepared.mapping_ids[0], MappingReviewDecision.REJECT)
    )
    corrected = service.apply_mapping(
        _mapping_command(
            prepared.mapping_ids[0],
            MappingReviewDecision.CORRECT,
            corrected_asset=Asset.TOPIX,
        )
    )

    assert approved.current_summary.forecast_count == 1
    assert rejected.current_summary.forecast_count == 0
    assert corrected.current_summary.forecast_count == 1
    assert (
        approved.current_summary.projection_batch_id
        < rejected.current_summary.projection_batch_id
    )
    assert (
        rejected.current_summary.projection_batch_id
        < corrected.current_summary.projection_batch_id
    )
    batches = tuple(
        db.execute(
            """
            SELECT trigger_kind, latest_mapping_review_id,
                   latest_period_review_id
            FROM forecast_projection_batches
            WHERE run_id=? AND trigger_kind='mapping_review'
            ORDER BY id
            """,
            (prepared.run_id,),
        )
    )
    assert [row["latest_mapping_review_id"] for row in batches] == [1, 2, 3]
    assert all(row["latest_period_review_id"] is None for row in batches)
    rows = HeatmapService(db).read_scope(
        scope_id, HeatmapGranularity.WEEK
    ).rows
    assert next(row for row in rows if row.asset is Asset.TOPIX).cells
    assert not next(row for row in rows if row.asset is Asset.NIKKEI_225).cells


def test_current_period_review_must_use_application_and_can_add_unknown_cell(db):
    prepared, initial, scope_id = create_accepted_unknown_period_fixture(db)
    period_id = prepared.period_ids[0]
    with pytest.raises(DomainError) as guarded:
        PeriodReviewService(db).review(
            period_id,
            PeriodReviewDecision.APPROVE_UNKNOWN,
            "user",
            "Synthetic guarded approval",
        )
    assert guarded.value.code == "REVIEW_APPLICATION_REQUIRED"

    approved = ReviewApplicationService(db).apply_period(
        period_id,
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "Synthetic current unknown approval",
    )

    assert approved.applied_to_current is True
    assert approved.current_summary.forecast_count == 1
    assert approved.current_summary.projection_batch_id > initial.id
    assert approved.rebuilt_cell_count == 2
    for granularity in HeatmapGranularity:
        cells = HeatmapService(db).read_scope(scope_id, granularity).rows[0].cells
        assert len(cells) == 1
        assert cells[0].period_key == "unknown"
    batch = db.execute(
        "SELECT * FROM forecast_projection_batches WHERE id=?",
        (approved.current_summary.projection_batch_id,),
    ).fetchone()
    assert batch["trigger_kind"] == ProjectionTrigger.PERIOD_REVIEW.value
    assert batch["latest_mapping_review_id"] is None
    assert batch["latest_period_review_id"] is not None

    rejected = ReviewApplicationService(db).apply_period(
        period_id,
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic current unknown rejection",
    )
    assert rejected.current_summary.forecast_count == 0
    assert rejected.rebuilt_cell_count == 0


def test_review_application_preserves_correction_driven_stale_warning(db):
    prepared, _, scope_id = create_accepted_low_mapping_fixture(
        db, "stale-review"
    )
    segment_id = db.execute(
        "SELECT segment_id FROM analysis_run_segments "
        "WHERE run_id=? ORDER BY ordinal LIMIT 1",
        (prepared.run_id,),
    ).fetchone()[0]
    with transaction(db):
        AnalysisRepository(db).mark_scopes_using_segment_stale(
            segment_id, "SPEAKER_ASSIGNMENT_CHANGED"
        )

    command = _mapping_command(
        prepared.mapping_ids[0], MappingReviewDecision.APPROVE
    )
    with pytest.raises(DomainError) as guarded:
        MappingReviewService(db).review(command)
    assert guarded.value.code == "REVIEW_APPLICATION_REQUIRED"
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0

    result = ReviewApplicationService(db).apply_mapping(command)

    scope = AnalysisRepository(db).get_scope(scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "SPEAKER_ASSIGNMENT_CHANGED",
    )
    view = HeatmapService(db).read_scope(scope_id, HeatmapGranularity.WEEK)
    assert view.scope_status is ScopeStatus.STALE
    assert view.stale_reason == "SPEAKER_ASSIGNMENT_CHANGED"
    assert result.current_summary.forecast_count == 1


def test_review_application_updates_display_while_fresh_run_stays_running(db):
    prepared, _, scope_id = create_accepted_low_mapping_fixture(
        db, "running-review"
    )
    scope = AnalysisRepository(db).get_scope(scope_id)
    fresh_prepared = _create_job_for_input(
        db, scope.subject_id, scope.cutoff_day_jst
    )
    fresh_run = _begin(db, fresh_prepared)
    fresh_before = (
        JobStateService(db).status(fresh_prepared.job_id),
        tuple(
            tuple(row)
            for row in db.execute(
                "SELECT unit_key, status, output_hash FROM job_units "
                "WHERE job_id=? ORDER BY ordinal",
                (fresh_prepared.job_id,),
            )
        ),
        tuple(
            tuple(row)
            for row in db.execute(
                "SELECT status, safe_error_code, created_at "
                "FROM analysis_run_events WHERE run_id=? ORDER BY id",
                (fresh_run.id,),
            )
        ),
    )

    result = ReviewApplicationService(db).apply_mapping(
        _mapping_command(
            prepared.mapping_ids[0], MappingReviewDecision.APPROVE
        )
    )

    scope_after = AnalysisRepository(db).get_scope(scope_id)
    assert result.applied_to_current is True
    assert result.current_summary.forecast_count == 1
    assert scope_after.status is ScopeStatus.RUNNING
    assert scope_after.stale_reason is None
    assert (
        JobStateService(db).status(fresh_prepared.job_id),
        tuple(
            tuple(row)
            for row in db.execute(
                "SELECT unit_key, status, output_hash FROM job_units "
                "WHERE job_id=? ORDER BY ordinal",
                (fresh_prepared.job_id,),
            )
        ),
        tuple(
            tuple(row)
            for row in db.execute(
                "SELECT status, safe_error_code, created_at "
                "FROM analysis_run_events WHERE run_id=? ORDER BY id",
                (fresh_run.id,),
            )
        ),
    ) == fresh_before


def test_historical_mapping_and_period_reviews_keep_low_level_behavior(db):
    mapping_prepared, _ = _completed_run(
        db, confidence=Confidence.LOW, label="historical-mapping"
    )
    mapping_review_id = MappingReviewService(db).review(
        _mapping_command(
            mapping_prepared.mapping_ids[0], MappingReviewDecision.APPROVE
        )
    )
    period_prepared = _prepare_upstream(
        db,
        (StatementSpec("historical-period", NEWER, period_expression="当面"),),
    )
    period_review_id = PeriodReviewService(db).review(
        period_prepared.period_ids[0],
        PeriodReviewDecision.APPROVE_UNKNOWN,
        "user",
        "Synthetic historical approval",
    )

    assert mapping_review_id > 0
    assert period_review_id > 0
    assert db.execute("SELECT COUNT(*) FROM current_result_sets").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM forecast_projection_batches "
        "WHERE trigger_kind!='initial'"
    ).fetchone()[0] == 0


def test_internal_review_insertions_require_caller_owned_transactions(db):
    mapping_prepared, _ = _completed_run(
        db, confidence=Confidence.LOW, label="internal-mapping"
    )
    with pytest.raises(DomainError) as mapping_error:
        MappingReviewService(db)._review_in_transaction(
            _mapping_command(
                mapping_prepared.mapping_ids[0], MappingReviewDecision.APPROVE
            )
        )
    assert mapping_error.value.code == "REVIEW_TRANSACTION_REQUIRED"

    period_prepared = _prepare_upstream(
        db,
        (StatementSpec("internal-period", NEWER, period_expression="当面"),),
    )
    with pytest.raises(DomainError) as period_error:
        PeriodReviewService(db)._review_in_transaction(
            period_prepared.period_ids[0],
            PeriodReviewDecision.REJECT,
            "user",
            "Synthetic internal rejection",
        )
    assert period_error.value.code == "REVIEW_TRANSACTION_REQUIRED"


_REVIEW_FAILURE_TRIGGERS = {
    "review_insert": """
        CREATE TRIGGER synthetic_application_review
        BEFORE INSERT ON mapping_reviews
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
    "review_audit": """
        CREATE TRIGGER synthetic_application_review_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.entity_type='analysis_asset_mapping'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
    "projection": """
        CREATE TRIGGER synthetic_application_projection
        BEFORE INSERT ON forecast_projection_batches
        WHEN NEW.trigger_kind='mapping_review'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
    "current": """
        CREATE TRIGGER synthetic_application_current
        BEFORE DELETE ON current_result_sets
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
    "cache": """
        CREATE TRIGGER synthetic_application_cache
        BEFORE INSERT ON heatmap_cells
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
    "result_audit": """
        CREATE TRIGGER synthetic_application_result_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.entity_type='analysis_scope' AND NEW.operation='result_replaced'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_APPLICATION_FAILURE'); END
    """,
}


@pytest.mark.parametrize("failure_point", tuple(_REVIEW_FAILURE_TRIGGERS))
def test_every_review_application_boundary_rolls_back_together(
    db, failure_point
):
    prepared, _, scope_id = create_accepted_low_mapping_fixture(
        db, f"review-rollback-{failure_point}"
    )
    before = _review_state(db, prepared, scope_id)
    db.execute(_REVIEW_FAILURE_TRIGGERS[failure_point])

    with pytest.raises((DomainError, sqlite3.Error)):
        ReviewApplicationService(db).apply_mapping(
            _mapping_command(
                prepared.mapping_ids[0], MappingReviewDecision.APPROVE
            )
        )

    assert _review_state(db, prepared, scope_id) == before
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_period_review_application_failure_rolls_back_review_and_projection(db):
    prepared, _, scope_id = create_accepted_unknown_period_fixture(
        db, "period-rollback"
    )
    before = _review_state(db, prepared, scope_id)
    db.execute(
        """
        CREATE TRIGGER synthetic_period_application_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.entity_type='analysis_statement_period'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PERIOD_APPLICATION_FAILURE'); END
        """
    )

    with pytest.raises((DomainError, sqlite3.Error)):
        ReviewApplicationService(db).apply_period(
            prepared.period_ids[0],
            PeriodReviewDecision.APPROVE_UNKNOWN,
            "user",
            "Synthetic period rollback",
        )

    assert _review_state(db, prepared, scope_id) == before
    assert db.execute("SELECT COUNT(*) FROM period_reviews").fetchone()[0] == 0


def test_review_application_writes_only_safe_audits_and_no_new_codex_run(db):
    prepared, _, scope_id = create_accepted_low_mapping_fixture(
        db, "review-safety"
    )
    private_input = AnalysisRepository(db).get_snapshot(prepared.run_id).input_text
    assert private_input is not None
    assert "Synthetic projection evidence" in private_input
    run_count = db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
    output_before = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM analysis_run_outputs WHERE run_id=?",
            (prepared.run_id,),
        )
    )

    result = ReviewApplicationService(db).apply_mapping(
        _mapping_command(prepared.mapping_ids[0], MappingReviewDecision.APPROVE)
    )

    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == run_count
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM analysis_run_outputs WHERE run_id=?",
            (prepared.run_id,),
        )
    ) == output_before
    audits = tuple(
        dict(row)
        for row in db.execute(
            "SELECT * FROM audit_events WHERE id > 0 ORDER BY id"
        )
    )
    serialized = json.dumps(audits, ensure_ascii=False, sort_keys=True)
    serialized_result = repr(asdict(result))
    for forbidden in (
        "Synthetic projection evidence",
        "transcript",
        "excerpt",
        "input_text",
        "audio_path",
        "prompt_body",
    ):
        assert forbidden not in serialized
        assert forbidden not in serialized_result
    assert db.execute(
        "SELECT COUNT(*) FROM audit_events "
        "WHERE entity_type='analysis_scope' AND entity_id=? "
        "AND operation='result_replaced'",
        (str(scope_id),),
    ).fetchone()[0] == 2


def test_application_rejects_noncanonical_accepted_history_before_review(db):
    prepared, _, _ = create_accepted_low_mapping_fixture(db, "review-history")
    accepted_at = db.execute(
        "SELECT created_at FROM analysis_run_events "
        "WHERE run_id=? AND status='accepted'",
        (prepared.run_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO analysis_run_events(run_id,status,safe_error_code,created_at) "
        "VALUES (?, 'accepted', NULL, ?)",
        (prepared.run_id, accepted_at),
    )

    with pytest.raises(DomainError):
        ReviewApplicationService(db).apply_mapping(
            _mapping_command(
                prepared.mapping_ids[0], MappingReviewDecision.APPROVE
            )
        )
    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0


def test_application_rejects_malformed_final_success_provenance(db):
    prepared, _, _ = create_accepted_low_mapping_fixture(
        db, "review-final-provenance"
    )
    db.execute("DROP TRIGGER job_unit_attempts_no_update")
    db.execute(
        """
        UPDATE job_unit_attempts
        SET output_hash=?
        WHERE job_id=? AND unit_key=? AND result_status='success'
        """,
        ("f" * 64, prepared.job_id, FINAL_PROMOTION_UNIT_KEY),
    )

    with pytest.raises(DomainError):
        ReviewApplicationService(db).apply_mapping(
            _mapping_command(
                prepared.mapping_ids[0], MappingReviewDecision.APPROVE
            )
        )

    assert db.execute("SELECT COUNT(*) FROM mapping_reviews").fetchone()[0] == 0
