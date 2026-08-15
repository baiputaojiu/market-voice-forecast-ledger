import json
import sqlite3
from dataclasses import asdict

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    Confidence,
    MappingReviewDecision,
    PeriodReviewDecision,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.forecasts import ProjectionTrigger
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    ASSET_MAPPING_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from market_voice_forecast_ledger.services.asset_mapping import AssetMappingService
from market_voice_forecast_ledger.services.current_results import (
    CurrentResultDelta,
    CurrentResultService,
    CurrentResultSummary,
)
from market_voice_forecast_ledger.services.forecast_projection import (
    ForecastProjectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.mapping_review import (
    MappingReviewCommand,
    MappingReviewService,
)
from market_voice_forecast_ledger.services.codex_contract import (
    CODEX_BATCH_UNIT_KEY,
    CodexContractService,
    CodexRunReceipt,
)
from market_voice_forecast_ledger.services.periods import (
    PeriodReviewService,
    PeriodService,
)
from market_voice_forecast_ledger.services.statements import StatementService
from tests.backend.integration.test_analysis_input_boundaries import (
    _analysis_manifest,
    _begin,
    _prepare_personal_analysis,
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


def test_internal_current_writer_requires_a_caller_owned_transaction(db):
    prepared = _prepare_upstream(db, (StatementSpec("transaction", NEWER),))
    batch = _project(db, prepared)

    with pytest.raises(DomainError) as error:
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )

    assert error.value.code == "CURRENT_REPLACEMENT_TRANSACTION_REQUIRED"


def test_current_result_types_are_immutable_dataclasses():
    assert CurrentResultSummary.__dataclass_params__.frozen is True
    assert CurrentResultDelta.__dataclass_params__.frozen is True


def _completed_run(db, *, confidence=Confidence.HIGH, label="current"):
    prepared = _prepare_upstream(
        db,
        (
            StatementSpec(
                label,
                NEWER,
                confidence=confidence,
            ),
        ),
    )
    return prepared, _project(db, prepared)


def _completed_run_after_actual_codex_retry(db):
    prepared_input = _prepare_personal_analysis(db)
    run = _begin(db, prepared_input)
    jobs = JobStateService(db)
    segment = AnalysisRepository(db).get_input_segments(run.id)[0]
    output_json = json.dumps(
        {
            "run_id": run.id,
            "batch_key": CODEX_BATCH_UNIT_KEY,
            "statements": [
                {
                    "statement_type": "future_forecast",
                    "forecast_basis": "direct",
                    "condition_kind": "unconditional",
                    "condition_text": None,
                    "direction_kind": "up",
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
                            "segment_id": segment.segment_id,
                            "excerpt": "Synthetic subject evidence.",
                        }
                    ],
                }
            ],
        }
    )
    jobs.begin_unit(prepared_input.job_id, CODEX_BATCH_UNIT_KEY)
    with pytest.raises(DomainError) as failed:
        CodexContractService(db).validate_and_store(
            run.id,
            CODEX_BATCH_UNIT_KEY,
            output_json,
            CodexRunReceipt(
                "gpt-5.6-sol", "max", 1, "stored_statements_only"
            ),
        )
    assert failed.value.code == "CODEX_TOOL_CALL_DETECTED"
    input_hash = jobs.unit(
        prepared_input.job_id, ANALYSIS_INPUT_UNIT_KEY
    ).output_hash
    jobs.resume(
        prepared_input.job_id, {ANALYSIS_INPUT_UNIT_KEY: input_hash}
    )
    jobs.begin_unit(prepared_input.job_id, CODEX_BATCH_UNIT_KEY)
    CodexContractService(db).validate_and_store(
        run.id,
        CODEX_BATCH_UNIT_KEY,
        output_json,
        _valid_receipt(),
    )
    jobs.begin_unit(prepared_input.job_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    statements = StatementService(db).normalize_and_store(run.id)
    jobs.begin_unit(prepared_input.job_id, PERIOD_NORMALIZATION_UNIT_KEY)
    periods = PeriodService(db).normalize_run(run.id)
    jobs.begin_unit(prepared_input.job_id, ASSET_MAPPING_UNIT_KEY)
    mappings = AssetMappingService(db).map_run(run.id)
    prepared = PreparedProjection(
        run.id,
        prepared_input.job_id,
        tuple(row.id for row in statements),
        tuple(row.id for row in periods),
        tuple(row.id for row in mappings),
        (segment.video_id,),
    )
    return prepared, _project(db, prepared)


def _scope_id(db, run_id):
    return AnalysisRepository(db).get_run(run_id).scope_id


def _replace(db, prepared, batch):
    with transaction(db):
        return CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )


def _scope_bytes(db, scope_id):
    tables = (
        "current_result_sets",
        "current_statements",
        "current_asset_mappings",
        "current_forecasts",
    )
    return tuple(
        (
            table,
            tuple(
                tuple(row)
                for row in db.execute(
                    f"SELECT * FROM {table} WHERE scope_id=? ORDER BY 1, 2",
                    (scope_id,),
                )
            ),
        )
        for table in tables
    )


@pytest.mark.parametrize(
    "failure_point",
    (
        "delete",
        "statements",
        "mappings",
        "forecasts",
        "final_summary",
    ),
)
def test_each_internal_fault_rolls_back_old_rows_and_scope_after_reopen(
    tmp_path, monkeypatch, failure_point
):
    db_path = tmp_path / "ledger.sqlite3"
    db = open_database(db_path)
    apply_migrations(db)
    prepared, batch = _completed_run(db, label=f"fault-{failure_point}")
    scope_id = _scope_id(db, prepared.run_id)
    _replace(db, prepared, batch)
    before_summary = CurrentResultService(db).get_scope(scope_id)
    before_rows = _scope_bytes(db, scope_id)
    before_scope = dict(
        db.execute("SELECT * FROM analysis_scopes WHERE id=?", (scope_id,)).fetchone()
    )

    service = CurrentResultService(db)
    method_by_point = {
        "delete": "_delete_scope_rows",
        "statements": "_copy_statements",
        "mappings": "_copy_mappings",
        "forecasts": "_copy_forecasts",
    }
    if failure_point == "final_summary":
        original = service._summarize_scope
        calls = 0

        def fail_second_summary(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic final summary failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "_summarize_scope", fail_second_summary)
    else:
        method_name = method_by_point[failure_point]
        original = getattr(service, method_name)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError(f"synthetic {failure_point} failure")

        monkeypatch.setattr(service, method_name, fail)

    with pytest.raises(sqlite3.OperationalError, match="synthetic"):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)
    db.close()

    reopened = open_database(db_path)
    try:
        assert CurrentResultService(reopened).get_scope(scope_id) == before_summary
        assert _scope_bytes(reopened, scope_id) == before_rows
        assert dict(
            reopened.execute(
                "SELECT * FROM analysis_scopes WHERE id=?", (scope_id,)
            ).fetchone()
        ) == before_scope
    finally:
        reopened.close()


def test_successful_internal_writer_does_not_commit_the_callers_transaction(
    tmp_path,
):
    db_path = tmp_path / "ledger.sqlite3"
    db = open_database(db_path)
    apply_migrations(db)
    prepared, batch = _completed_run(db, label="caller-rollback")
    scope_id = _scope_id(db, prepared.run_id)

    with pytest.raises(RuntimeError, match="caller rollback"):
        with transaction(db):
            result = CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )
            assert result.after.statement_count == 1
            raise RuntimeError("caller rollback")
    db.close()

    reopened = open_database(db_path)
    try:
        summary = CurrentResultService(reopened).get_scope(scope_id)
        assert summary.source_run_id is None
        assert summary.projection_batch_id is None
        assert summary.statement_count == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("final_state", (UnitStatus.PENDING, UnitStatus.RUNNING))
def test_pending_or_running_final_unit_is_allowed_and_left_unchanged(
    db, final_state
):
    prepared, batch = _completed_run(db, label=f"final-{final_state.value}")
    jobs = JobStateService(db)
    if final_state is UnitStatus.RUNNING:
        jobs.begin_unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    unit_before = jobs.unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    job_before = jobs.status(prepared.job_id)
    scope_before = AnalysisRepository(db).get_scope(
        _scope_id(db, prepared.run_id)
    )
    events_before = db.execute(
        "SELECT COUNT(*) FROM analysis_run_events WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()[0]
    audit_before = db.execute(
        "SELECT COUNT(*) FROM audit_events"
    ).fetchone()[0]

    result = _replace(db, prepared, batch)

    assert result.after.forecast_count == 1
    assert jobs.unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY) == unit_before
    assert jobs.status(prepared.job_id) is job_before
    assert AnalysisRepository(db).get_scope(scope_before.id) == scope_before
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events WHERE run_id=?",
        (prepared.run_id,),
    ).fetchone()[0] == events_before
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == (
        audit_before
    )


def test_pending_final_unit_rejects_a_stale_prebound_input_before_deletion(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="stale-final-binding")
    db.execute(
        """
        UPDATE job_units
        SET bound_input_hash=?
        WHERE job_id=? AND unit_key=?
        """,
        ("f" * 64, prepared.job_id, FINAL_PROMOTION_UNIT_KEY),
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("stale final binding reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


@pytest.mark.parametrize("final_state", (UnitStatus.SUCCESS, UnitStatus.FAILED))
def test_successful_or_failed_final_unit_is_rejected_before_deletion(
    db, monkeypatch, final_state
):
    prepared, batch = _completed_run(db, label=f"invalid-{final_state.value}")
    _replace(db, prepared, batch)
    jobs = JobStateService(db)
    jobs.begin_unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    if final_state is UnitStatus.SUCCESS:
        with transaction(db):
            jobs.complete_unit_in_transaction(
                prepared.job_id, FINAL_PROMOTION_UNIT_KEY, "synthetic-final-output"
            )
    else:
        jobs.fail_unit(
            prepared.job_id, FINAL_PROMOTION_UNIT_KEY, "synthetic_final_failure"
        )
    before = _scope_bytes(db, _scope_id(db, prepared.run_id))
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("validation reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)

    assert _scope_bytes(db, _scope_id(db, prepared.run_id)) == before


def test_stored_manifest_total_must_match_the_exact_seven_unit_graph(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="manifest-total")
    db.execute("DROP TRIGGER jobs_manifest_immutable")
    db.execute(
        "UPDATE jobs SET total_units=8 WHERE id=?", (prepared.job_id,)
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("invalid manifest total reached deletion"),
    )

    with pytest.raises(DomainError) as error:
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )

    assert error.value.code == "STORED_MANIFEST_MISMATCH"


_ARTIFACT_UNITS = {
    "input": ANALYSIS_INPUT_UNIT_KEY,
    "codex": CODEX_BATCH_UNIT_KEY,
    "statements": STATEMENT_NORMALIZATION_UNIT_KEY,
    "periods": PERIOD_NORMALIZATION_UNIT_KEY,
    "mappings": ASSET_MAPPING_UNIT_KEY,
    "forecasts": FORECAST_PROJECTION_UNIT_KEY,
}


def _tamper_content(db, prepared, batch, artifact):
    if artifact == "input":
        db.execute("DROP TRIGGER analysis_input_snapshots_limited_update")
        db.execute(
            "UPDATE analysis_input_snapshots SET input_sha256=? WHERE run_id=?",
            ("e" * 64, prepared.run_id),
        )
    elif artifact == "codex":
        db.execute("DROP TRIGGER analysis_run_outputs_no_update")
        db.execute(
            "UPDATE analysis_run_outputs SET canonical_output_json='{}' WHERE run_id=?",
            (prepared.run_id,),
        )
    elif artifact == "statements":
        db.execute("DROP TRIGGER analysis_statements_no_update")
        db.execute(
            "UPDATE analysis_statements SET original_target_expression='tampered' WHERE run_id=?",
            (prepared.run_id,),
        )
    elif artifact == "periods":
        db.execute("DROP TRIGGER analysis_statement_periods_no_update")
        db.execute(
            """
            UPDATE analysis_statement_periods
            SET source_expression='tampered'
            WHERE statement_id IN (
                SELECT id FROM analysis_statements WHERE run_id=?
            )
            """,
            (prepared.run_id,),
        )
    elif artifact == "mappings":
        db.execute("DROP TRIGGER analysis_asset_mappings_no_update")
        db.execute(
            "UPDATE analysis_asset_mappings SET conversion_reason='tampered' WHERE run_id=?",
            (prepared.run_id,),
        )
    else:
        db.execute("DROP TRIGGER analysis_forecasts_no_update")
        db.execute(
            "UPDATE analysis_forecasts SET stable_selection_key=stable_selection_key || ':tampered' WHERE projection_batch_id=?",
            (batch.id,),
        )


@pytest.mark.parametrize("artifact", tuple(_ARTIFACT_UNITS))
@pytest.mark.parametrize("tamper_kind", ("status", "hash", "content"))
def test_every_upstream_artifact_status_hash_and_content_is_revalidated(
    db, monkeypatch, artifact, tamper_kind
):
    prepared, batch = _completed_run(
        db, label=f"tamper-{artifact}-{tamper_kind}"
    )
    _replace(db, prepared, batch)
    unit_key = _ARTIFACT_UNITS[artifact]
    if tamper_kind == "status":
        db.execute(
            "UPDATE job_units SET status='pending' WHERE job_id=? AND unit_key=?",
            (prepared.job_id, unit_key),
        )
    elif tamper_kind == "hash":
        db.execute(
            "UPDATE job_units SET output_hash=? WHERE job_id=? AND unit_key=?",
            ("f" * 64, prepared.job_id, unit_key),
        )
    else:
        _tamper_content(db, prepared, batch, artifact)
    scope_id = _scope_id(db, prepared.run_id)
    before = _scope_bytes(db, scope_id)
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("artifact validation reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)

    assert _scope_bytes(db, scope_id) == before


def test_missing_codex_transport_output_is_rejected_before_deletion(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="missing-codex")
    _replace(db, prepared, batch)
    db.execute("DROP TRIGGER analysis_run_outputs_no_delete")
    db.execute(
        "DELETE FROM analysis_run_outputs WHERE run_id=?", (prepared.run_id,)
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("Codex count validation reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)


def test_actual_codex_failure_then_retry_has_valid_promotion_provenance(db):
    prepared, batch = _completed_run_after_actual_codex_retry(db)

    result = _replace(db, prepared, batch)

    assert result.after.source_run_id == prepared.run_id
    assert tuple(
        row["status"]
        for row in db.execute(
            """
            SELECT status
            FROM analysis_run_events
            WHERE run_id=?
            ORDER BY id
            """,
            (prepared.run_id,),
        )
    ) == ("started", "failed", "transport_validated")


def _append_raw_run_statuses(db, run_id, statuses):
    output_created_at = db.execute(
        "SELECT created_at FROM analysis_run_outputs WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    for status in statuses:
        db.execute(
            """
            INSERT INTO analysis_run_events(
                run_id, status, safe_error_code, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                status,
                "synthetic_failure" if status == "failed" else None,
                output_created_at,
            ),
        )


@pytest.mark.parametrize(
    "statuses",
    (
        pytest.param(("failed", "started"), id="failed-then-started"),
        pytest.param(
            ("started", "transport_validated"),
            id="duplicate-started-before-transport",
        ),
        pytest.param(
            ("accepted", "transport_validated"),
            id="accepted-before-transport",
        ),
        pytest.param(("transport_validated",), id="extra-transport"),
    ),
)
def test_noncanonical_run_event_history_is_rejected_before_deletion(
    db, monkeypatch, statuses
):
    prepared, batch = _completed_run(db, label=f"run-history-{statuses[0]}")
    _append_raw_run_statuses(db, prepared.run_id, statuses)
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("invalid run history reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


def test_transport_event_time_must_match_the_immutable_output(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="transport-time")
    db.execute("DROP TRIGGER analysis_run_events_no_update")
    db.execute(
        """
        UPDATE analysis_run_events
        SET created_at='2000-01-01T00:00:00.000000Z'
        WHERE run_id=? AND status='transport_validated'
        """,
        (prepared.run_id,),
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("mismatched transport time reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


def _tamper_codex_success_attempts(db, prepared, tamper_kind):
    if tamper_kind == "missing":
        db.execute("DROP TRIGGER job_unit_attempts_no_delete")
        db.execute(
            """
            DELETE FROM job_unit_attempts
            WHERE job_id=? AND unit_key=?
            """,
            (prepared.job_id, CODEX_BATCH_UNIT_KEY),
        )
        return
    if tamper_kind in {"gap", "hash"}:
        db.execute("DROP TRIGGER job_unit_attempts_no_update")
        column = "attempt_no" if tamper_kind == "gap" else "output_hash"
        value = 2 if tamper_kind == "gap" else "f" * 64
        db.execute(
            f"""
            UPDATE job_unit_attempts
            SET {column}=?
            WHERE job_id=? AND unit_key=?
            """,
            (value, prepared.job_id, CODEX_BATCH_UNIT_KEY),
        )
        return

    db.execute(
        """
        UPDATE job_units
        SET attempt_count=2
        WHERE job_id=? AND unit_key=?
        """,
        (prepared.job_id, CODEX_BATCH_UNIT_KEY),
    )
    output_hash = JobStateService(db).unit(
        prepared.job_id, CODEX_BATCH_UNIT_KEY
    ).output_hash
    is_success = tamper_kind == "multiple_success"
    db.execute(
        """
        INSERT INTO job_unit_attempts(
            job_id, unit_key, attempt_no, result_status, output_hash,
            error_code, started_at, finished_at
        ) VALUES (?, ?, 2, ?, ?, ?, ?, ?)
        """,
        (
            prepared.job_id,
            CODEX_BATCH_UNIT_KEY,
            "success" if is_success else "failed",
            output_hash if is_success else None,
            None if is_success else "synthetic_failure",
            "2026-08-15T00:00:00.000000Z",
            "2026-08-15T00:00:01.000000Z",
        ),
    )


@pytest.mark.parametrize(
    "tamper_kind",
    ("missing", "gap", "hash", "multiple_success", "success_not_final"),
)
def test_codex_output_origin_requires_exact_success_attempt_history(
    db, monkeypatch, tamper_kind
):
    prepared, batch = _completed_run(
        db, label=f"codex-attempt-{tamper_kind}"
    )
    _tamper_codex_success_attempts(db, prepared, tamper_kind)
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("invalid Codex attempt reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


def _success_artifacts(db, job_id):
    return {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            "SELECT unit_key, output_hash FROM job_units WHERE job_id=? AND status='success'",
            (job_id,),
        )
    }


def _external_inputs(db, job_id):
    return {
        row["unit_key"]: row["external_input_hash"]
        for row in db.execute(
            "SELECT unit_key, external_input_hash FROM job_units WHERE job_id=?",
            (job_id,),
        )
    }


def _attach_reusing_successor(db, prepared):
    run = AnalysisRepository(db).get_run(prepared.run_id)
    JobStateService(db).request_stop(prepared.job_id)
    manifest = _analysis_manifest(run.input_contract_hash, run.settings)
    successor_id, plan = JobStateService(db).create_successor(
        prepared.job_id,
        manifest,
        _success_artifacts(db, prepared.job_id),
        _external_inputs(db, prepared.job_id),
    )
    AnalysisRunService(db).attach_successor(prepared.run_id, successor_id)
    return successor_id, plan


def test_valid_active_successor_can_promote_reused_durable_outputs(db):
    prepared, batch = _completed_run(db, label="successor-reuse")
    successor_id, plan = _attach_reusing_successor(db, prepared)

    result = _replace(db, prepared, batch)

    assert FORECAST_PROJECTION_UNIT_KEY in plan.reused_unit_keys
    assert AnalysisRepository(db).get_active_job_id(prepared.run_id) == successor_id
    assert result.after.source_run_id == prepared.run_id


def _successor_executes_projection(db, label):
    prepared = _prepare_upstream(
        db, (StatementSpec(label, NEWER),)
    )
    successor_id, plan = _attach_reusing_successor(db, prepared)
    projection = ForecastProjectionService(db)
    JobStateService(db).begin_unit(
        successor_id,
        FORECAST_PROJECTION_UNIT_KEY,
        projection.effective_review_state_hash(prepared.run_id),
    )
    batch = projection.project_run(
        prepared.run_id, ProjectionTrigger.INITIAL
    )
    return prepared, successor_id, plan, batch


def test_valid_successor_can_finish_a_pending_upstream_unit_then_replace(db):
    prepared, _, plan, batch = _successor_executes_projection(
        db, "successor-finishes-projection"
    )

    result = _replace(db, prepared, batch)

    assert FORECAST_PROJECTION_UNIT_KEY not in plan.reused_unit_keys
    assert result.after.source_run_id == prepared.run_id
    assert result.after.projection_batch_id == batch.id


def test_valid_successor_can_rerun_a_verified_empty_statement_artifact(db):
    prepared_input = _prepare_personal_analysis(db)
    run = _begin(db, prepared_input)
    jobs = JobStateService(db)
    jobs.begin_unit(prepared_input.job_id, CODEX_BATCH_UNIT_KEY)
    CodexContractService(db).validate_and_store(
        run.id,
        CODEX_BATCH_UNIT_KEY,
        json.dumps(
            {
                "run_id": run.id,
                "batch_key": CODEX_BATCH_UNIT_KEY,
                "statements": [],
            }
        ),
        _valid_receipt(),
    )
    predecessor = PreparedProjection(
        run.id, prepared_input.job_id, (), (), (), ()
    )
    successor_id, successor_plan = _attach_reusing_successor(db, predecessor)
    assert STATEMENT_NORMALIZATION_UNIT_KEY not in successor_plan.reused_unit_keys

    jobs.begin_unit(successor_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    assert StatementService(db).normalize_and_store(run.id) == ()
    first_output_hash = jobs.unit(
        successor_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).output_hash
    verified_artifacts = _success_artifacts(db, successor_id)
    verified_artifacts[STATEMENT_NORMALIZATION_UNIT_KEY] = (
        "mismatched-statement-artifact"
    )

    resume_plan = jobs.resume(successor_id, verified_artifacts)

    assert STATEMENT_NORMALIZATION_UNIT_KEY in resume_plan.pending_unit_keys
    assert jobs.unit(
        successor_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).status is UnitStatus.PENDING
    jobs.begin_unit(successor_id, STATEMENT_NORMALIZATION_UNIT_KEY)
    assert StatementService(db).normalize_and_store(run.id) == ()
    second_output_hash = jobs.unit(
        successor_id, STATEMENT_NORMALIZATION_UNIT_KEY
    ).output_hash
    assert second_output_hash == first_output_hash

    attempts = tuple(
        tuple(row)
        for row in db.execute(
            """
            SELECT attempt_no, result_status, output_hash
            FROM job_unit_attempts
            WHERE job_id=? AND unit_key=?
            ORDER BY attempt_no
            """,
            (successor_id, STATEMENT_NORMALIZATION_UNIT_KEY),
        )
    )
    assert attempts == (
        (1, "success", first_output_hash),
        (2, "success", second_output_hash),
    )
    events = tuple(
        (row["event_kind"], json.loads(row["metadata_json"]))
        for row in db.execute(
            """
            SELECT event_kind, metadata_json
            FROM job_events
            WHERE job_id=? AND unit_key=?
            ORDER BY id
            """,
            (successor_id, STATEMENT_NORMALIZATION_UNIT_KEY),
        )
    )
    assert tuple(kind for kind, _ in events) == (
        "unit_started",
        "unit_succeeded",
        "unit_reset",
        "unit_started",
        "unit_succeeded",
    )
    assert events[2][1] == {
        "attempt_no": 1,
        "reason": "verification_mismatch",
    }

    jobs.begin_unit(successor_id, PERIOD_NORMALIZATION_UNIT_KEY)
    periods = PeriodService(db).normalize_run(run.id)
    jobs.begin_unit(successor_id, ASSET_MAPPING_UNIT_KEY)
    mappings = AssetMappingService(db).map_run(run.id)
    prepared = PreparedProjection(
        run.id,
        successor_id,
        (),
        tuple(row.id for row in periods),
        tuple(row.id for row in mappings),
        (),
    )
    batch = _project(db, prepared)

    result = _replace(db, prepared, batch)

    assert result.after.source_run_id == run.id
    assert result.after.projection_batch_id == batch.id
    assert result.after.statement_count == 0


def _insert_raw_attempt(
    db,
    job_id,
    unit_key,
    attempt_no,
    result_status,
    output_hash=None,
):
    db.execute(
        """
        INSERT INTO job_unit_attempts(
            job_id, unit_key, attempt_no, result_status, output_hash,
            error_code, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            unit_key,
            attempt_no,
            result_status,
            output_hash,
            "synthetic_failure" if result_status == "failed" else None,
            "2026-08-15T00:00:00.000000Z",
            "2026-08-15T00:00:01.000000Z",
        ),
    )


def test_predecessor_success_requires_reuse_not_fabricated_active_execution(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="predecessor-success")
    successor_id, _ = _attach_reusing_successor(db, prepared)
    output_hash = JobStateService(db).unit(
        successor_id, FORECAST_PROJECTION_UNIT_KEY
    ).output_hash
    db.execute("DROP TRIGGER job_events_no_delete")
    db.execute(
        """
        DELETE FROM job_events
        WHERE job_id=? AND unit_key=? AND event_kind='unit_reused'
        """,
        (successor_id, FORECAST_PROJECTION_UNIT_KEY),
    )
    db.execute(
        """
        UPDATE job_units
        SET attempt_count=1
        WHERE job_id=? AND unit_key=?
        """,
        (successor_id, FORECAST_PROJECTION_UNIT_KEY),
    )
    _insert_raw_attempt(
        db,
        successor_id,
        FORECAST_PROJECTION_UNIT_KEY,
        1,
        "success",
        output_hash,
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("fabricated execution reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


def test_reused_successor_unit_requires_zero_attempt_rows(db, monkeypatch):
    prepared, batch = _completed_run(db, label="reuse-attempt-row")
    successor_id, _ = _attach_reusing_successor(db, prepared)
    _insert_raw_attempt(
        db,
        successor_id,
        FORECAST_PROJECTION_UNIT_KEY,
        1,
        "failed",
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("reused unit attempt row reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


@pytest.mark.parametrize("tamper_kind", ("extra_after_success", "gap"))
def test_active_successor_execution_requires_complete_attempt_history(
    db, monkeypatch, tamper_kind
):
    prepared, successor_id, _, batch = _successor_executes_projection(
        db, f"active-attempts-{tamper_kind}"
    )
    if tamper_kind == "extra_after_success":
        _insert_raw_attempt(
            db,
            successor_id,
            FORECAST_PROJECTION_UNIT_KEY,
            2,
            "failed",
        )
    else:
        db.execute("DROP TRIGGER job_unit_attempts_no_update")
        db.execute(
            """
            UPDATE job_unit_attempts
            SET attempt_no=3
            WHERE job_id=? AND unit_key=?
            """,
            (successor_id, FORECAST_PROJECTION_UNIT_KEY),
        )
        db.execute(
            """
            UPDATE job_units
            SET attempt_count=3
            WHERE job_id=? AND unit_key=?
            """,
            (successor_id, FORECAST_PROJECTION_UNIT_KEY),
        )
        _insert_raw_attempt(
            db,
            successor_id,
            FORECAST_PROJECTION_UNIT_KEY,
            1,
            "failed",
        )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("invalid active attempts reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )


def test_successor_reuse_requires_the_exact_immediate_predecessor_output(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="successor-exact-reuse")
    _attach_reusing_successor(db, prepared)
    db.execute(
        """
        UPDATE job_units
        SET output_hash=?
        WHERE job_id=? AND unit_key=?
        """,
        ("f" * 64, prepared.job_id, ANALYSIS_INPUT_UNIT_KEY),
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("invalid reuse reached deletion"),
    )

    with pytest.raises(DomainError) as error:
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )

    assert error.value.code == "CURRENT_REPLACEMENT_VALIDATION_FAILED"


def test_an_older_job_success_cannot_substitute_for_active_success_provenance(
    db, monkeypatch
):
    prepared = _prepare_upstream(
        db, (StatementSpec("active-success-provenance", NEWER),)
    )
    successor_id, _ = _attach_reusing_successor(db, prepared)
    projection = ForecastProjectionService(db)
    JobStateService(db).begin_unit(
        successor_id,
        FORECAST_PROJECTION_UNIT_KEY,
        projection.effective_review_state_hash(prepared.run_id),
    )
    batch = projection.project_run(
        prepared.run_id, ProjectionTrigger.INITIAL
    )
    output_hash = JobStateService(db).unit(
        successor_id, FORECAST_PROJECTION_UNIT_KEY
    ).output_hash
    db.execute("DROP TRIGGER job_unit_attempts_no_delete")
    db.execute(
        """
        DELETE FROM job_unit_attempts
        WHERE job_id=? AND unit_key=? AND result_status='success'
        """,
        (successor_id, FORECAST_PROJECTION_UNIT_KEY),
    )
    db.execute(
        """
        INSERT INTO job_unit_attempts(
            job_id, unit_key, attempt_no, result_status, output_hash,
            error_code, started_at, finished_at
        ) VALUES (?, ?, 1, 'success', ?, NULL, ?, ?)
        """,
        (
            prepared.job_id,
            FORECAST_PROJECTION_UNIT_KEY,
            output_hash,
            "2026-08-15T00:00:00.000000Z",
            "2026-08-15T00:00:01.000000Z",
        ),
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("older success reached deletion"),
    )

    with pytest.raises(DomainError) as error:
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, batch.id
            )

    assert error.value.code == "CURRENT_REPLACEMENT_VALIDATION_FAILED"


def test_a_superseded_successful_attempt_cannot_hide_invalid_active_attempt(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="superseded-attempt")
    successor_id, _ = _attach_reusing_successor(db, prepared)
    db.execute(
        "UPDATE job_units SET status='pending' WHERE job_id=? AND unit_key=?",
        (successor_id, FORECAST_PROJECTION_UNIT_KEY),
    )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("superseded attempt reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)


def test_projection_batch_must_be_run_owned_latest_and_link_complete(
    db, monkeypatch
):
    first, first_batch = _completed_run(db, label="batch-owner-a")
    second, second_batch = _completed_run(db, label="batch-owner-b")
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("foreign batch reached deletion"),
    )
    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(first.run_id, second_batch.id)

    monkeypatch.undo()
    db.execute("DROP TRIGGER analysis_forecast_statement_links_no_update")
    db.execute(
        "UPDATE analysis_forecast_statement_links SET ordinal=2 WHERE forecast_id=?",
        (first_batch.forecasts[0].id,),
    )
    with pytest.raises(DomainError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                first.run_id, first_batch.id
            )


@pytest.mark.parametrize("after_review", (False, True))
def test_review_trigger_requires_its_review_head_to_advance(
    db, monkeypatch, after_review
):
    prepared, initial = _completed_run(
        db,
        confidence=(Confidence.LOW if after_review else Confidence.HIGH),
        label=f"duplicate-review-{after_review}",
    )
    if after_review:
        MappingReviewService(db).review(
            MappingReviewCommand(
                prepared.mapping_ids[0],
                MappingReviewDecision.APPROVE,
                "user",
                "Synthetic first mapping review",
                None,
            )
        )
        with transaction(db):
            ForecastProjectionService(db)._project_run_in_transaction(
                prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
            )
    with transaction(db):
        duplicate = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )
    assert duplicate.id > initial.id
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("duplicate review batch reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, duplicate.id
            )


def test_review_batch_trigger_must_match_the_head_that_advanced(
    db, monkeypatch
):
    prepared, _ = _completed_run(
        db, confidence=Confidence.LOW, label="wrong-review-trigger"
    )
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.APPROVE,
            "user",
            "Synthetic mapping review under wrong trigger",
            None,
        )
    )
    with transaction(db):
        wrong_trigger = (
            ForecastProjectionService(db)._project_run_in_transaction(
                prepared.run_id, ProjectionTrigger.PERIOD_REVIEW
            )
        )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("wrong review trigger reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, wrong_trigger.id
            )


@pytest.mark.parametrize(
    "trigger_kind",
    (ProjectionTrigger.MAPPING_REVIEW, ProjectionTrigger.PERIOD_REVIEW),
)
def test_one_review_batch_cannot_advance_both_review_heads(
    db, monkeypatch, trigger_kind
):
    prepared, _ = _completed_run(
        db, confidence=Confidence.LOW, label=f"two-heads-{trigger_kind.value}"
    )
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.APPROVE,
            "user",
            "Synthetic simultaneous mapping review",
            None,
        )
    )
    PeriodReviewService(db).review(
        prepared.period_ids[0],
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic simultaneous period review",
    )
    with transaction(db):
        simultaneous = (
            ForecastProjectionService(db)._project_run_in_transaction(
                prepared.run_id, trigger_kind
            )
        )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("two review heads reached deletion"),
    )

    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(
                prepared.run_id, simultaneous.id
            )


def test_period_review_batch_with_only_a_period_head_advance_is_valid(db):
    prepared, _ = _completed_run(db, label="valid-period-lineage")
    PeriodReviewService(db).review(
        prepared.period_ids[0],
        PeriodReviewDecision.REJECT,
        "user",
        "Synthetic valid period review",
    )
    with transaction(db):
        reviewed = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.PERIOD_REVIEW
        )

    result = _replace(db, prepared, reviewed)

    assert result.after.projection_batch_id == reviewed.id
    assert result.after.forecast_count == 0


def _reviewed_batch(db):
    prepared, initial = _completed_run(
        db, confidence=Confidence.LOW, label="review-batch"
    )
    assert initial.forecasts == ()
    MappingReviewService(db).review(
        MappingReviewCommand(
            prepared.mapping_ids[0],
            MappingReviewDecision.APPROVE,
            "user",
            "Synthetic current-result approval",
            None,
        )
    )
    with transaction(db):
        reviewed = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )
    return prepared, initial, reviewed


def test_review_batch_uses_initial_unit_hash_and_must_be_newest_current_head(db):
    prepared, initial, reviewed = _reviewed_batch(db)

    result = _replace(db, prepared, reviewed)

    assert result.after.projection_batch_id == reviewed.id
    assert result.after.forecast_count == 1
    with pytest.raises(DomainError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, initial.id
            )

    with transaction(db):
        MappingReviewService(db)._review_in_transaction(
            MappingReviewCommand(
                prepared.mapping_ids[0],
                MappingReviewDecision.REJECT,
                "user",
                "Synthetic newer rejection",
                None,
            )
        )
    with pytest.raises(DomainError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, reviewed.id
            )


def test_older_review_batch_and_structurally_incomplete_latest_batch_fail(db):
    prepared, _, reviewed = _reviewed_batch(db)
    with transaction(db):
        newest = ForecastProjectionService(db)._project_run_in_transaction(
            prepared.run_id, ProjectionTrigger.MAPPING_REVIEW
        )
    with pytest.raises(DomainError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, reviewed.id
            )

    db.execute("DROP TRIGGER analysis_forecast_statement_links_no_delete")
    db.execute("DROP TRIGGER analysis_forecasts_no_delete")
    db.execute(
        "DELETE FROM analysis_forecast_statement_links WHERE forecast_id=?",
        (newest.forecasts[0].id,),
    )
    db.execute(
        "DELETE FROM analysis_forecasts WHERE id=?", (newest.forecasts[0].id,)
    )
    with pytest.raises(DomainError):
        with transaction(db):
            CurrentResultService(db)._replace_scope_rows_in_transaction(
                prepared.run_id, newest.id
            )


def test_empty_validated_forecast_batch_is_a_valid_current_result(db):
    prepared, batch = _completed_run(
        db, confidence=Confidence.LOW, label="empty-forecast-batch"
    )
    assert batch.forecasts == ()

    result = _replace(db, prepared, batch)

    assert result.after.source_run_id == prepared.run_id
    assert result.after.projection_batch_id == batch.id
    assert result.after.statement_count == 1
    assert result.after.mapping_count == 1
    assert result.after.eligible_mapping_count == 0
    assert result.after.forecast_count == 0


def test_summary_is_deterministic_safe_and_contains_no_private_text(db):
    prepared, batch = _completed_run(db, label="safe-summary")

    result = _replace(db, prepared, batch)
    reread = CurrentResultService(db).get_scope(_scope_id(db, prepared.run_id))
    serialized = repr(asdict(reread))

    assert reread == result.after
    assert reread.statement_ids == tuple(sorted(reread.statement_ids))
    assert reread.mapping_ids == tuple(sorted(reread.mapping_ids))
    assert reread.forecast_ids == tuple(sorted(reread.forecast_ids))
    assert "Synthetic projection evidence" not in serialized
    assert "excerpt" not in serialized
    assert "input_text" not in serialized


def test_task_16_exposes_only_atomic_public_current_result_mutation():
    public_callables = {
        name
        for name in dir(CurrentResultService)
        if not name.startswith("_")
        and callable(getattr(CurrentResultService, name))
    }

    assert public_callables == {"get_scope", "promote_completed_run"}
