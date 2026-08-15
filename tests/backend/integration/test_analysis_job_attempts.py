from dataclasses import replace

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    JobKind,
    JobStage,
    JobStatus,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    JobManifest,
    ManifestUnit,
)
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.jobs import JobRepository
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.integration.test_analysis_input_boundaries import (
    FIXED_UTC,
    PreparedAnalysis,
    _begin,
    _prepare_personal_analysis,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _source_artifacts(db, job_id: int) -> dict[str, str]:
    return {
        row["unit_key"]: row["output_hash"]
        for row in db.execute(
            """
            SELECT unit_key, output_hash
            FROM job_units
            WHERE job_id=? AND status='success'
            """,
            (job_id,),
        )
    }


def _source_external_inputs(db, job_id: int) -> dict[str, str | None]:
    return {
        row["unit_key"]: row["external_input_hash"]
        for row in db.execute(
            "SELECT unit_key, external_input_hash FROM job_units WHERE job_id=?",
            (job_id,),
        )
    }


def _create_successor(
    db,
    prepared: PreparedAnalysis,
    *,
    manifest: JobManifest | None = None,
    artifact_overrides: dict[str, str] | None = None,
):
    artifacts = _source_artifacts(db, prepared.job_id)
    artifacts.update(artifact_overrides or {})
    return JobStateService(db).create_successor(
        prepared.job_id,
        manifest or prepared.manifest,
        artifacts,
        _source_external_inputs(db, prepared.job_id),
    )


def _complete_unit(db, job_id: int, unit_key: str, output_hash: str) -> None:
    with transaction(db):
        JobStateService(db).complete_unit_in_transaction(
            job_id, unit_key, output_hash
        )


def test_stopped_run_attaches_successor_only_after_reusing_durable_successes(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).request_stop(prepared.job_id)

    successor_id, plan = _create_successor(db, prepared)
    attempt = AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert ANALYSIS_INPUT_UNIT_KEY in plan.reused_unit_keys
    assert attempt.run_id == run.id
    assert attempt.job_id == successor_id
    assert attempt.attempt_ordinal == 2
    assert attempt.source_job_id == prepared.job_id
    assert AnalysisRepository(db).get_active_job_id(run.id) == successor_id
    assert AnalysisRepository(db).get_run(run.id).active_job_id == successor_id
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_job_attempts WHERE run_id=?", (run.id,)
    ).fetchone()[0] == 2


def test_successor_missing_input_snapshot_reuse_requires_a_new_run(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).request_stop(prepared.job_id)
    successor_id, plan = _create_successor(
        db,
        prepared,
        artifact_overrides={ANALYSIS_INPUT_UNIT_KEY: "different-input-artifact"},
    )
    assert ANALYSIS_INPUT_UNIT_KEY not in plan.reused_unit_keys

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert error.value.code == "SUCCESSOR_REQUIRES_NEW_RUN"
    assert AnalysisRepository(db).get_active_job_id(run.id) == prepared.job_id


def test_completed_codex_unit_cannot_be_recomputed_inside_same_run(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    service = JobStateService(db)
    service.begin_unit(prepared.job_id, "codex:batch:1")
    _complete_unit(db, prepared.job_id, "codex:batch:1", "codex-output-v1")
    service.request_stop(prepared.job_id)

    successor_id, plan = _create_successor(
        db,
        prepared,
        artifact_overrides={"codex:batch:1": "different-codex-output"},
    )
    assert ANALYSIS_INPUT_UNIT_KEY in plan.reused_unit_keys
    assert "codex:batch:1" not in plan.reused_unit_keys

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert error.value.code == "SUCCESSOR_REQUIRES_NEW_RUN"


def test_normally_failed_active_job_cannot_attach_successor(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    service = JobStateService(db)
    service.begin_unit(prepared.job_id, "codex:batch:1")
    service.fail_unit(prepared.job_id, "codex:batch:1", "synthetic_failure")
    successor_id, _ = _create_successor(db, prepared)

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert error.value.code == "SUCCESSOR_SOURCE_NOT_SAFE"


def test_stopped_source_must_have_no_running_unit_to_be_safe(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).begin_unit(prepared.job_id, "codex:batch:1")
    db.execute(
        "UPDATE jobs SET status=? WHERE id=?",
        (JobStatus.STOPPED.value, prepared.job_id),
    )
    with transaction(db):
        successor_id = JobRepository(db).create(
            prepared.manifest,
            source_job_id=prepared.job_id,
            created_at=FIXED_UTC,
        )

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert error.value.code == "SUCCESSOR_SOURCE_NOT_SAFE"


def test_input_changed_failed_job_can_attach_compatible_successor(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    service = JobStateService(db)
    service.begin_unit(prepared.job_id, "codex:batch:1")
    service.fail_unit(prepared.job_id, "codex:batch:1", "synthetic_retryable")
    service.resume(prepared.job_id, _source_artifacts(db, prepared.job_id))
    with pytest.raises(DomainError) as changed:
        service.begin_unit(
            prepared.job_id, "codex:batch:1", "changed-external-input"
        )
    assert changed.value.code == "UNIT_INPUT_CHANGED"
    assert service.status(prepared.job_id) is JobStatus.FAILED
    assert service.unit(
        prepared.job_id, "codex:batch:1"
    ).status is UnitStatus.PENDING

    successor_id, plan = _create_successor(db, prepared)
    attempt = AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert ANALYSIS_INPUT_UNIT_KEY in plan.reused_unit_keys
    assert attempt.source_job_id == prepared.job_id
    assert AnalysisRepository(db).get_active_job_id(run.id) == successor_id


def test_successor_must_descend_from_the_current_active_attempt(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    service = JobStateService(db)
    service.request_stop(prepared.job_id)
    first_successor_id, _ = _create_successor(db, prepared)
    AnalysisRunService(db).attach_successor(run.id, first_successor_id)
    service.request_stop(first_successor_id)

    stale_branch_id, _ = _create_successor(db, prepared)
    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, stale_branch_id)

    assert error.value.code == "SUCCESSOR_NOT_ACTIVE_DESCENDANT"
    assert AnalysisRepository(db).get_active_job_id(run.id) == first_successor_id


def test_successor_manifest_must_keep_exact_graph_and_codex_contract(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).request_stop(prepared.job_id)
    changed_units = tuple(
        replace(unit, execution_contract_hash="different-codex-contract")
        if unit.unit_key == "codex:batch:1"
        else unit
        for unit in prepared.manifest.units
    )
    changed_manifest = JobManifest.build(JobKind.ANALYSIS_SCOPE, changed_units)
    successor_id, _ = _create_successor(
        db, prepared, manifest=changed_manifest
    )

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, successor_id)

    assert error.value.code == "SUCCESSOR_REQUIRES_NEW_RUN"


def test_video_pipeline_job_cannot_be_attached_to_analysis_run(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    JobStateService(db).request_stop(prepared.job_id)
    video_manifest = JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "video:synthetic",
                JobStage.VIDEO_METADATA,
                1,
                "synthetic-video-input",
                (),
                "synthetic-video-contract",
            ),
        ),
    )
    video_job_id = JobStateService(db).create(video_manifest)

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).attach_successor(run.id, video_job_id)

    assert error.value.code == "SUCCESSOR_NOT_ACTIVE_DESCENDANT"


def test_same_job_cannot_start_a_second_run(db):
    prepared = _prepare_personal_analysis(db)
    first = _begin(db, prepared)

    with pytest.raises(DomainError) as error:
        AnalysisRunService(db).begin(prepared.command)

    assert error.value.code == "ANALYSIS_JOB_ALREADY_ATTACHED"
    assert AnalysisRepository(db).count_runs(first.scope_id) == 1
