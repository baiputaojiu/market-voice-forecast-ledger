import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.config import Settings
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
from market_voice_forecast_ledger.services.retention import RetentionService
from market_voice_forecast_ledger.voice import media as voice_media
from market_voice_forecast_ledger.voice.media import AcquiredMedia, NormalizedAudio
from market_voice_forecast_ledger.voice.protocol import (
    AdapterRequest,
    AdapterResponse,
)
from market_voice_forecast_ledger.voice.runtime import RuntimeAttestation
from market_voice_forecast_ledger.workers.presence_verification import (
    PresenceVerificationWorker,
)
from tests.backend.integration.test_presence_pilot import (
    pilot_service,
    seed_pilot_environment,
)
from tests.backend.integration.test_voice_reference_enrollment import (
    FEATURE_BYTES,
    FEATURE_HASH,
    NOW,
    ReferenceSeed,
    add_valid_clip,
    db,
    seed_reference_profile,
)
from tests.backend.voice_fakes import fake_runtime_attestation


REVIEWED_AT = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
VOICE_UNIT_KEYS = tuple(unit_key for unit_key, _ in PRESENCE_UNITS)
ATTEMPT_HISTORY_MUTATIONS = (
    "summary_count",
    "attempt_number",
    "attempt_number_type",
    "terminal_status",
    "invalid_status",
    "started_at",
    "finished_at",
    "timestamp_order",
    "error",
    "output",
    "output_type",
)


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
        SELECT unit_key, ordinal
        FROM job_units WHERE job_id=? ORDER BY ordinal
        """,
        (job_id,),
    ).fetchall()
    service = JobStateService(db, clock=lambda: NOW)
    for row in rows:
        external_input_hash = (
            sha256_text(f"external-{row['ordinal']}")
            if row["ordinal"] % 2 == 0
            else None
        )
        output_hash = (
            run["output_hash"]
            if row["unit_key"] == "voice:proposal" and run is not None
            else sha256_text(f"output-{row['ordinal']}")
        )
        service.begin_unit(
            job_id,
            row["unit_key"],
            external_input_hash=external_input_hash,
        )
        service.complete_unit(job_id, row["unit_key"], output_hash)
    with transaction(db):
        service.succeed_job_in_transaction(job_id)


def mutate_success_attempt(
    db: sqlite3.Connection, job_id: int, mutation: str
) -> None:
    unit_key = "voice:score"
    if mutation == "summary_count":
        db.execute(
            "UPDATE job_units SET attempt_count=2 WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        )
        return
    db.execute("DROP TRIGGER job_unit_attempts_no_update")
    if mutation == "attempt_number":
        assignment = "attempt_no=2"
    elif mutation == "attempt_number_type":
        assignment = "attempt_no=1.5"
    elif mutation == "terminal_status":
        assignment = (
            "result_status='failed', output_hash=NULL, "
            "error_code='VOICE_FAILED'"
        )
    elif mutation == "invalid_status":
        db.execute("PRAGMA ignore_check_constraints=ON")
        assignment = "result_status='unknown'"
    elif mutation == "started_at":
        assignment = "started_at='2026-08-22T03:00:00.000000Z'"
    elif mutation == "finished_at":
        assignment = "finished_at='2026-08-22T05:00:00.000000Z'"
    elif mutation == "timestamp_order":
        assignment = (
            "started_at='2026-08-22T05:00:00.000000Z', "
            "finished_at='2026-08-22T04:00:00.000000Z'"
        )
    elif mutation == "error":
        db.execute("PRAGMA ignore_check_constraints=ON")
        assignment = "error_code='VOICE_FAILED'"
    elif mutation == "output":
        assignment = f"output_hash='{'f' * 64}'"
    elif mutation == "output_type":
        db.execute(
            """
            UPDATE job_unit_attempts SET output_hash=?
            WHERE job_id=? AND unit_key=? AND attempt_no=1
            """,
            (sqlite3.Binary(b"not-a-hash"), job_id, unit_key),
        )
        return
    else:
        raise AssertionError(f"unknown test mutation: {mutation}")
    db.execute(
        f"UPDATE job_unit_attempts SET {assignment} "
        "WHERE job_id=? AND unit_key=? AND attempt_no=1",
        (job_id, unit_key),
    )
    db.execute("PRAGMA ignore_check_constraints=OFF")


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

    service = JobStateService(db, clock=lambda: NOW)
    service.begin_unit(job.job_id, VOICE_UNIT_KEYS[0])
    service.fail_unit(job.job_id, VOICE_UNIT_KEYS[0], "VOICE_FAILED")
    service.resume(job.job_id, {})
    assert service.status(job.job_id) is JobStatus.RETRYING
    assert repository.list_runnable_job_ids() == (job.job_id,)


def test_video_pipeline_transaction_owned_creation_requires_caller_transaction(
    db,
) -> None:
    job = seed_job(db)
    service = JobStateService(db, clock=lambda: REVIEWED_AT)

    with pytest.raises(DomainError) as caught:
        service.create_video_pipeline_in_transaction(
            build_presence_job_manifest(job.snapshot),
            (job.reference.candidate_id,),
            created_at=REVIEWED_AT,
        )
    assert caught.value.code == "JOB_TRANSACTION_REQUIRED"


def test_video_pipeline_transaction_owned_creation_rolls_back_with_caller(
    db,
) -> None:
    job = seed_job(db)
    before_jobs = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    before_sets = db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_binding_sets"
    ).fetchone()[0]
    before_bindings = db.execute(
        "SELECT COUNT(*) FROM video_pipeline_job_bindings"
    ).fetchone()[0]
    service = JobStateService(db, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="injected caller failure"):
        with transaction(db):
            created_job_id = service.create_video_pipeline_in_transaction(
                build_presence_job_manifest(job.snapshot),
                (job.reference.candidate_id,),
                created_at=REVIEWED_AT,
            )
            created = db.execute(
                "SELECT created_at, updated_at FROM jobs WHERE id=?",
                (created_job_id,),
            ).fetchone()
            assert created["created_at"] == utc_iso(REVIEWED_AT)
            assert created["updated_at"] == utc_iso(REVIEWED_AT)
            raise RuntimeError("injected caller failure")

    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
    assert (
        db.execute(
            "SELECT COUNT(*) FROM video_pipeline_job_binding_sets"
        ).fetchone()[0]
        == before_sets
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM video_pipeline_job_bindings"
        ).fetchone()[0]
        == before_bindings
    )


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
    attempts = db.execute(
        """
        SELECT unit.unit_key, unit.attempt_count,
               unit.output_hash AS unit_output_hash,
               attempt.attempt_no, attempt.result_status,
               attempt.output_hash AS attempt_output_hash,
               attempt.error_code, attempt.started_at, attempt.finished_at
        FROM job_units AS unit
        JOIN job_unit_attempts AS attempt
          ON attempt.job_id=unit.job_id AND attempt.unit_key=unit.unit_key
        WHERE unit.job_id=?
        ORDER BY unit.ordinal, attempt.attempt_no
        """,
        (job.job_id,),
    ).fetchall()
    assert tuple(row["unit_key"] for row in attempts) == VOICE_UNIT_KEYS
    assert all(
        row["attempt_count"] == 1
        and row["attempt_no"] == 1
        and row["result_status"] == "success"
        and row["attempt_output_hash"] == row["unit_output_hash"]
        and row["error_code"] is None
        and row["started_at"] == utc_iso(NOW)
        and row["finished_at"] == utc_iso(NOW)
        for row in attempts
    )
    assert artifacts.run is not None and artifacts.run.id == run_id


@pytest.mark.parametrize("mutation", ATTEMPT_HISTORY_MUTATIONS)
def test_artifact_read_rejects_attempt_history_drift(
    db, mutation: str
) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    mutate_success_attempt(db, job.job_id, mutation)

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).require_job_artifacts(job.job_id)


def test_runnable_list_rejects_queued_job_with_completed_attempt(db) -> None:
    job = seed_job(db)
    service = JobStateService(db, clock=lambda: NOW)
    service.begin_unit(job.job_id, VOICE_UNIT_KEYS[0])
    service.complete_unit(job.job_id, VOICE_UNIT_KEYS[0], "a" * 64)
    db.execute(
        "UPDATE jobs SET status='queued', updated_at=? WHERE id=?",
        (utc_iso(NOW), job.job_id),
    )

    with pytest.raises(DomainError, match="VOICE_MANIFEST_STORED_INVALID"):
        VoiceVerificationRepository(db).list_runnable_job_ids()


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


class SimulatedCrash(BaseException):
    pass


def _synthetic_pcm_wav(*, duration_ms: int = 2_000) -> bytes:
    frame_count = 16_000 * duration_ms // 1_000
    pcm = b"\x00\x00" * frame_count
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, 16_000, 32_000, 2, 16, b"data", len(pcm),
    ) + pcm


class FakePresenceMediaAcquirer:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self.calls: list[tuple[str, Path, Path, Path]] = []

    def acquire_registered(
        self,
        video_id: str,
        target_dir: Path,
        *,
        source_path: Path,
        part_path: Path,
    ) -> AcquiredMedia:
        for path in (source_path, part_path):
            row = self._db.execute(
                "SELECT status FROM local_artifacts WHERE local_path=? "
                "ORDER BY id DESC LIMIT 1", (str(path),)
            ).fetchone()
            assert row is not None and row["status"] == "pending"
        self.calls.append((video_id, target_dir, source_path, part_path))
        payload = f"synthetic-media:{video_id}".encode("ascii")
        source_path.write_bytes(payload)
        return AcquiredMedia(
            path=source_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            video_id=video_id,
        )


class FakePresenceMediaNormalizer:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self.calls: list[tuple[Path, Path]] = []

    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio:
        row = self._db.execute(
            "SELECT status FROM local_artifacts WHERE local_path=? "
            "ORDER BY id DESC LIMIT 1", (str(target),)
        ).fetchone()
        assert row is not None and row["status"] == "pending"
        self.calls.append((source, target))
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        payload = _synthetic_pcm_wav()
        target.write_bytes(payload)
        return NormalizedAudio(
            path=target,
            sha256=hashlib.sha256(payload).hexdigest(),
            source_sha256=source_hash,
        )


class FailingPresenceMediaNormalizer(FakePresenceMediaNormalizer):
    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio:
        self.calls.append((source, target))
        target.write_bytes(b"private-partial-normalized-audio")
        raise DomainError(
            "VOICE_MEDIA_NORMALIZATION_FAILED",
            "private ffmpeg timeout",
        )


class ReentrantPresenceMediaNormalizer(FakePresenceMediaNormalizer):
    def __init__(
        self,
        db: sqlite3.Connection,
        competing_worker: PresenceVerificationWorker,
    ) -> None:
        super().__init__(db)
        self._competing_worker = competing_worker
        self.competing_summary = None

    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio:
        self.competing_summary = self._competing_worker.run_once()
        return super().normalize_registered(source, target)


class MutatingPresenceMediaNormalizer(FakePresenceMediaNormalizer):
    def __init__(self, db: sqlite3.Connection, mutation: str) -> None:
        super().__init__(db)
        self._mutation = mutation

    def normalize_registered(
        self, source: Path, target: Path
    ) -> NormalizedAudio:
        if self._mutation == "begin_to_producer":
            source.write_bytes(b"mutated-before-normalizer-consumption")
        normalized = super().normalize_registered(source, target)
        if self._mutation == "producer_to_completion":
            armed = True

            def mutate_on_completion_begin(statement: str) -> None:
                nonlocal armed
                if armed and statement == "BEGIN IMMEDIATE":
                    armed = False
                    target.write_bytes(b"mutated-after-normalizer-validation")

            self._db.set_trace_callback(mutate_on_completion_begin)
        return normalized


class FakePresenceAdapter:
    def __init__(
        self,
        *,
        segments: tuple[tuple[int, int, float], ...] = (
            (0, 1_000, 0.9), (1_000, 1_900, 0.8)
        ),
        failure: Exception | None = None,
    ) -> None:
        self._segments = segments
        self._failure = failure
        self.calls: list[AdapterRequest] = []

    def score(self, request: AdapterRequest) -> AdapterResponse:
        self.calls.append(request)
        if self._failure is not None:
            raise self._failure
        segment_values: list[dict[str, object]] = []
        for ordinal, (start_ms, end_ms, score) in enumerate(
            self._segments, start=1
        ):
            segment_values.append(
                {
                    "end_ms": end_ms,
                    "evidence_hash": sha256_text(
                        canonical_json(
                            {
                                "audio_sha256": request.audio_sha256,
                                "end_ms": end_ms,
                                "ordinal": ordinal,
                                "raw_score": score,
                                "start_ms": start_ms,
                            }
                        )
                    ),
                    "ordinal": ordinal,
                    "raw_score": score,
                    "start_ms": start_ms,
                }
            )
        maximum = max(item[2] for item in self._segments)
        if maximum >= request.subject_boundary:
            proposal = VoiceProposal.LIKELY_PRESENT
        elif maximum <= request.interviewer_boundary:
            proposal = VoiceProposal.LIKELY_ABSENT
        else:
            proposal = VoiceProposal.NEEDS_REVIEW
        values: dict[str, object] = {
            "adapter_contract_version": request.adapter_contract_version,
            "input_hash": request.input_hash,
            "model_name": request.model_name,
            "model_version": request.model_version,
            "proposal": proposal.value,
            "segments": segment_values,
            "vad_contract_version": request.vad_contract_version,
        }
        values["output_hash"] = sha256_text(canonical_json(values))
        return AdapterResponse.model_validate_json(
            canonical_json(values), strict=True
        )


class PartialPresenceAdapter(FakePresenceAdapter):
    def score(self, request: AdapterRequest) -> AdapterResponse:
        self.calls.append(request)
        return cast(AdapterResponse, object())


@dataclass(frozen=True, slots=True)
class PresenceWorkerHarness:
    worker: PresenceVerificationWorker
    acquirer: FakePresenceMediaAcquirer
    normalizer: FakePresenceMediaNormalizer
    adapter: FakePresenceAdapter
    runtime: RuntimeAttestation
    work_root: Path


_PRESENCE_RUNTIMES: dict[Path, RuntimeAttestation] = {}


def presence_worker_harness(
    db: sqlite3.Connection,
    tmp_path: Path,
    job: JobSeed,
    *,
    adapter: FakePresenceAdapter | None = None,
    normalizer: FakePresenceMediaNormalizer | None = None,
    runtime: RuntimeAttestation | None = None,
    after_unit_committed: Callable[[int, str], None] | None = None,
) -> PresenceWorkerHarness:
    work_root = tmp_path / "presence-audio"
    work_root.mkdir(exist_ok=True)
    effective_runtime = runtime
    if effective_runtime is None:
        effective_runtime = _PRESENCE_RUNTIMES.get(tmp_path)
    if effective_runtime is None:
        generated_runtime, _ = fake_runtime_attestation(tmp_path / "runtime")
        effective_runtime = replace(
            generated_runtime,
            model_name=job.snapshot.model_name,
            model_version=job.snapshot.model_version,
            adapter_contract_version=job.snapshot.adapter_version,
            vad_contract_version=job.snapshot.vad_contract_version,
        )
        _PRESENCE_RUNTIMES[tmp_path] = effective_runtime
    acquirer = FakePresenceMediaAcquirer(db)
    effective_normalizer = normalizer or FakePresenceMediaNormalizer(db)
    effective_adapter = adapter or FakePresenceAdapter()
    settings = Settings(
        data_dir=work_root.parent,
        database_path=tmp_path / "unused.sqlite3",
        temp_audio_dir=work_root,
    )
    worker = PresenceVerificationWorker(
        db,
        media_acquirer=acquirer,
        media_normalizer=effective_normalizer,
        adapter=effective_adapter,
        retention=RetentionService(db, settings, clock=lambda: NOW),
        runtime=effective_runtime,
        temp_audio_root=work_root,
        clock=lambda: NOW,
        after_unit_committed=after_unit_committed,
    )
    return PresenceWorkerHarness(
        worker, acquirer, effective_normalizer, effective_adapter,
        effective_runtime, work_root
    )


def _assert_current_presence_unverified(
    db: sqlite3.Connection, candidate_id: int
) -> None:
    row = db.execute(
        "SELECT decision.state FROM subject_video_candidates AS candidate "
        "JOIN presence_decisions AS decision "
        "ON decision.id=candidate.current_presence_decision_id "
        "WHERE candidate.id=?", (candidate_id,)
    ).fetchone()
    assert row is not None and row["state"] == "presence_unverified"


@pytest.mark.parametrize(
    ("lower_status", "higher_status"),
    (
        ("queued", "failed"),
        ("queued", "retrying"),
        ("failed", "queued"),
        ("retrying", "queued"),
    ),
)
def test_worker_selects_lowest_id_across_queued_failed_and_retrying_jobs(
    db,
    tmp_path: Path,
    lower_status: str,
    higher_status: str,
) -> None:
    seed_pilot_environment(db)
    service = pilot_service(db)
    preview = service.preview_pilot()
    creation = service.create_pilot(preview.preview_hash)
    lower_id, higher_id = creation.job_ids[:2]
    jobs = JobStateService(db, clock=lambda: NOW)
    for job_id, status in (
        (lower_id, lower_status),
        (higher_id, higher_status),
    ):
        if status == "queued":
            continue
        artifacts = VoiceVerificationRepository(db).require_job_artifacts(job_id)
        external_hash = sha256_text(
            canonical_json(
                {
                    "manifest_hash": artifacts.manifest.manifest_hash,
                    "presence_decision_hash": (
                        artifacts.manifest.snapshot.presence_decision_hash
                    ),
                    "reference_feature_hash": (
                        artifacts.reference.feature.feature_sha256
                    ),
                    "schema": "presence-worker-external-input.v1",
                    "unit_key": "video:validate",
                }
            )
        )
        jobs.begin_unit(job_id, "video:validate", external_hash)
        jobs.fail_unit(job_id, "video:validate", "VOICE_PROCESSING_FAILED")
        if status == "retrying":
            jobs.resume(job_id, {})
    manifest = VoiceVerificationRepository(db).get_manifest_for_job(lower_id)
    harness = presence_worker_harness(
        db, tmp_path, SimpleNamespace(snapshot=manifest.snapshot)
    )

    summary = harness.worker.run_once()

    assert summary.job_id == lower_id
    assert JobStateService(db).status(higher_id).value == higher_status


def test_second_connection_does_not_recover_a_live_external_unit(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    database_path = Path(
        db.execute("PRAGMA database_list").fetchone()["file"]
    )
    competing_db = open_database(database_path)
    try:
        competing = presence_worker_harness(competing_db, tmp_path, job)
        reentrant = ReentrantPresenceMediaNormalizer(db, competing.worker)
        active = presence_worker_harness(
            db, tmp_path, job, normalizer=reentrant
        )

        summary = active.worker.run_once()

        assert summary.succeeded_jobs == 1
        assert reentrant.competing_summary is not None
        assert reentrant.competing_summary.job_id is None
        assert reentrant.competing_summary.failed_jobs == 0
        assert competing.acquirer.calls == []
        assert competing.normalizer.calls == []
        assert competing.adapter.calls == []
        rows = tuple(
            db.execute(
                "SELECT status, attempt_count FROM job_units "
                "WHERE job_id=? ORDER BY ordinal", (job.job_id,)
            )
        )
        assert all(row["status"] == "success" for row in rows)
        assert all(row["attempt_count"] == 1 for row in rows)
    finally:
        competing_db.close()


@pytest.mark.parametrize("crash_after", VOICE_UNIT_KEYS)
def test_worker_restarts_only_unverified_suffix(
    db, tmp_path: Path, crash_after: str
) -> None:
    job = seed_job(db)

    def crash(_job_id: int, unit_key: str) -> None:
        if unit_key == crash_after:
            raise SimulatedCrash(unit_key)

    first = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=crash
    )
    with pytest.raises(SimulatedCrash):
        first.worker.run_once()

    second = presence_worker_harness(db, tmp_path, job)
    summary = second.worker.run_once()

    assert summary.job_id == job.job_id
    assert summary.succeeded_jobs == 1
    assert summary.failed_code is None
    rows = tuple(
        db.execute(
            "SELECT unit_key, status, attempt_count, output_hash "
            "FROM job_units WHERE job_id=? ORDER BY ordinal", (job.job_id,)
        )
    )
    assert tuple(row["unit_key"] for row in rows) == VOICE_UNIT_KEYS
    assert all(row["status"] == "success" for row in rows)
    assert all(row["attempt_count"] == 1 for row in rows)
    assert all(row["output_hash"] is not None for row in rows)
    assert JobStateService(db).status(job.job_id) is JobStatus.SUCCEEDED
    _assert_current_presence_unverified(db, job.reference.candidate_id)


def test_recovery_reuses_verified_prefix_and_resets_first_mismatch_suffix(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)

    def crash(_job_id: int, unit_key: str) -> None:
        if unit_key == "voice:score":
            raise SimulatedCrash(unit_key)

    first = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=crash
    )
    with pytest.raises(SimulatedCrash):
        first.worker.run_once()
    source_path = first.acquirer.calls[0][2]
    source_path.write_bytes(b"corrupt-source")

    second = presence_worker_harness(db, tmp_path, job)
    plan = second.worker.recover_job(job.job_id)

    assert plan.reused_unit_keys == ("video:validate",)
    assert plan.next_unit_key == "audio:acquire"
    assert plan.pending_unit_keys == VOICE_UNIT_KEYS[1:]
    assert not source_path.exists()
    assert second.worker.run_once().succeeded_jobs == 1


def test_recovery_does_not_rebind_external_input_after_model_drift(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)

    def crash(_job_id: int, unit_key: str) -> None:
        if unit_key == "voice:score":
            raise SimulatedCrash(unit_key)

    first = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=crash
    )
    with pytest.raises(SimulatedCrash):
        first.worker.run_once()
    original = db.execute(
        "SELECT external_input_hash FROM job_units "
        "WHERE job_id=? AND unit_key='voice:score'", (job.job_id,)
    ).fetchone()[0]
    drifted = replace(first.runtime, model_sha256="f" * 64)

    second = presence_worker_harness(db, tmp_path, job, runtime=drifted)
    plan = second.worker.recover_job(job.job_id)
    assert plan.reused_unit_keys == VOICE_UNIT_KEYS[:4]
    assert plan.next_unit_key == "voice:score"
    summary = second.worker.run_once()

    assert summary.failed_code == "VOICE_PROCESSING_FAILED"
    assert db.execute(
        "SELECT external_input_hash FROM job_units "
        "WHERE job_id=? AND unit_key='voice:score'", (job.job_id,)
    ).fetchone()[0] == original
    _assert_current_presence_unverified(db, job.reference.candidate_id)


def test_adapter_response_is_not_adopted_before_proposal_transaction(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)

    def crash(_job_id: int, unit_key: str) -> None:
        if unit_key == "voice:score":
            raise SimulatedCrash(unit_key)

    harness = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=crash
    )
    with pytest.raises(SimulatedCrash):
        harness.worker.run_once()
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_runs"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_segments"
    ).fetchone()[0] == 0


def test_proposal_run_segments_and_unit_success_are_atomic(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    db.execute(
        "CREATE TEMP TRIGGER fail_second_worker_segment "
        "BEFORE INSERT ON voice_verification_segments "
        "WHEN NEW.ordinal=2 BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
    )
    harness = presence_worker_harness(db, tmp_path, job)

    summary = harness.worker.run_once()

    assert summary.failed_code == "VOICE_PROCESSING_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_runs"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_segments"
    ).fetchone()[0] == 0
    proposal = db.execute(
        "SELECT status, output_hash FROM job_units "
        "WHERE job_id=? AND unit_key='voice:proposal'", (job.job_id,)
    ).fetchone()
    assert tuple(proposal) == ("failed", None)
    db.execute("DROP TRIGGER fail_second_worker_segment")

    retry = presence_worker_harness(db, tmp_path, job).worker.run_once()
    assert retry.succeeded_jobs == 1


def test_cleanup_failure_keeps_job_failed_and_records_retry(
    db, tmp_path: Path, monkeypatch
) -> None:
    job = seed_job(db)
    calls = 0

    def deny_first(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("private path must not escape")
        path.unlink()

    monkeypatch.setattr(
        "market_voice_forecast_ledger.services.retention._unlink_retained_audio_path",
        deny_first,
    )
    harness = presence_worker_harness(db, tmp_path, job)

    summary = harness.worker.run_once()

    assert summary.failed_code == "AUDIO_DELETE_PERMISSION"
    assert JobStateService(db).status(job.job_id) is JobStatus.FAILED
    _assert_current_presence_unverified(db, job.reference.candidate_id)
    failed = db.execute(
        "SELECT retry_count, safe_error_code FROM local_artifacts "
        "WHERE status='delete_failed'"
    ).fetchone()
    assert tuple(failed) == (1, "AUDIO_DELETE_PERMISSION")

    monkeypatch.setattr(
        "market_voice_forecast_ledger.services.retention._unlink_retained_audio_path",
        Path.unlink,
    )
    retry = presence_worker_harness(db, tmp_path, job).worker.run_once()
    assert retry.succeeded_jobs == 1
    rows = tuple(db.execute("SELECT local_path, status FROM local_artifacts"))
    assert rows and all(row["status"] == "deleted" for row in rows)
    assert all(not Path(row["local_path"]).exists() for row in rows)


def test_normalization_timeout_discards_partial_artifact_before_retry(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    failing = FailingPresenceMediaNormalizer(db)
    first = presence_worker_harness(
        db, tmp_path, job, normalizer=failing
    )

    summary = first.worker.run_once()

    assert summary.failed_code == "VOICE_MEDIA_NORMALIZATION_FAILED"
    partial_path = failing.calls[0][1]
    assert partial_path.read_bytes() == b"private-partial-normalized-audio"
    assert db.execute(
        "SELECT status FROM local_artifacts WHERE local_path=?",
        (str(partial_path),),
    ).fetchone()[0] == "pending"

    retry = presence_worker_harness(db, tmp_path, job).worker.run_once()

    assert retry.succeeded_jobs == 1
    normalized_attempt = db.execute(
        "SELECT attempt_count FROM job_units "
        "WHERE job_id=? AND unit_key='audio:normalize'",
        (job.job_id,),
    ).fetchone()[0]
    assert normalized_attempt == 2
    rows = tuple(
        db.execute(
            "SELECT status FROM local_artifacts WHERE local_path=? ORDER BY id",
            (str(partial_path),),
        )
    )
    assert tuple(row["status"] for row in rows) == ("deleted", "deleted")
    assert not partial_path.exists()


@pytest.mark.parametrize(
    "mutation",
    ("hash_to_begin", "begin_to_producer", "producer_to_completion"),
)
def test_normalization_never_succeeds_with_stale_consumed_bytes(
    db, tmp_path: Path, mutation: str
) -> None:
    job = seed_job(db)
    normalizer = MutatingPresenceMediaNormalizer(db, mutation)

    def arm_hash_to_begin_mutation(job_id: int, unit_key: str) -> None:
        if mutation != "hash_to_begin" or unit_key != "audio:acquire":
            return
        source = (
            tmp_path
            / "presence-audio"
            / f"presence-job-{job_id:020d}"
            / "source.media"
        )
        armed = True

        def mutate_on_begin(statement: str) -> None:
            nonlocal armed
            if armed and statement == "BEGIN IMMEDIATE":
                armed = False
                source.write_bytes(b"mutated-after-external-input-hash")

        db.set_trace_callback(mutate_on_begin)

    harness = presence_worker_harness(
        db,
        tmp_path,
        job,
        normalizer=normalizer,
        after_unit_committed=arm_hash_to_begin_mutation,
    )
    try:
        summary = harness.worker.run_once()
    finally:
        db.set_trace_callback(None)

    assert summary.succeeded_jobs == 0
    assert JobStateService(db).status(job.job_id) is JobStatus.FAILED
    normalized = JobStateService(db).unit(job.job_id, "audio:normalize")
    assert normalized.status.value == "failed"
    assert normalized.output_hash is None
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_runs"
    ).fetchone()[0] == 0
    _assert_current_presence_unverified(db, job.reference.candidate_id)


@pytest.mark.parametrize(
    ("boundary_action", "expected_status"),
    (("pause", JobStatus.PAUSED), ("stop", JobStatus.STOPPED)),
)
def test_worker_honors_pause_and_stop_at_committed_unit_boundary(
    db, tmp_path: Path, boundary_action: str, expected_status: JobStatus
) -> None:
    job = seed_job(db)

    def request_boundary(job_id: int, unit_key: str) -> None:
        if unit_key != "video:validate":
            return
        service = JobStateService(db, clock=lambda: NOW)
        if boundary_action == "pause":
            service.request_pause(job_id)
        else:
            service.request_stop(job_id)

    harness = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=request_boundary
    )
    summary = harness.worker.run_once()

    assert summary.succeeded_jobs == 0
    assert summary.failed_code is None
    assert JobStateService(db).status(job.job_id) is expected_status
    assert harness.acquirer.calls == []
    _assert_current_presence_unverified(db, job.reference.candidate_id)


@pytest.mark.parametrize(
    ("boundary_action", "expected_status", "summary_field"),
    (
        ("pause", JobStatus.PAUSED, "paused_jobs"),
        ("stop", JobStatus.STOPPED, "stopped_jobs"),
    ),
)
def test_requested_boundary_settles_before_adapter_verification(
    db,
    tmp_path: Path,
    boundary_action: str,
    expected_status: JobStatus,
    summary_field: str,
) -> None:
    job = seed_job(db)
    crashing = FakePresenceAdapter(
        failure=cast(Exception, SimulatedCrash("voice:vad"))
    )
    first = presence_worker_harness(db, tmp_path, job, adapter=crashing)
    with pytest.raises(SimulatedCrash):
        first.worker.run_once()
    assert JobStateService(db).unit(
        job.job_id, "voice:vad"
    ).status.value == "running"
    jobs = JobStateService(db, clock=lambda: NOW)
    if boundary_action == "pause":
        jobs.request_pause(job.job_id)
    else:
        jobs.request_stop(job.job_id)
    replacement = presence_worker_harness(db, tmp_path, job)

    summary = replacement.worker.run_once()

    assert JobStateService(db).status(job.job_id) is expected_status
    assert getattr(summary, summary_field) == 1
    assert replacement.acquirer.calls == []
    assert replacement.normalizer.calls == []
    assert replacement.adapter.calls == []
    _assert_current_presence_unverified(db, job.reference.candidate_id)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (
            RuntimeError("C:/private/audio/secret.wav provider body"),
            "VOICE_PROCESSING_FAILED",
        ),
        (
            DomainError(
                "VOICE_ADAPTER_PROCESS_FAILED",
                "private child timeout at C:/private/audio.wav",
            ),
            "VOICE_ADAPTER_PROCESS_FAILED",
        ),
        (
            DomainError(
                "VOICE_ADAPTER_RESPONSE_INVALID",
                "missing speech in private adapter response",
            ),
            "VOICE_ADAPTER_RESPONSE_INVALID",
        ),
    ),
)
def test_worker_maps_private_failures_to_constant_safe_codes(
    db,
    tmp_path: Path,
    failure: Exception,
    expected_code: str,
) -> None:
    job = seed_job(db)
    adapter = FakePresenceAdapter(failure=failure)
    harness = presence_worker_harness(db, tmp_path, job, adapter=adapter)

    summary = harness.worker.run_once()

    assert summary.failed_code == expected_code
    error = db.execute(
        "SELECT error_code FROM job_units WHERE job_id=? AND status='failed'",
        (job.job_id,),
    ).fetchone()[0]
    assert error == expected_code
    assert "private" not in repr(summary).lower()
    _assert_current_presence_unverified(db, job.reference.candidate_id)


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    (
        (
            RuntimeError("PRIVATE_STATUS_SENTINEL C:/private/audio.wav"),
            "VOICE_PROCESSING_FAILED",
        ),
        (
            DomainError(
                "VOICE_ADAPTER_PROCESS_FAILED",
                "PRIVATE_STATUS_SENTINEL C:/private/audio.wav",
            ),
            "VOICE_ADAPTER_PROCESS_FAILED",
        ),
    ),
)
def test_run_once_sanitizes_complete_status_boundary(
    db,
    tmp_path: Path,
    monkeypatch,
    cause: Exception,
    expected_code: str,
) -> None:
    job = seed_job(db)
    harness = presence_worker_harness(db, tmp_path, job)

    def fail_status(_job_id: int) -> JobStatus:
        raise cause

    monkeypatch.setattr(harness.worker._jobs, "status", fail_status)

    summary = harness.worker.run_once()

    assert summary.failed_code == expected_code
    assert "PRIVATE_STATUS_SENTINEL" not in repr(summary)
    assert "private" not in repr(summary).lower()


def test_run_once_sanitizes_closed_connection_during_selection(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    database_path = Path(
        db.execute("PRAGMA database_list").fetchone()["file"]
    )
    closed = open_database(database_path)
    harness = presence_worker_harness(closed, tmp_path, job)
    closed.close()

    summary = harness.worker.run_once()

    assert summary.failed_code == "VOICE_PROCESSING_FAILED"
    assert "closed" not in repr(summary).lower()
    assert "database" not in repr(summary).lower()


@pytest.mark.parametrize(
    ("cause", "expected_code"),
    (
        (
            RuntimeError("PRIVATE_RECOVERY_SENTINEL C:/private/audio.wav"),
            "VOICE_PROCESSING_FAILED",
        ),
        (
            DomainError(
                "VOICE_ADAPTER_RESPONSE_INVALID",
                "PRIVATE_RECOVERY_SENTINEL C:/private/audio.wav",
            ),
            "VOICE_ADAPTER_RESPONSE_INVALID",
        ),
    ),
)
def test_recover_job_sanitizes_complete_public_boundary(
    db,
    tmp_path: Path,
    monkeypatch,
    cause: Exception,
    expected_code: str,
) -> None:
    job = seed_job(db)
    jobs = JobStateService(db, clock=lambda: NOW)
    jobs.begin_unit(job.job_id, "video:validate")
    jobs.fail_unit(job.job_id, "video:validate", "VOICE_PROCESSING_FAILED")
    harness = presence_worker_harness(db, tmp_path, job)

    def fail_recovery(_job_id: int):
        raise cause

    monkeypatch.setattr(harness.worker, "_canonical_artifacts", fail_recovery)

    with pytest.raises(DomainError) as caught:
        harness.worker.recover_job(job.job_id)

    assert caught.value.code == expected_code
    assert "PRIVATE_RECOVERY_SENTINEL" not in str(caught.value)
    assert "private" not in str(caught.value).lower()


def test_worker_rejects_partial_adapter_response_before_any_run_write(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    harness = presence_worker_harness(
        db, tmp_path, job, adapter=PartialPresenceAdapter()
    )

    summary = harness.worker.run_once()

    assert summary.failed_code == "VOICE_ADAPTER_RESPONSE_INVALID"
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_runs"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_segments"
    ).fetchone()[0] == 0
    _assert_current_presence_unverified(db, job.reference.candidate_id)


def test_corrupt_stored_run_is_never_adopted_or_exposed(
    db, tmp_path: Path
) -> None:
    job = seed_job(db)
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    def crash(_job_id: int, unit_key: str) -> None:
        if unit_key == "voice:proposal":
            raise SimulatedCrash(unit_key)

    first = presence_worker_harness(
        db, tmp_path, job, after_unit_committed=crash
    )
    with pytest.raises(SimulatedCrash):
        first.worker.run_once()
    db.execute("DROP TRIGGER voice_verification_runs_no_update")
    db.execute(
        "UPDATE voice_verification_runs SET output_hash=? WHERE job_id=?",
        ("f" * 64, job.job_id),
    )

    summary = presence_worker_harness(db, tmp_path, job).worker.run_once()

    assert summary.failed_code == "VOICE_PROCESSING_FAILED"
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0
    _assert_current_presence_unverified(db, job.reference.candidate_id)
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


@pytest.mark.parametrize("mutation", ATTEMPT_HISTORY_MUTATIONS)
def test_review_write_rejects_attempt_history_drift_before_any_write(
    db, mutation: str
) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    mutate_success_attempt(db, job.job_id, mutation)
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
