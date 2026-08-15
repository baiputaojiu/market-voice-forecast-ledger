import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AnalysisRunStatus,
    HeatmapGranularity,
    JobStatus,
    ScopeStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import FINAL_PROMOTION_UNIT_KEY
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.heatmap import HeatmapService
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.integration.test_atomic_result_replacement import (
    _completed_run,
    _scope_id,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _ready(db, label="promotion"):
    prepared, batch = _completed_run(db, label=label)
    scope_id = _scope_id(db, prepared.run_id)
    with transaction(db):
        CurrentResultService(db)._replace_scope_rows_in_transaction(
            prepared.run_id, batch.id
        )
    HeatmapService(db).rebuild_scope(scope_id)
    JobStateService(db).begin_unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    return prepared, batch, scope_id


def _successful_artifacts(db, job_id):
    return {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            "SELECT unit_key, output_hash FROM job_units "
            "WHERE job_id=? AND status='success'",
            (job_id,),
        )
    }


def _durable_state(db, scope_id, run_id, job_id):
    queries = (
        ("analysis_scopes", "SELECT * FROM analysis_scopes WHERE id=?", (scope_id,)),
        ("current_result_sets", "SELECT * FROM current_result_sets WHERE scope_id=?", (scope_id,)),
        ("current_statements", "SELECT * FROM current_statements WHERE scope_id=? ORDER BY 1,2", (scope_id,)),
        ("current_asset_mappings", "SELECT * FROM current_asset_mappings WHERE scope_id=? ORDER BY 1,2", (scope_id,)),
        ("current_forecasts", "SELECT * FROM current_forecasts WHERE scope_id=? ORDER BY 1,2", (scope_id,)),
        ("heatmap_cells", "SELECT * FROM heatmap_cells WHERE scope_id=? ORDER BY id", (scope_id,)),
        ("heatmap_cell_forecasts", "SELECT * FROM heatmap_cell_forecasts WHERE scope_id=? ORDER BY heatmap_cell_id,ordinal", (scope_id,)),
        ("analysis_run_events", "SELECT * FROM analysis_run_events WHERE run_id=? ORDER BY id", (run_id,)),
        ("audit_events", "SELECT * FROM audit_events WHERE entity_type='analysis_scope' AND entity_id=? ORDER BY id", (str(scope_id),)),
        ("jobs", "SELECT * FROM jobs WHERE id=?", (job_id,)),
        ("job_units", "SELECT * FROM job_units WHERE job_id=? ORDER BY ordinal", (job_id,)),
        ("job_unit_attempts", "SELECT * FROM job_unit_attempts WHERE job_id=? ORDER BY unit_key,attempt_no", (job_id,)),
    )
    return tuple(
        (name, tuple(tuple(row) for row in db.execute(sql, params)))
        for name, sql, params in queries
    )


def test_successful_promotion_commits_current_cache_acceptance_audit_and_job(db):
    prepared, batch, scope_id = _ready(db, "success")
    service = CurrentResultService(db)

    summary = service.promote_completed_run(prepared.run_id, batch.id)

    assert summary == service.get_scope(scope_id)
    assert summary.source_run_id == prepared.run_id
    assert summary.projection_batch_id == batch.id
    assert HeatmapService(db).read_scope(
        scope_id, HeatmapGranularity.WEEK
    ).rows[0].cells
    scope = AnalysisRepository(db).get_scope(scope_id)
    assert (scope.status, scope.stale_reason) == (ScopeStatus.CURRENT, None)
    assert JobStateService(db).status(prepared.job_id) is JobStatus.SUCCEEDED
    final = JobStateService(db).unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    assert final.status is UnitStatus.SUCCESS
    assert final.output_hash is not None and len(final.output_hash) == 64
    assert tuple(
        AnalysisRunStatus(row[0])
        for row in db.execute(
            "SELECT status FROM analysis_run_events WHERE run_id=? ORDER BY id",
            (prepared.run_id,),
        )
    )[-1] is AnalysisRunStatus.ACCEPTED
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events "
        "WHERE run_id=? AND status='accepted'",
        (prepared.run_id,),
    ).fetchone()[0] == 1
    audit = db.execute(
        "SELECT * FROM audit_events WHERE entity_type='analysis_scope' "
        "AND entity_id=? AND operation='result_replaced'",
        (str(scope_id),),
    ).fetchone()
    assert audit is not None
    safe = json.dumps(
        {"before": json.loads(audit["before_json"]), "after": json.loads(audit["after_json"])},
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
        assert forbidden not in safe


_FAILURE_TRIGGERS = {
    "current_delete": """
        CREATE TRIGGER synthetic_promotion_current_delete
        BEFORE DELETE ON current_result_sets
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "cache_delete": """
        CREATE TRIGGER synthetic_promotion_cache_delete
        BEFORE DELETE ON heatmap_cells
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "cache_insert": """
        CREATE TRIGGER synthetic_promotion_cache_insert
        BEFORE INSERT ON heatmap_cells
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "cache_link": """
        CREATE TRIGGER synthetic_promotion_cache_link
        BEFORE INSERT ON heatmap_cell_forecasts
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "scope_update": """
        CREATE TRIGGER synthetic_promotion_scope_update
        BEFORE UPDATE ON analysis_scopes
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "run_event": """
        CREATE TRIGGER synthetic_promotion_run_event
        BEFORE INSERT ON analysis_run_events
        WHEN NEW.status='accepted'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "audit": """
        CREATE TRIGGER synthetic_promotion_audit
        BEFORE INSERT ON audit_events
        WHEN NEW.entity_type='analysis_scope' AND NEW.operation='result_replaced'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "final_complete": """
        CREATE TRIGGER synthetic_promotion_final_complete
        BEFORE UPDATE ON job_units
        WHEN OLD.unit_key='heatmap:promote-current' AND NEW.status='success'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
    "job_success": """
        CREATE TRIGGER synthetic_promotion_job_success
        BEFORE UPDATE ON jobs
        WHEN NEW.status='succeeded'
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PROMOTION_FAILURE'); END
    """,
}


@pytest.mark.parametrize("failure_point", tuple(_FAILURE_TRIGGERS))
def test_every_promotion_boundary_rolls_back_after_close_reopen(
    tmp_path, failure_point
):
    db_path = tmp_path / "ledger.sqlite3"
    first = open_database(db_path)
    apply_migrations(first)
    prepared, batch, scope_id = _ready(first, f"rollback-{failure_point}")
    before = _durable_state(first, scope_id, prepared.run_id, prepared.job_id)
    first.execute(_FAILURE_TRIGGERS[failure_point])

    with pytest.raises((DomainError, sqlite3.Error)):
        CurrentResultService(first).promote_completed_run(
            prepared.run_id, batch.id
        )
    first.close()

    reopened = open_database(db_path)
    try:
        assert _durable_state(
            reopened, scope_id, prepared.run_id, prepared.job_id
        ) == before
        assert JobStateService(reopened).unit(
            prepared.job_id, FINAL_PROMOTION_UNIT_KEY
        ).status is UnitStatus.RUNNING
    finally:
        reopened.close()


def test_failed_python_promotion_requires_explicit_interrupted_recovery(tmp_path):
    db_path = tmp_path / "ledger.sqlite3"
    first = open_database(db_path)
    apply_migrations(first)
    prepared, batch, scope_id = _ready(first, "recovery")
    attempt_counts = tuple(
        row[0]
        for row in first.execute(
            "SELECT attempt_count FROM job_units WHERE job_id=? ORDER BY ordinal",
            (prepared.job_id,),
        )
    )
    first.execute(_FAILURE_TRIGGERS["cache_insert"])
    with pytest.raises((DomainError, sqlite3.Error)):
        CurrentResultService(first).promote_completed_run(prepared.run_id, batch.id)
    first.close()

    reopened = open_database(db_path)
    try:
        jobs = JobStateService(reopened)
        artifacts = _successful_artifacts(reopened, prepared.job_id)
        with pytest.raises(DomainError) as live:
            jobs.resume(prepared.job_id, artifacts)
        assert live.value.code == "INTERRUPTED_RECOVERY_REQUIRED"
        recovery = jobs.recover_interrupted(prepared.job_id, artifacts)
        assert recovery.next_unit_key == FINAL_PROMOTION_UNIT_KEY
        assert jobs.unit(
            prepared.job_id, FINAL_PROMOTION_UNIT_KEY
        ).status is UnitStatus.PENDING
        resume = jobs.resume(prepared.job_id, artifacts)
        assert resume.next_unit_key == FINAL_PROMOTION_UNIT_KEY
        jobs.begin_unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
        reopened.execute("DROP TRIGGER synthetic_promotion_cache_insert")

        result = CurrentResultService(reopened).promote_completed_run(
            prepared.run_id, batch.id
        )

        assert result.scope_id == scope_id
        assert JobStateService(reopened).status(prepared.job_id) is JobStatus.SUCCEEDED
        assert tuple(
            row[0]
            for row in reopened.execute(
                "SELECT attempt_count FROM job_units WHERE job_id=? ORDER BY ordinal",
                (prepared.job_id,),
            )
        ) == attempt_counts[:-1] + (attempt_counts[-1] + 1,)
        final_attempts = tuple(
            row[0]
            for row in reopened.execute(
                "SELECT result_status FROM job_unit_attempts "
                "WHERE job_id=? AND unit_key=? ORDER BY attempt_no",
                (prepared.job_id, FINAL_PROMOTION_UNIT_KEY),
            )
        )
        assert final_attempts == ("interrupted", "success")
    finally:
        reopened.close()


def test_stale_during_run_rejects_promotion_and_preserves_old_display(db):
    prepared, batch, scope_id = _ready(db, "stale-during-run")
    old_summary = CurrentResultService(db).get_scope(scope_id)
    old_view = asdict(
        HeatmapService(db).read_scope(scope_id, HeatmapGranularity.WEEK)
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

    with pytest.raises(DomainError) as error:
        CurrentResultService(db).promote_completed_run(prepared.run_id, batch.id)

    assert error.value.code == "CURRENT_PROMOTION_NOT_ALLOWED"
    scope = AnalysisRepository(db).get_scope(scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "SPEAKER_ASSIGNMENT_CHANGED",
    )
    assert CurrentResultService(db).get_scope(scope_id) == old_summary
    expected_view = old_view | {
        "scope_status": ScopeStatus.STALE,
        "stale_reason": "SPEAKER_ASSIGNMENT_CHANGED",
        "rows": tuple(
            row
            | {
                "scope_status": ScopeStatus.STALE,
                "stale_reason": "SPEAKER_ASSIGNMENT_CHANGED",
            }
            for row in old_view["rows"]
        ),
    }
    assert asdict(
        HeatmapService(db).read_scope(scope_id, HeatmapGranularity.WEEK)
    ) == expected_view
    assert JobStateService(db).unit(
        prepared.job_id, FINAL_PROMOTION_UNIT_KEY
    ).status is UnitStatus.RUNNING


def test_promotion_requires_final_exactly_running_and_exact_bound_input(db):
    prepared, batch = _completed_run(db, label="pending-final")
    scope_id = _scope_id(db, prepared.run_id)
    with pytest.raises(DomainError) as pending:
        CurrentResultService(db).promote_completed_run(prepared.run_id, batch.id)
    assert pending.value.code == "CURRENT_PROMOTION_NOT_ALLOWED"
    assert CurrentResultService(db).get_scope(scope_id).source_run_id is None

    JobStateService(db).begin_unit(prepared.job_id, FINAL_PROMOTION_UNIT_KEY)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET bound_input_hash=? "
        "WHERE job_id=? AND unit_key=?",
        ("f" * 64, prepared.job_id, FINAL_PROMOTION_UNIT_KEY),
    )
    with pytest.raises(DomainError):
        CurrentResultService(db).promote_completed_run(prepared.run_id, batch.id)
    assert CurrentResultService(db).get_scope(scope_id).source_run_id is None


def test_repeated_successful_promotion_is_a_safe_conflict_without_mutation(db):
    prepared, batch, scope_id = _ready(db, "repeat")
    service = CurrentResultService(db)
    service.promote_completed_run(prepared.run_id, batch.id)
    before = _durable_state(db, scope_id, prepared.run_id, prepared.job_id)

    with pytest.raises(DomainError) as error:
        service.promote_completed_run(prepared.run_id, batch.id)

    assert error.value.code == "CURRENT_PROMOTION_NOT_ALLOWED"
    assert _durable_state(db, scope_id, prepared.run_id, prepared.job_id) == before
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_events "
        "WHERE run_id=? AND status='accepted'",
        (prepared.run_id,),
    ).fetchone()[0] == 1


def test_promotion_rejects_acceptance_time_before_transport_and_rolls_back(db):
    prepared, batch, scope_id = _ready(db, "acceptance-time")
    before = _durable_state(db, scope_id, prepared.run_id, prepared.job_id)

    with pytest.raises(DomainError):
        CurrentResultService(
            db,
            clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
        ).promote_completed_run(prepared.run_id, batch.id)

    assert _durable_state(db, scope_id, prepared.run_id, prepared.job_id) == before


@pytest.mark.parametrize("value", (0, -1, True, "1"))
def test_promotion_rejects_nonpositive_or_noninteger_ids(db, value):
    with pytest.raises(DomainError) as error:
        CurrentResultService(db).promote_completed_run(value, value)
    assert error.value.code == "CURRENT_PROMOTION_INVALID"
