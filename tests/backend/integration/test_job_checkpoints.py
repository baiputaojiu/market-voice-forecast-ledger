import hashlib
import json
import sqlite3
from dataclasses import dataclass

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
    ASSET_MAPPING_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    FORECAST_PROJECTION_UNIT_KEY,
    PERIOD_NORMALIZATION_UNIT_KEY,
    STATEMENT_NORMALIZATION_UNIT_KEY,
    JobManifest,
    ManifestUnit,
)
from market_voice_forecast_ledger.services.job_state import JobStateService


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.sqlite3"


@pytest.fixture
def db(db_path):
    conn = open_database(db_path)
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _video_manifest(count: int = 8) -> JobManifest:
    return JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        tuple(
            ManifestUnit(
                f"transcription:chunk:{ordinal}",
                JobStage.TRANSCRIPTION,
                ordinal,
                f"chunk-{ordinal}-input",
                (),
                "transcription-contract-v1",
            )
            for ordinal in range(1, count + 1)
        ),
    )


def _analysis_manifest() -> JobManifest:
    return JobManifest.build(
        JobKind.ANALYSIS_SCOPE,
        (
            ManifestUnit(
                ANALYSIS_INPUT_UNIT_KEY,
                JobStage.ANALYSIS_INPUT_EXTRACTION,
                1,
                "analysis-input-contract-v1",
                (),
                "input-freeze-contract-v1",
            ),
            ManifestUnit(
                "codex:batch:1",
                JobStage.CODEX_ANALYSIS,
                2,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                "codex-contract-v1",
            ),
            ManifestUnit(
                STATEMENT_NORMALIZATION_UNIT_KEY,
                JobStage.CODEX_ANALYSIS,
                3,
                None,
                ("codex:batch:1",),
                "statement-contract-v1",
            ),
            ManifestUnit(
                PERIOD_NORMALIZATION_UNIT_KEY,
                JobStage.CODEX_ANALYSIS,
                4,
                None,
                (STATEMENT_NORMALIZATION_UNIT_KEY,),
                "period-contract-v1",
            ),
            ManifestUnit(
                ASSET_MAPPING_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                5,
                None,
                (PERIOD_NORMALIZATION_UNIT_KEY,),
                "mapping-contract-v1",
            ),
            ManifestUnit(
                FORECAST_PROJECTION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                6,
                None,
                (ASSET_MAPPING_UNIT_KEY,),
                "projection-contract-v1",
            ),
            ManifestUnit(
                FINAL_PROMOTION_UNIT_KEY,
                JobStage.HEATMAP_UPDATE,
                7,
                None,
                (FORECAST_PROJECTION_UNIT_KEY,),
                "promotion-contract-v1",
            ),
        ),
    )


@pytest.fixture
def eight_chunk_job(db):
    return JobStateService(db).create(_video_manifest())


def _stored_hashes_for_first_four() -> dict[str, str]:
    return {
        f"transcription:chunk:{ordinal}": f"chunk-{ordinal}-artifact"
        for ordinal in range(1, 5)
    }


def _mark_first_four_chunks_complete(db, job_id: int) -> None:
    service = JobStateService(db)
    for unit_key, output_hash in _stored_hashes_for_first_four().items():
        service.begin_unit(job_id, unit_key)
        service.complete_unit(job_id, unit_key, output_hash)


def _complete_analysis_unit(
    db,
    service: JobStateService,
    job_id: int,
    unit_key: str,
    output_hash: str,
) -> None:
    with transaction(db):
        service.complete_unit_in_transaction(job_id, unit_key, output_hash)


@dataclass(frozen=True)
class _CompletedJob:
    id: int
    manifest: JobManifest
    artifact_hashes: dict[str, str]
    external_input_hashes: dict[str, str | None]

    def manifest_with_contract(self, contract_hash: str) -> JobManifest:
        return JobManifest.build(
            self.manifest.kind,
            tuple(
                ManifestUnit(
                    unit.unit_key,
                    unit.stage,
                    unit.ordinal,
                    unit.declared_input_hash,
                    unit.dependency_keys,
                    (
                        contract_hash
                        if unit.unit_key == "codex:batch:1"
                        else unit.execution_contract_hash
                    ),
                )
                for unit in self.manifest.units
            ),
        )


def _complete_analysis_job(
    db, *, stop_before_projection: bool = False
) -> _CompletedJob:
    manifest = _analysis_manifest()
    service = JobStateService(db)
    job_id = service.create(manifest)
    artifacts = {
        unit.unit_key: f"artifact-{unit.ordinal}" for unit in manifest.units
    }
    external_inputs = {unit.unit_key: None for unit in manifest.units}
    external_inputs[FORECAST_PROJECTION_UNIT_KEY] = "review-state-v1"
    for unit in manifest.units:
        external_input_hash = external_inputs[unit.unit_key]
        service.begin_unit(job_id, unit.unit_key, external_input_hash)
        if stop_before_projection and unit.unit_key == FORECAST_PROJECTION_UNIT_KEY:
            service.fail_unit(job_id, unit.unit_key, "synthetic_retryable")
            break
        _complete_analysis_unit(
            db,
            service,
            job_id,
            unit.unit_key,
            artifacts[unit.unit_key],
        )
    if not stop_before_projection:
        with transaction(db):
            service.succeed_job_in_transaction(job_id)
    return _CompletedJob(job_id, manifest, artifacts, external_inputs)


@pytest.fixture
def completed_source_job(db):
    return _complete_analysis_job(db)


@pytest.fixture
def failed_projection_job(db):
    return _complete_analysis_job(db, stop_before_projection=True)


def test_begin_unit_requires_successful_dependencies_and_binds_exact_input(db):
    manifest = JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "video:one",
                JobStage.VIDEO_METADATA,
                1,
                "video-input-v1",
                (),
                "metadata-contract-v1",
            ),
            ManifestUnit(
                "audio:one",
                JobStage.AUDIO_ACQUISITION,
                2,
                None,
                ("video:one",),
                "audio-contract-v1",
            ),
        ),
    )
    service = JobStateService(db)
    job_id = service.create(manifest)

    with pytest.raises(DomainError) as error:
        service.begin_unit(job_id, "audio:one")
    assert error.value.code == "UNIT_DEPENDENCY_NOT_SUCCESS"

    root = service.begin_unit(job_id, "video:one")
    expected_root_hash = hashlib.sha256(
        b'{"declared_input_hash":"video-input-v1",'
        b'"dependency_outputs":[],"external_input_hash":null}'
    ).hexdigest()
    assert root.bound_input_hash == expected_root_hash
    service.complete_unit(job_id, "video:one", "metadata-artifact-v1")

    dependent = service.begin_unit(job_id, "audio:one", "audio-review-v1")
    expected_dependent_hash = hashlib.sha256(
        b'{"declared_input_hash":null,'
        b'"dependency_outputs":["metadata-artifact-v1"],'
        b'"external_input_hash":"audio-review-v1"}'
    ).hexdigest()
    assert dependent.bound_input_hash == expected_dependent_hash


def test_effective_input_orders_dependency_outputs_by_manifest_ordinal(db):
    manifest = JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "video:one",
                JobStage.VIDEO_METADATA,
                1,
                "video-input-v1",
                (),
                "metadata-contract-v1",
            ),
            ManifestUnit(
                "audio:one",
                JobStage.AUDIO_ACQUISITION,
                2,
                "audio-input-v1",
                (),
                "audio-contract-v1",
            ),
            ManifestUnit(
                "transcription:chunk:1",
                JobStage.TRANSCRIPTION,
                3,
                None,
                ("audio:one", "video:one"),
                "transcription-contract-v1",
            ),
        ),
    )
    service = JobStateService(db)
    job_id = service.create(manifest)
    service.begin_unit(job_id, "video:one")
    service.complete_unit(job_id, "video:one", "metadata-artifact-v1")
    service.begin_unit(job_id, "audio:one")
    service.complete_unit(job_id, "audio:one", "audio-artifact-v1")

    unit = service.begin_unit(job_id, "transcription:chunk:1")
    expected_hash = hashlib.sha256(
        b'{"declared_input_hash":null,'
        b'"dependency_outputs":['
        b'"metadata-artifact-v1","audio-artifact-v1"],'
        b'"external_input_hash":null}'
    ).hexdigest()

    assert unit.bound_input_hash == expected_hash


def test_resume_reuses_only_success_units_with_matching_artifact_hash(
    db, eight_chunk_job
):
    _mark_first_four_chunks_complete(db, eight_chunk_job)

    plan = JobStateService(db).resume(
        eight_chunk_job, _stored_hashes_for_first_four()
    )

    assert plan.next_unit_key == "transcription:chunk:5"
    assert len(plan.reused_unit_keys) == 4


def test_resume_invalidates_success_and_its_dependents_on_artifact_mismatch(db):
    manifest = JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "video:one",
                JobStage.VIDEO_METADATA,
                1,
                "video-input-v1",
                (),
                "metadata-contract-v1",
            ),
            ManifestUnit(
                "audio:one",
                JobStage.AUDIO_ACQUISITION,
                2,
                None,
                ("video:one",),
                "audio-contract-v1",
            ),
        ),
    )
    service = JobStateService(db)
    job_id = service.create(manifest)
    service.begin_unit(job_id, "video:one")
    service.complete_unit(job_id, "video:one", "metadata-artifact-v1")
    service.begin_unit(job_id, "audio:one")
    service.complete_unit(job_id, "audio:one", "audio-artifact-v1")

    plan = service.resume(
        job_id,
        {"video:one": "different-artifact", "audio:one": "audio-artifact-v1"},
    )

    assert plan.reused_unit_keys == ()
    assert plan.pending_unit_keys == ("video:one", "audio:one")
    assert service.unit(job_id, "video:one").output_hash is None
    assert service.unit(job_id, "audio:one").output_hash is None


def test_success_unit_is_not_reused_when_execution_contract_changes(
    db, completed_source_job
):
    changed = completed_source_job.manifest_with_contract("contract-v2")

    successor_id, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        changed,
        completed_source_job.artifact_hashes,
        completed_source_job.external_input_hashes,
    )

    assert successor_id != completed_source_job.id
    assert "codex:batch:1" not in plan.reused_unit_keys
    assert "codex:batch:1" in plan.pending_unit_keys


def test_successor_with_every_verified_unit_reused_is_succeeded(
    db, completed_source_job
):
    successor_id, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        completed_source_job.manifest,
        completed_source_job.artifact_hashes,
        completed_source_job.external_input_hashes,
    )

    assert plan.pending_unit_keys == ()
    assert plan.next_unit_key is None
    assert JobStateService(db).status(successor_id) is JobStatus.SUCCEEDED


def test_successor_rejects_a_manifest_from_the_other_job_kind(db):
    service = JobStateService(db)
    source_job_id = service.create(_video_manifest(1))
    service.request_stop(source_job_id)

    with pytest.raises(DomainError) as error:
        service.create_successor(
            source_job_id,
            _analysis_manifest(),
            {},
            {},
        )

    assert error.value.code == "SUCCESSOR_KIND_MISMATCH"


def test_successor_requires_the_supplied_external_input_to_match(
    db, completed_source_job
):
    changed_external_inputs = completed_source_job.external_input_hashes | {
        FORECAST_PROJECTION_UNIT_KEY: "review-state-v2"
    }

    _, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        completed_source_job.manifest,
        completed_source_job.artifact_hashes,
        changed_external_inputs,
    )

    assert FORECAST_PROJECTION_UNIT_KEY in plan.pending_unit_keys
    assert FINAL_PROMOTION_UNIT_KEY in plan.pending_unit_keys


def test_successor_invalidates_the_dependent_closure_when_an_upstream_artifact_differs(
    db, completed_source_job
):
    artifacts = completed_source_job.artifact_hashes | {
        ANALYSIS_INPUT_UNIT_KEY: "different-real-artifact"
    }

    _, plan = JobStateService(db).create_successor(
        completed_source_job.id,
        completed_source_job.manifest,
        artifacts,
        completed_source_job.external_input_hashes,
    )

    assert ANALYSIS_INPUT_UNIT_KEY in plan.pending_unit_keys
    assert "codex:batch:1" in plan.pending_unit_keys
    assert STATEMENT_NORMALIZATION_UNIT_KEY in plan.pending_unit_keys


def test_retry_rejects_changed_external_input_for_the_same_unit(
    db, failed_projection_job
):
    JobStateService(db).resume(
        failed_projection_job.id, failed_projection_job.artifact_hashes
    )

    with pytest.raises(DomainError) as error:
        JobStateService(db).begin_unit(
            failed_projection_job.id,
            FORECAST_PROJECTION_UNIT_KEY,
            external_input_hash="review-state-v2",
        )

    assert error.value.code == "UNIT_INPUT_CHANGED"
    assert (
        JobStateService(db).status(failed_projection_job.id)
        is JobStatus.FAILED
    )


def test_changed_retry_input_can_continue_only_in_a_successor(
    db, failed_projection_job
):
    service = JobStateService(db)
    service.resume(
        failed_projection_job.id, failed_projection_job.artifact_hashes
    )
    changed_external_inputs = failed_projection_job.external_input_hashes | {
        FORECAST_PROJECTION_UNIT_KEY: "review-state-v2"
    }
    with pytest.raises(DomainError) as error:
        service.begin_unit(
            failed_projection_job.id,
            FORECAST_PROJECTION_UNIT_KEY,
            "review-state-v2",
        )
    assert error.value.code == "UNIT_INPUT_CHANGED"

    successor_id, plan = service.create_successor(
        failed_projection_job.id,
        failed_projection_job.manifest,
        failed_projection_job.artifact_hashes,
        changed_external_inputs,
    )

    assert successor_id != failed_projection_job.id
    assert FORECAST_PROJECTION_UNIT_KEY in plan.pending_unit_keys
    assert service.status(successor_id) is JobStatus.QUEUED


def test_interrupted_fifth_unit_restarts_from_pending_after_reopen(
    db_path, tmp_path, eight_chunk_job
):
    first = open_database(db_path)
    _mark_first_four_chunks_complete(first, eight_chunk_job)
    JobStateService(first).begin_unit(eight_chunk_job, "transcription:chunk:5")
    first.close()
    partial = tmp_path / "chunk-5.partial.json"
    partial.write_text('{"partial":true}', encoding="utf-8")
    artifact_hashes = _stored_hashes_for_first_four() | {
        "transcription:chunk:5": hashlib.sha256(partial.read_bytes()).hexdigest()
    }

    reopened = open_database(db_path)
    try:
        plan = JobStateService(reopened).recover_interrupted(
            eight_chunk_job, artifact_hashes
        )

        assert len(plan.reused_unit_keys) == 4
        assert plan.next_unit_key == "transcription:chunk:5"
        assert (
            JobStateService(reopened).unit(
                eight_chunk_job, "transcription:chunk:5"
            ).status
            is UnitStatus.PENDING
        )
        attempt = reopened.execute(
            "SELECT result_status FROM job_unit_attempts "
            "WHERE job_id=? AND unit_key=? AND attempt_no=1",
            (eight_chunk_job, "transcription:chunk:5"),
        ).fetchone()
        assert attempt["result_status"] == "interrupted"
    finally:
        reopened.close()


def test_complete_unit_in_transaction_rolls_back_with_the_artifact_write(db_path):
    first = open_database(db_path)
    apply_migrations(first)
    service = JobStateService(first)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")

    with pytest.raises(RuntimeError, match="synthetic injected failure"):
        with transaction(first):
            first.execute(
                "INSERT INTO app_metadata(key, value) VALUES ('artifact', 'durable')"
            )
            service.complete_unit_in_transaction(
                job_id, "transcription:chunk:1", "chunk-1-artifact"
            )
            raise RuntimeError("synthetic injected failure")
    first.close()

    reopened = open_database(db_path)
    try:
        assert reopened.execute(
            "SELECT COUNT(*) FROM app_metadata WHERE key='artifact'"
        ).fetchone()[0] == 0
        assert (
            JobStateService(reopened).unit(
                job_id, "transcription:chunk:1"
            ).status
            is UnitStatus.RUNNING
        )
        assert reopened.execute(
            "SELECT COUNT(*) FROM job_unit_attempts WHERE job_id=?", (job_id,)
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_transaction_internal_methods_require_the_callers_transaction(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")

    with pytest.raises(DomainError) as complete_error:
        service.complete_unit_in_transaction(
            job_id, "transcription:chunk:1", "chunk-1-artifact"
        )
    assert complete_error.value.code == "JOB_TRANSACTION_REQUIRED"

    with pytest.raises(DomainError) as fail_error:
        service.fail_unit_in_transaction(
            job_id, "transcription:chunk:1", "synthetic_retryable"
        )
    assert fail_error.value.code == "JOB_TRANSACTION_REQUIRED"

    with pytest.raises(DomainError) as success_error:
        service.succeed_job_in_transaction(job_id)
    assert success_error.value.code == "JOB_TRANSACTION_REQUIRED"


def test_analysis_success_requires_a_caller_owned_artifact_transaction(db):
    service = JobStateService(db)
    job_id = service.create(_analysis_manifest())
    service.begin_unit(job_id, ANALYSIS_INPUT_UNIT_KEY)

    with pytest.raises(DomainError) as error:
        service.complete_unit(
            job_id, ANALYSIS_INPUT_UNIT_KEY, "input-artifact"
        )
    assert error.value.code == "ANALYSIS_TRANSACTION_REQUIRED"
    assert (
        service.unit(job_id, ANALYSIS_INPUT_UNIT_KEY).status
        is UnitStatus.RUNNING
    )

    with transaction(db):
        db.execute(
            "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
            ("analysis-input-artifact", "durable"),
        )
        service.complete_unit_in_transaction(
            job_id, ANALYSIS_INPUT_UNIT_KEY, "input-artifact"
        )

    assert (
        service.unit(job_id, ANALYSIS_INPUT_UNIT_KEY).status
        is UnitStatus.SUCCESS
    )
    assert db.execute(
        "SELECT value FROM app_metadata WHERE key='analysis-input-artifact'"
    ).fetchone()[0] == "durable"


def test_pause_and_stop_take_effect_only_at_safe_unit_boundaries(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(2))
    service.begin_unit(job_id, "transcription:chunk:1")

    assert service.request_pause(job_id) is JobStatus.PAUSE_REQUESTED
    service.complete_unit(job_id, "transcription:chunk:1", "chunk-1-artifact")
    assert service.status(job_id) is JobStatus.PAUSED

    plan = service.resume(job_id, {"transcription:chunk:1": "chunk-1-artifact"})
    assert plan.next_unit_key == "transcription:chunk:2"
    assert service.status(job_id) is JobStatus.RUNNING
    service.begin_unit(job_id, "transcription:chunk:2")

    assert service.request_stop(job_id) is JobStatus.CANCEL_REQUESTED
    service.complete_unit(job_id, "transcription:chunk:2", "chunk-2-artifact")
    assert service.status(job_id) is JobStatus.STOPPED


def test_pause_waits_for_every_running_unit_to_reach_a_safe_boundary(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(2))
    service.begin_unit(job_id, "transcription:chunk:1")
    service.begin_unit(job_id, "transcription:chunk:2")

    assert service.request_pause(job_id) is JobStatus.PAUSE_REQUESTED
    service.complete_unit(job_id, "transcription:chunk:1", "chunk-1-artifact")
    assert service.status(job_id) is JobStatus.PAUSE_REQUESTED

    service.complete_unit(job_id, "transcription:chunk:2", "chunk-2-artifact")
    assert service.status(job_id) is JobStatus.PAUSED


def test_unit_failure_during_pause_request_fails_the_job(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")
    service.request_pause(job_id)

    service.fail_unit(job_id, "transcription:chunk:1", "synthetic_retryable")

    assert service.status(job_id) is JobStatus.FAILED
    assert (
        service.unit(job_id, "transcription:chunk:1").status
        is UnitStatus.FAILED
    )


def test_pause_failure_with_another_running_unit_requires_crash_recovery(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(2))
    service.begin_unit(job_id, "transcription:chunk:1")
    service.begin_unit(job_id, "transcription:chunk:2")
    service.request_pause(job_id)
    service.fail_unit(job_id, "transcription:chunk:1", "synthetic_retryable")

    with pytest.raises(DomainError) as error:
        service.resume(job_id, {})
    assert error.value.code == "INTERRUPTED_RECOVERY_REQUIRED"

    plan = service.recover_interrupted(job_id, {})
    assert service.status(job_id) is JobStatus.RETRYING
    assert plan.pending_unit_keys == (
        "transcription:chunk:1",
        "transcription:chunk:2",
    )
    attempts = tuple(
        row["result_status"]
        for row in db.execute(
            "SELECT result_status FROM job_unit_attempts "
            "WHERE job_id=? ORDER BY unit_key",
            (job_id,),
        )
    )
    assert attempts == ("failed", "interrupted")


def test_resume_does_not_restart_a_live_pause_requested_attempt(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")
    service.request_pause(job_id)

    with pytest.raises(DomainError) as error:
        service.resume(job_id, {})

    assert error.value.code == "PAUSE_BOUNDARY_NOT_REACHED"
    assert service.status(job_id) is JobStatus.PAUSE_REQUESTED
    assert (
        service.unit(job_id, "transcription:chunk:1").status
        is UnitStatus.RUNNING
    )


def test_reopen_recovery_of_pause_request_stays_paused_until_resume(db_path):
    first = open_database(db_path)
    apply_migrations(first)
    service = JobStateService(first)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")
    service.request_pause(job_id)
    first.close()

    reopened = open_database(db_path)
    try:
        reopened_service = JobStateService(reopened)
        plan = reopened_service.recover_interrupted(job_id, {})
        assert plan.next_unit_key == "transcription:chunk:1"
        assert reopened_service.status(job_id) is JobStatus.PAUSED
        assert (
            reopened_service.unit(job_id, "transcription:chunk:1").status
            is UnitStatus.PENDING
        )

        reopened_service.resume(job_id, {})
        assert reopened_service.status(job_id) is JobStatus.RUNNING
    finally:
        reopened.close()


def test_queued_job_can_stop_but_cannot_pause_or_resume(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(1))

    with pytest.raises(DomainError) as pause_error:
        service.request_pause(job_id)
    assert pause_error.value.code == "INVALID_JOB_TRANSITION"

    assert service.request_stop(job_id) is JobStatus.STOPPED
    with pytest.raises(DomainError) as resume_error:
        service.resume(job_id, {})
    assert resume_error.value.code == "STOPPED_JOB_REQUIRES_SUCCESSOR"


def test_cancel_requested_crash_finalizes_stop_before_successor(
    db_path, tmp_path
):
    manifest = _video_manifest(1)
    first = open_database(db_path)
    apply_migrations(first)
    service = JobStateService(first)
    job_id = service.create(manifest)
    service.begin_unit(job_id, "transcription:chunk:1")
    assert service.request_stop(job_id) is JobStatus.CANCEL_REQUESTED
    first.close()
    partial = tmp_path / "stopped-chunk.partial"
    partial.write_text("synthetic partial", encoding="utf-8")

    reopened = open_database(db_path)
    try:
        reopened_service = JobStateService(reopened)
        with pytest.raises(DomainError) as error:
            reopened_service.recover_interrupted(
                job_id,
                {
                    "transcription:chunk:1": hashlib.sha256(
                        partial.read_bytes()
                    ).hexdigest()
                },
            )
        assert error.value.code == "STOPPED_JOB_REQUIRES_SUCCESSOR"
        assert reopened_service.status(job_id) is JobStatus.STOPPED
        assert (
            reopened_service.unit(job_id, "transcription:chunk:1").status
            is UnitStatus.PENDING
        )
        attempt = reopened.execute(
            "SELECT result_status FROM job_unit_attempts WHERE job_id=?",
            (job_id,),
        ).fetchone()
        assert attempt["result_status"] == "interrupted"

        successor_id, plan = reopened_service.create_successor(
            job_id,
            manifest,
            {},
            {"transcription:chunk:1": None},
        )
        assert successor_id != job_id
        assert plan.pending_unit_keys == ("transcription:chunk:1",)
    finally:
        reopened.close()


def test_upstream_guard_checks_every_pre_promotion_unit_not_only_dependencies(db):
    manifest = JobManifest.build(
        JobKind.ANALYSIS_SCOPE,
        (
            ManifestUnit(
                ANALYSIS_INPUT_UNIT_KEY,
                JobStage.ANALYSIS_INPUT_EXTRACTION,
                1,
                "analysis-input-v1",
                (),
                "freeze-contract-v1",
            ),
            ManifestUnit(
                "codex:independent-check",
                JobStage.CODEX_ANALYSIS,
                2,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                "check-contract-v1",
            ),
            ManifestUnit(
                "codex:promotion-source",
                JobStage.CODEX_ANALYSIS,
                3,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                "source-contract-v1",
            ),
            ManifestUnit(
                FINAL_PROMOTION_UNIT_KEY,
                JobStage.HEATMAP_UPDATE,
                4,
                None,
                ("codex:promotion-source",),
                "promotion-contract-v1",
            ),
        ),
    )
    service = JobStateService(db)
    job_id = service.create(manifest)
    service.begin_unit(job_id, ANALYSIS_INPUT_UNIT_KEY)
    _complete_analysis_unit(
        db, service, job_id, ANALYSIS_INPUT_UNIT_KEY, "input-artifact"
    )
    service.begin_unit(job_id, "codex:promotion-source")
    _complete_analysis_unit(
        db,
        service,
        job_id,
        "codex:promotion-source",
        "source-artifact",
    )

    with pytest.raises(DomainError) as error:
        service.require_upstream_success(job_id, FINAL_PROMOTION_UNIT_KEY)

    assert error.value.code == "UPSTREAM_UNITS_INCOMPLETE"


def test_job_success_requires_every_unit_including_final_promotion(db):
    manifest = _analysis_manifest()
    service = JobStateService(db)
    job_id = service.create(manifest)
    for unit in manifest.units[:-1]:
        external = (
            "review-state-v1"
            if unit.unit_key == FORECAST_PROJECTION_UNIT_KEY
            else None
        )
        service.begin_unit(job_id, unit.unit_key, external)
        _complete_analysis_unit(
            db,
            service,
            job_id,
            unit.unit_key,
            f"artifact-{unit.ordinal}",
        )

    service.require_upstream_success(job_id, FINAL_PROMOTION_UNIT_KEY)
    with transaction(db):
        with pytest.raises(DomainError) as error:
            service.succeed_job_in_transaction(job_id)
        assert error.value.code == "ALL_UNITS_MUST_SUCCEED"

    service.begin_unit(job_id, FINAL_PROMOTION_UNIT_KEY)
    with transaction(db):
        service.complete_unit_in_transaction(
            job_id, FINAL_PROMOTION_UNIT_KEY, "promotion-artifact"
        )
        service.succeed_job_in_transaction(job_id)
        assert service.status(job_id) is JobStatus.SUCCEEDED


def test_attempts_and_events_are_append_only_and_store_safe_metadata_only(
    db, tmp_path
):
    service = JobStateService(db)
    job_id = service.create(_video_manifest(1))
    service.begin_unit(job_id, "transcription:chunk:1")
    private_path = str(tmp_path / "private-audio.wav")

    with transaction(db):
        with pytest.raises(DomainError) as error:
            service.fail_unit_in_transaction(
                job_id, "transcription:chunk:1", private_path
            )
        assert error.value.code == "UNSAFE_ERROR_CODE"

    service.fail_unit(job_id, "transcription:chunk:1", "synthetic_retryable")
    attempt = db.execute(
        "SELECT id, error_code FROM job_unit_attempts WHERE job_id=?", (job_id,)
    ).fetchone()
    events = tuple(
        db.execute(
            "SELECT id, metadata_json FROM job_events WHERE job_id=? ORDER BY id",
            (job_id,),
        )
    )

    assert attempt["error_code"] == "synthetic_retryable"
    assert events
    for event in events:
        metadata = json.loads(event["metadata_json"])
        serialized = json.dumps(metadata)
        assert private_path not in serialized
        assert "text_body" not in serialized
        assert "audio_path" not in serialized

    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute(
            "UPDATE job_unit_attempts SET error_code='changed' WHERE id=?",
            (attempt["id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY"):
        db.execute("DELETE FROM job_events WHERE id=?", (events[0]["id"],))


def test_job_unit_schema_stores_only_a_safe_error_code_for_failures(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(job_units)")}

    assert "error_code" in columns
    assert columns.isdisjoint(
        {"error_message", "traceback", "partial_output", "artifact_path"}
    )


def test_database_rejects_manifest_field_updates_unit_inserts_and_deletes(db):
    job_id = JobStateService(db).create(_video_manifest(1))

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"):
        db.execute(
            "UPDATE jobs SET manifest_hash='changed' WHERE id=?", (job_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"):
        db.execute(
            "UPDATE job_units SET unit_key='changed:key' "
            "WHERE job_id=? AND unit_key=?",
            (job_id, "transcription:chunk:1"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"):
        db.execute(
            "DELETE FROM job_units WHERE job_id=? AND unit_key=?",
            (job_id, "transcription:chunk:1"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_MANIFEST"):
        db.execute(
            """
            INSERT INTO job_units(
                job_id,
                unit_key,
                stage,
                ordinal,
                declared_input_hash,
                dependency_keys_json,
                execution_contract_hash,
                status,
                attempt_count
            ) VALUES (?, 'extra:unit', 'transcription', 2, 'extra-input', '[]',
                      'extra-contract', 'pending', 0)
            """,
            (job_id,),
        )
