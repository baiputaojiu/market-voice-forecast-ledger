import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import (
    canonical_json,
    sha256_text,
    utc_iso,
)
from market_voice_forecast_ledger.domain.discovery import (
    PresenceOrigin,
    PresenceState,
    canonical_presence_decision_hash,
)
from market_voice_forecast_ledger.domain.enums import JobStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import (
    PRESENCE_UNITS,
    ReviewAction,
    VoiceManifestSnapshot,
    VoiceProposal,
    VoiceRunResult,
    VoiceSegmentScore,
    build_presence_job_manifest,
)
from market_voice_forecast_ledger.repositories.voice_verification import (
    VoiceVerificationRepository,
    canonical_voice_segment_hash,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from tests.backend.integration.test_voice_reference_enrollment import (
    FEATURE_BYTES,
    FEATURE_HASH,
    NOW,
    ReferenceSeed,
    add_valid_clip,
    db,
    seed_reference_profile,
)


REVIEWED_AT = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
VOICE_UNIT_KEYS = tuple(unit_key for unit_key, _ in PRESENCE_UNITS)


@dataclass(frozen=True)
class ReviewCommand:
    run_id: int
    action: ReviewAction
    reason: str
    actor: str


@dataclass(frozen=True)
class JobSeed:
    reference: ReferenceSeed
    job_id: int
    snapshot: VoiceManifestSnapshot


def segment_hash(snapshot: VoiceManifestSnapshot, input_hash: str, segment) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": snapshot.adapter_version,
                "end_ms": segment[2],
                "input_hash": input_hash,
                "model_name": snapshot.model_name,
                "model_version": snapshot.model_version,
                "ordinal": segment[0],
                "raw_match_score": segment[3],
                "schema": "voice-verification-segment.v1",
                "start_ms": segment[1],
                "threshold_config_version": snapshot.threshold_config_version,
                "vad_contract_version": snapshot.vad_contract_version,
            }
        )
    )


def run_output_hash(
    snapshot: VoiceManifestSnapshot,
    input_hash: str,
    proposal: VoiceProposal,
    segments: tuple[VoiceSegmentScore, ...],
) -> str:
    return sha256_text(
        canonical_json(
            {
                "adapter_version": snapshot.adapter_version,
                "input_hash": input_hash,
                "model_name": snapshot.model_name,
                "model_version": snapshot.model_version,
                "proposal": proposal.value,
                "schema": "voice-verification-output.v1",
                "segments": [
                    {
                        "end_ms": segment.end_ms,
                        "evidence_hash": segment.evidence_hash,
                        "ordinal": segment.ordinal,
                        "raw_match_score": segment.raw_match_score,
                        "start_ms": segment.start_ms,
                    }
                    for segment in segments
                ],
                "threshold_config_version": snapshot.threshold_config_version,
                "vad_contract_version": snapshot.vad_contract_version,
            }
        )
    )


def seed_job(db: sqlite3.Connection) -> JobSeed:
    reference = seed_reference_profile(db)
    repository = VoiceVerificationRepository(db)
    repository.add_reference_feature(
        reference.reference_profile_id,
        encoding_version="speaker-embedding-v1",
        float_dtype="float32",
        dimension=4,
        embedding_blob=FEATURE_BYTES,
        created_at=NOW,
    )
    add_valid_clip(db, reference)
    snapshot = VoiceManifestSnapshot(
        candidate_id=reference.candidate_id,
        video_id=reference.video_id,
        profile_id=reference.profile_id,
        presence_decision_id=reference.decision_id,
        presence_decision_hash=reference.decision_hash,
        reference_profile_id=reference.reference_profile_id,
        reference_feature_hash=FEATURE_HASH,
        threshold_config_version="voice-threshold-v1",
        model_name="speaker-model",
        model_version="1.0",
        adapter_version="adapter-v1",
        vad_contract_version="vad-v1",
        selection_contract_version="pilot-selection-v1",
    )
    job_id = JobStateService(db).create_video_pipeline(
        build_presence_job_manifest(snapshot), (reference.candidate_id,)
    )
    repository.add_manifest(job_id, snapshot, created_at=NOW)
    return JobSeed(reference=reference, job_id=job_id, snapshot=snapshot)


def canonical_run(db: sqlite3.Connection, job: JobSeed) -> int:
    input_hash = "c" * 64
    raw_segments = ((1, 1_000, 2_000, 0.75), (2, 2_000, 4_000, 0.80))
    segments = tuple(
        VoiceSegmentScore(
            ordinal,
            start_ms,
            end_ms,
            score,
            segment_hash(job.snapshot, input_hash, item),
        )
        for item in raw_segments
        for ordinal, start_ms, end_ms, score in (item,)
    )
    result = VoiceRunResult(
        job_id=job.job_id,
        candidate_id=job.reference.candidate_id,
        input_hash=input_hash,
        output_hash=run_output_hash(
            job.snapshot, input_hash, VoiceProposal.LIKELY_PRESENT, segments
        ),
        proposal=VoiceProposal.LIKELY_PRESENT,
        result_code="VOICE_PROPOSAL_READY",
    )
    with transaction(db):
        return VoiceVerificationRepository(db).add_run_with_segments(
            result, segments, completed_at=NOW
        )


def mark_job_succeeded(db: sqlite3.Connection, job_id: int) -> None:
    run = db.execute(
        "SELECT output_hash FROM voice_verification_runs WHERE job_id=?", (job_id,)
    ).fetchone()
    rows = db.execute(
        """
        SELECT unit_key, ordinal, declared_input_hash, dependency_keys_json
        FROM job_units WHERE job_id=? ORDER BY ordinal
        """,
        (job_id,),
    ).fetchall()
    outputs: dict[str, str] = {}
    for row in rows:
        dependencies = tuple(json.loads(row["dependency_keys_json"]))
        dependency_outputs = tuple(outputs[key] for key in dependencies)
        external_input_hash = (
            sha256_text(f"external-{row['ordinal']}")
            if row["ordinal"] % 2 == 0
            else None
        )
        bound_input_hash = expected_bound_input_hash(
            row["declared_input_hash"],
            dependency_outputs,
            external_input_hash,
        )
        output_hash = (
            run["output_hash"]
            if row["unit_key"] == "voice:proposal" and run is not None
            else sha256_text(f"output-{row['ordinal']}")
        )
        db.execute(
            """
            UPDATE job_units
            SET external_input_hash=?, bound_input_hash=?, output_hash=?,
                status='success', attempt_count=1, error_code=NULL,
                started_at=?, finished_at=?
            WHERE job_id=? AND unit_key=?
            """,
            (
                external_input_hash,
                bound_input_hash,
                output_hash,
                utc_iso(NOW),
                utc_iso(NOW),
                job_id,
                row["unit_key"],
            ),
        )
        outputs[row["unit_key"]] = output_hash
    db.execute(
        "UPDATE jobs SET status='succeeded', updated_at=? WHERE id=?",
        (utc_iso(NOW), job_id),
    )


def expected_bound_input_hash(
    declared_input_hash: str | None,
    dependency_outputs: tuple[str, ...],
    external_input_hash: str | None,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "declared_input_hash": declared_input_hash,
                "dependency_outputs": list(dependency_outputs),
                "external_input_hash": external_input_hash,
            }
        )
    )


def canonical_root_bound_input(db: sqlite3.Connection, job_id: int) -> str:
    row = db.execute(
        "SELECT declared_input_hash FROM job_units "
        "WHERE job_id=? AND ordinal=1",
        (job_id,),
    ).fetchone()
    return expected_bound_input_hash(row["declared_input_hash"], (), None)


def valid_review(run_id: int, action: ReviewAction) -> ReviewCommand:
    return ReviewCommand(
        run_id=run_id,
        action=action,
        reason="listened to the cited segment",
        actor="local_user",
    )


def move_candidate_pointer_from_frozen_decision(
    db: sqlite3.Connection, job: JobSeed
) -> int:
    created_at = datetime(2026, 8, 22, 4, 30, tzinfo=timezone.utc)
    evidence_ref = "stale-manifest-mutation"
    evidence_hash = "9" * 64
    decision_hash = canonical_presence_decision_hash(
        candidate_id=job.reference.candidate_id,
        state=PresenceState.CONFIRMED,
        decision_origin=PresenceOrigin.VOICE_VERIFICATION,
        evidence_ref=evidence_ref,
        evidence_hash=evidence_hash,
        created_at=created_at,
    )
    decision_id = db.execute(
        """
        INSERT INTO presence_decisions(
            candidate_id, state, decision_origin, evidence_ref,
            evidence_hash, decision_hash, created_at
        ) VALUES (?, 'presence_confirmed', 'voice_verification', ?, ?, ?, ?)
        """,
        (
            job.reference.candidate_id,
            evidence_ref,
            evidence_hash,
            decision_hash,
            utc_iso(created_at),
        ),
    ).lastrowid
    db.execute(
        "UPDATE subject_video_candidates SET current_presence_decision_id=? "
        "WHERE id=?",
        (decision_id, job.reference.candidate_id),
    )
    return decision_id


def insert_foreign_voice_decision_for_review(
    db: sqlite3.Connection, review_id: int, review_hash: str
) -> None:
    foreign_candidate_id = 999_999
    decision_hash = canonical_presence_decision_hash(
        candidate_id=foreign_candidate_id,
        state=PresenceState.CONFIRMED,
        decision_origin=PresenceOrigin.VOICE_VERIFICATION,
        evidence_ref=str(review_id),
        evidence_hash=review_hash,
        created_at=REVIEWED_AT,
    )
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute(
            """
            INSERT INTO presence_decisions(
                candidate_id, state, decision_origin, evidence_ref,
                evidence_hash, decision_hash, created_at
            ) VALUES (?, 'presence_confirmed', 'voice_verification', ?, ?, ?, ?)
            """,
            (
                foreign_candidate_id,
                str(review_id),
                review_hash,
                decision_hash,
                utc_iso(REVIEWED_AT),
            ),
        )
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def test_manifest_round_trip_and_runnable_jobs_are_fifo(db) -> None:
    job = seed_job(db)
    repository = VoiceVerificationRepository(db)

    stored = repository.get_manifest_for_job(job.job_id)
    assert stored.snapshot == job.snapshot
    assert (
        stored.manifest_hash
        == build_presence_job_manifest(job.snapshot).manifest_hash
    )
    assert repository.list_runnable_job_ids() == (job.job_id,)

    db.execute(
        "UPDATE jobs SET status=? WHERE id=?",
        (JobStatus.RETRYING.value, job.job_id),
    )
    assert repository.list_runnable_job_ids() == (job.job_id,)


def test_runnable_job_list_rejects_unknown_stored_status(db) -> None:
    job = seed_job(db)
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("UPDATE jobs SET status='corrupt' WHERE id=?", (job.job_id,))

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).list_runnable_job_ids()


def test_get_run_recomputes_segment_and_output_hashes(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    db.execute("DROP TRIGGER voice_verification_segments_no_update")
    db.execute(
        "UPDATE voice_verification_segments SET raw_match_score=999.0 "
        "WHERE run_id=? AND ordinal=1",
        (run_id,),
    )

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        VoiceVerificationRepository(db).get_run(run_id)


def test_run_write_requires_transaction_and_prevalidates_all_segments(db) -> None:
    job = seed_job(db)
    input_hash = "c" * 64
    bad_segments = (
        VoiceSegmentScore(2, 1_000, 2_000, 0.75, "d" * 64),
    )
    result = VoiceRunResult(
        job.job_id,
        job.reference.candidate_id,
        input_hash,
        "e" * 64,
        VoiceProposal.LIKELY_PRESENT,
        "VOICE_PROPOSAL_READY",
    )

    with pytest.raises(DomainError, match="TRANSACTION_REQUIRED"):
        VoiceVerificationRepository(db).add_run_with_segments(
            result, bad_segments, completed_at=NOW
        )
    with pytest.raises(DomainError, match="VOICE_RUN_INVALID"):
        with transaction(db):
            VoiceVerificationRepository(db).add_run_with_segments(
                result, bad_segments, completed_at=NOW
            )

    assert db.execute("SELECT COUNT(*) FROM voice_verification_runs").fetchone()[0] == 0


def test_run_and_segments_rollback_after_second_segment_failure(db) -> None:
    job = seed_job(db)
    db.execute(
        """
        CREATE TRIGGER inject_second_segment_failure
        BEFORE INSERT ON voice_verification_segments
        WHEN NEW.ordinal=2
        BEGIN SELECT RAISE(ABORT, 'INJECTED_FAILURE'); END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_FAILURE"):
        canonical_run(db, job)

    assert db.execute("SELECT COUNT(*) FROM voice_verification_runs").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_segments").fetchone()[0]
        == 0
    )


def test_run_write_rejects_stale_manifest_before_any_insert(db) -> None:
    job = seed_job(db)
    move_candidate_pointer_from_frozen_decision(db, job)

    with pytest.raises(DomainError, match="VOICE_RUN_INVALID"):
        canonical_run(db, job)

    assert db.execute("SELECT COUNT(*) FROM voice_verification_runs").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_segments").fetchone()[0]
        == 0
    )


def test_run_write_normalizes_negative_zero_before_hash_and_round_trip(db) -> None:
    job = seed_job(db)
    input_hash = "c" * 64
    normalized_segment = VoiceSegmentScore(
        1,
        1_000,
        2_000,
        0.0,
        segment_hash(job.snapshot, input_hash, (1, 1_000, 2_000, 0.0)),
    )
    supplied_segment = VoiceSegmentScore(
        normalized_segment.ordinal,
        normalized_segment.start_ms,
        normalized_segment.end_ms,
        -0.0,
        normalized_segment.evidence_hash,
    )
    assert canonical_voice_segment_hash(
        job.snapshot,
        input_hash,
        ordinal=1,
        start_ms=1_000,
        end_ms=2_000,
        raw_match_score=-0.0,
    ) == normalized_segment.evidence_hash
    result = VoiceRunResult(
        job_id=job.job_id,
        candidate_id=job.reference.candidate_id,
        input_hash=input_hash,
        output_hash=run_output_hash(
            job.snapshot,
            input_hash,
            VoiceProposal.LIKELY_PRESENT,
            (normalized_segment,),
        ),
        proposal=VoiceProposal.LIKELY_PRESENT,
        result_code="VOICE_PROPOSAL_READY",
    )

    with transaction(db):
        run_id = VoiceVerificationRepository(db).add_run_with_segments(
            result, (supplied_segment,), completed_at=NOW
        )

    stored = VoiceVerificationRepository(db).get_run(run_id)
    assert stored.segments[0].raw_match_score == 0.0
    assert math.copysign(1.0, stored.segments[0].raw_match_score) == 1.0
    assert stored.segments[0].evidence_hash == normalized_segment.evidence_hash
    assert stored.output_hash == result.output_hash


@pytest.mark.parametrize(
    (
        "unit_status",
        "external_input_hash",
        "bound_input_hash",
        "output_hash",
        "attempt_count",
        "error_code",
        "started_at",
        "finished_at",
    ),
    (
        ("pending", None, None, "a" * 64, 0, None, None, None),
        ("running", None, "a" * 64, None, 1, None, "not-utc", None),
        (
            "success",
            sqlite3.Binary(b"external"),
            "a" * 64,
            "b" * 64,
            1,
            None,
            utc_iso(NOW),
            utc_iso(NOW),
        ),
        (
            "failed",
            None,
            "a" * 64,
            None,
            1,
            "unsafe error",
            utc_iso(NOW),
            utc_iso(NOW),
        ),
        ("pending", None, None, None, 0.5, None, None, None),
        ("corrupt", None, None, None, 0, None, None, None),
    ),
)
def test_runnable_read_validates_exact_unit_rows_for_every_state(
    db,
    unit_status,
    external_input_hash,
    bound_input_hash,
    output_hash,
    attempt_count,
    error_code,
    started_at,
    finished_at,
) -> None:
    job = seed_job(db)
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute(
        """
        UPDATE job_units
        SET status=?, external_input_hash=?, bound_input_hash=?, output_hash=?,
            attempt_count=?, error_code=?, started_at=?, finished_at=?
        WHERE job_id=? AND ordinal=1
        """,
        (
            unit_status,
            external_input_hash,
            bound_input_hash,
            output_hash,
            attempt_count,
            error_code,
            started_at,
            finished_at,
            job.job_id,
        ),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).list_runnable_job_ids()


def test_artifact_read_validates_pending_unit_external_input_binding(db) -> None:
    job = seed_job(db)
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute(
        "UPDATE job_units SET external_input_hash=? "
        "WHERE job_id=? AND ordinal=1",
        ("a" * 64, job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_succeeded_artifacts_accept_canonical_lineage_for_all_voice_units(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)

    artifacts = VoiceVerificationRepository(db).require_job_artifacts(job.job_id)
    rows = db.execute(
        """
        SELECT unit_key, ordinal, declared_input_hash, dependency_keys_json,
               external_input_hash, bound_input_hash, output_hash
        FROM job_units WHERE job_id=? ORDER BY ordinal
        """,
        (job.job_id,),
    ).fetchall()
    by_key = {row["unit_key"]: row for row in rows}
    for row in rows:
        dependencies = sorted(
            (by_key[key] for key in json.loads(row["dependency_keys_json"])),
            key=lambda dependency: dependency["ordinal"],
        )
        assert row["bound_input_hash"] == expected_bound_input_hash(
            row["declared_input_hash"],
            tuple(dependency["output_hash"] for dependency in dependencies),
            row["external_input_hash"],
        )
    assert tuple(row["external_input_hash"] is None for row in rows) == (
        True,
        False,
        True,
        False,
        True,
        False,
        True,
    )
    assert artifacts.run is not None and artifacts.run.id == run_id


@pytest.mark.parametrize("unit_key", VOICE_UNIT_KEYS)
def test_artifact_read_rejects_bound_input_lineage_drift_for_every_voice_unit(
    db, unit_key: str
) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET bound_input_hash=? WHERE job_id=? AND unit_key=?",
        ("0" * 64, job.job_id, unit_key),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


@pytest.mark.parametrize("unit_key", VOICE_UNIT_KEYS)
def test_artifact_read_rejects_external_input_lineage_drift_for_every_voice_unit(
    db, unit_key: str
) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    row = db.execute(
        "SELECT external_input_hash FROM job_units WHERE job_id=? AND unit_key=?",
        (job.job_id, unit_key),
    ).fetchone()
    mutated_external = None if row["external_input_hash"] is not None else "e" * 64
    db.execute(
        "UPDATE job_units SET external_input_hash=? WHERE job_id=? AND unit_key=?",
        (mutated_external, job.job_id, unit_key),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_artifact_read_rejects_bound_hash_without_declared_input(db) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET bound_input_hash=? WHERE job_id=? AND ordinal=1",
        (expected_bound_input_hash(None, (), None), job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_artifact_read_rejects_dependency_output_lineage_drift(db) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute(
        "UPDATE job_units SET output_hash=? WHERE job_id=? AND ordinal=2",
        ("f" * 64, job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


@pytest.mark.parametrize(
    ("job_status", "unit_status"),
    (
        (JobStatus.QUEUED, "running"),
        (JobStatus.QUEUED, "failed"),
        (JobStatus.RUNNING, "failed"),
        (JobStatus.PAUSED, "running"),
        (JobStatus.STOPPED, "running"),
        (JobStatus.RETRYING, "running"),
        (JobStatus.RETRYING, "failed"),
    ),
)
def test_job_read_rejects_impossible_job_and_active_unit_relationship(
    db, job_status: JobStatus, unit_status: str
) -> None:
    job = seed_job(db)
    bound_input_hash = canonical_root_bound_input(db, job.job_id)
    if unit_status == "running":
        db.execute(
            """
            UPDATE job_units
            SET status='running', attempt_count=1, external_input_hash=NULL,
                bound_input_hash=?, output_hash=NULL, error_code=NULL,
                started_at=?, finished_at=NULL
            WHERE job_id=? AND ordinal=1
            """,
            (bound_input_hash, utc_iso(NOW), job.job_id),
        )
    else:
        db.execute(
            """
            UPDATE job_units
            SET status='failed', attempt_count=1, external_input_hash=NULL,
                bound_input_hash=?, output_hash=NULL, error_code='VOICE_FAILED',
                started_at=?, finished_at=?
            WHERE job_id=? AND ordinal=1
            """,
            (bound_input_hash, utc_iso(NOW), utc_iso(NOW), job.job_id),
        )
    db.execute(
        "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
        (job_status.value, utc_iso(NOW), job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).list_runnable_job_ids()


@pytest.mark.parametrize(
    "job_status", (JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED)
)
def test_job_read_rejects_request_state_without_running_unit(
    db, job_status: JobStatus
) -> None:
    job = seed_job(db)
    db.execute(
        "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
        (job_status.value, utc_iso(NOW), job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).list_runnable_job_ids()


@pytest.mark.parametrize(
    ("attempt_count", "started_at", "finished_at"),
    (
        (0, utc_iso(NOW), utc_iso(NOW)),
        (1, None, utc_iso(NOW)),
        (
            1,
            "2026-08-22T06:00:00.000000Z",
            "2026-08-22T04:00:00.000000Z",
        ),
        (0, None, utc_iso(NOW)),
    ),
)
def test_succeeded_artifact_read_rejects_impossible_attempt_timestamps(
    db, attempt_count: int, started_at: str | None, finished_at: str
) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute(
        """
        UPDATE job_units
        SET attempt_count=?, started_at=?, finished_at=?
        WHERE job_id=? AND unit_key='voice:score'
        """,
        (attempt_count, started_at, finished_at, job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_require_job_artifacts_does_not_trust_succeeded_status_without_run(db) -> None:
    job = seed_job(db)
    mark_job_succeeded(db, job.job_id)

    with pytest.raises(DomainError, match="VOICE_JOB_ARTIFACTS_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_require_job_artifacts_rejects_voice_proposal_status_hash_drift(db) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute(
        "UPDATE job_units SET output_hash=? "
        "WHERE job_id=? AND unit_key='voice:proposal'",
        ("f" * 64, job.job_id),
    )

    with pytest.raises(DomainError, match="INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_pending_review_requires_canonical_succeeded_artifacts(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    assert VoiceVerificationRepository(db).list_pending_reviews() == ()

    mark_job_succeeded(db, job.job_id)
    pending = VoiceVerificationRepository(db).list_pending_reviews()
    assert tuple(item.id for item in pending) == (run_id,)


def test_pending_review_list_rejects_unknown_stored_status(db) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("UPDATE jobs SET status='corrupt' WHERE id=?", (job.job_id,))

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        VoiceVerificationRepository(db).list_pending_reviews()


def test_pending_review_read_validates_success_unit_external_input_type(db) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET external_input_hash=? "
        "WHERE job_id=? AND ordinal=1",
        (sqlite3.Binary(b"external"), job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        VoiceVerificationRepository(db).list_pending_reviews()


def test_pending_review_read_rejects_malformed_existing_review(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    repository = VoiceVerificationRepository(db, clock=lambda: REVIEWED_AT)
    with transaction(db):
        repository.add_review_and_decision(valid_review(run_id, ReviewAction.HOLD))
    db.execute("DROP TRIGGER voice_verification_reviews_no_update")
    db.execute(
        "UPDATE voice_verification_reviews SET review_hash=? WHERE run_id=?",
        ("0" * 64, run_id),
    )

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        repository.list_pending_reviews()


def test_pending_review_read_rejects_duplicate_existing_reviews(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    repository = VoiceVerificationRepository(db, clock=lambda: REVIEWED_AT)
    with transaction(db):
        repository.add_review_and_decision(valid_review(run_id, ReviewAction.HOLD))
    db.execute(
        "ALTER TABLE voice_verification_reviews "
        "RENAME TO voice_verification_reviews_strict"
    )
    db.execute(
        "CREATE TABLE voice_verification_reviews AS "
        "SELECT * FROM voice_verification_reviews_strict WHERE 0"
    )
    db.execute(
        "INSERT INTO voice_verification_reviews "
        "SELECT * FROM voice_verification_reviews_strict"
    )
    db.execute(
        "INSERT INTO voice_verification_reviews "
        "SELECT * FROM voice_verification_reviews_strict"
    )

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        repository.list_pending_reviews()


def test_review_write_requires_caller_transaction(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)

    with pytest.raises(DomainError, match="TRANSACTION_REQUIRED"):
        VoiceVerificationRepository(db).add_review_and_decision(
            valid_review(run_id, ReviewAction.CONFIRM)
        )


@pytest.mark.parametrize(
    "job_status",
    tuple(status for status in JobStatus if status is not JobStatus.SUCCEEDED),
)
def test_review_write_requires_succeeded_job_before_any_write(
    db, job_status: JobStatus
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    if job_status in {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED}:
        db.execute(
            """
            UPDATE job_units
            SET status='running', attempt_count=1, external_input_hash=NULL,
                bound_input_hash=?, output_hash=NULL, error_code=NULL,
                started_at=?, finished_at=NULL
            WHERE job_id=? AND ordinal=1
            """,
            (
                canonical_root_bound_input(db, job.job_id),
                utc_iso(NOW),
                job.job_id,
            ),
        )
    db.execute(
        "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
        (job_status.value, utc_iso(NOW), job.job_id),
    )
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(DomainError, match="VOICE_REVIEW_INVALID"):
        with transaction(db):
            VoiceVerificationRepository(
                db, clock=lambda: REVIEWED_AT
            ).add_review_and_decision(
                valid_review(run_id, ReviewAction.CONFIRM)
            )

    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_reviews").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0]
        == before_decisions
    )
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0] == job.reference.decision_id


def test_review_write_requires_fully_verified_success_units_before_any_write(
    db,
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET external_input_hash=? "
        "WHERE job_id=? AND ordinal=1",
        (sqlite3.Binary(b"external"), job.job_id),
    )
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        with transaction(db):
            VoiceVerificationRepository(
                db, clock=lambda: REVIEWED_AT
            ).add_review_and_decision(
                valid_review(run_id, ReviewAction.CONFIRM)
            )

    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_reviews").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0]
        == before_decisions
    )
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0] == job.reference.decision_id


@pytest.mark.parametrize("mutation", ("bound_lineage", "attempt_timestamps"))
def test_review_write_rejects_impossible_success_evidence_before_any_write(
    db, mutation: str
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    if mutation == "bound_lineage":
        db.execute("DROP TRIGGER job_units_input_binding_immutable")
        db.execute(
            "UPDATE job_units SET bound_input_hash=? "
            "WHERE job_id=? AND unit_key='audio:cleanup'",
            ("0" * 64, job.job_id),
        )
    else:
        db.execute(
            "UPDATE job_units SET attempt_count=0, started_at=NULL "
            "WHERE job_id=? AND unit_key='voice:score'",
            (job.job_id,),
        )
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        with transaction(db):
            VoiceVerificationRepository(
                db, clock=lambda: REVIEWED_AT
            ).add_review_and_decision(
                valid_review(run_id, ReviewAction.CONFIRM)
            )

    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_reviews").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0]
        == before_decisions
    )
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0] == job.reference.decision_id


@pytest.mark.parametrize(
    ("action", "expected_state"),
    (
        (ReviewAction.CONFIRM, PresenceState.CONFIRMED.value),
        (ReviewAction.REJECT, PresenceState.REJECTED.value),
    ),
)
def test_review_atomically_inserts_decision_and_updates_frozen_pointer(
    db, action: ReviewAction, expected_state: str
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    repository = VoiceVerificationRepository(db, clock=lambda: REVIEWED_AT)

    with transaction(db):
        review_id = repository.add_review_and_decision(valid_review(run_id, action))

    review = db.execute(
        "SELECT * FROM voice_verification_reviews WHERE id=?", (review_id,)
    ).fetchone()
    decision = db.execute(
        "SELECT * FROM presence_decisions WHERE evidence_ref=?",
        (str(review_id),),
    ).fetchone()
    current_id = db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0]
    assert decision["state"] == expected_state
    assert decision["decision_origin"] == "voice_verification"
    assert decision["evidence_hash"] == review["review_hash"]
    assert current_id == decision["id"]
    assert repository.get_run(run_id).id == run_id


def test_hold_review_creates_no_decision_and_does_not_move_pointer(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    repository = VoiceVerificationRepository(db, clock=lambda: REVIEWED_AT)

    with transaction(db):
        review_id = repository.add_review_and_decision(
            valid_review(run_id, ReviewAction.HOLD)
        )

    assert db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0] == 1
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0] == job.reference.decision_id
    assert db.execute(
        "SELECT action FROM voice_verification_reviews WHERE id=?", (review_id,)
    ).fetchone()[0] == "hold"


@pytest.mark.parametrize("action", (ReviewAction.HOLD, ReviewAction.CONFIRM))
def test_review_read_rejects_foreign_candidate_decision_with_same_evidence_ref(
    db, action: ReviewAction
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    repository = VoiceVerificationRepository(db, clock=lambda: REVIEWED_AT)
    with transaction(db):
        review_id = repository.add_review_and_decision(valid_review(run_id, action))
    review_hash = db.execute(
        "SELECT review_hash FROM voice_verification_reviews WHERE id=?",
        (review_id,),
    ).fetchone()[0]
    insert_foreign_voice_decision_for_review(db, review_id, review_hash)

    with pytest.raises(DomainError, match="VOICE_RUN_STORED_INVALID"):
        repository.get_run(run_id)


def test_review_and_decision_rollback_when_pointer_update_fails(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute(
        """
        CREATE TRIGGER inject_review_pointer_failure
        BEFORE UPDATE OF current_presence_decision_id ON subject_video_candidates
        BEGIN SELECT RAISE(ABORT, 'INJECTED_POINTER_FAILURE'); END
        """
    )
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError, match="INJECTED_POINTER_FAILURE"):
        with transaction(db):
            repository = VoiceVerificationRepository(
                db, clock=lambda: REVIEWED_AT
            )
            repository.add_review_and_decision(
                valid_review(run_id, ReviewAction.CONFIRM)
            )

    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_reviews").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT COUNT(*) FROM presence_decisions").fetchone()[0]
        == before_decisions
    )
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates WHERE id=?",
        (job.reference.candidate_id,),
    ).fetchone()[0] == job.reference.decision_id


def test_review_rejects_stale_current_pointer_before_inserting_review(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    db.execute("DROP TRIGGER subject_video_candidates_current_presence_owner_update")
    db.execute(
        "UPDATE subject_video_candidates SET current_presence_decision_id=NULL "
        "WHERE id=?",
        (job.reference.candidate_id,),
    )

    with pytest.raises(DomainError, match="PRESENCE_REVIEW_STALE"):
        with transaction(db):
            repository = VoiceVerificationRepository(
                db, clock=lambda: REVIEWED_AT
            )
            repository.add_review_and_decision(
                valid_review(run_id, ReviewAction.CONFIRM)
            )

    assert (
        db.execute("SELECT COUNT(*) FROM voice_verification_reviews").fetchone()[0]
        == 0
    )
