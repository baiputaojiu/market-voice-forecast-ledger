import sqlite3
from dataclasses import asdict

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    Confidence,
    MappingReviewDecision,
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
from market_voice_forecast_ledger.services.codex_contract import CODEX_BATCH_UNIT_KEY
from tests.backend.integration.test_analysis_input_boundaries import (
    _analysis_manifest,
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


def test_newest_failed_run_event_is_rejected_but_older_failure_after_retry_is_allowed(
    db, monkeypatch
):
    prepared, batch = _completed_run(db, label="run-events")
    _replace(db, prepared, batch)
    repository = AnalysisRepository(db)
    with transaction(db):
        repository.append_run_event(
            prepared.run_id, AnalysisRunStatus.FAILED, "synthetic_failure"
        )
    service = CurrentResultService(db)
    monkeypatch.setattr(
        service,
        "_delete_scope_rows",
        lambda *args: pytest.fail("failed run reached deletion"),
    )
    with pytest.raises(DomainError):
        with transaction(db):
            service._replace_scope_rows_in_transaction(prepared.run_id, batch.id)

    monkeypatch.undo()
    with transaction(db):
        repository.append_run_event(
            prepared.run_id, AnalysisRunStatus.TRANSPORT_VALIDATED, None
        )
    _replace(db, prepared, batch)


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


def test_valid_successor_can_finish_a_pending_upstream_unit_then_replace(db):
    prepared = _prepare_upstream(
        db, (StatementSpec("successor-finishes-projection", NEWER),)
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

    result = _replace(db, prepared, batch)

    assert FORECAST_PROJECTION_UNIT_KEY not in plan.reused_unit_keys
    assert result.after.source_run_id == prepared.run_id
    assert result.after.projection_batch_id == batch.id


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

    MappingReviewService(db).review(
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


def test_task_14_exposes_no_public_current_result_mutation():
    public_callables = {
        name
        for name in dir(CurrentResultService)
        if not name.startswith("_")
        and callable(getattr(CurrentResultService, name))
    }

    assert public_callables == {"get_scope"}
