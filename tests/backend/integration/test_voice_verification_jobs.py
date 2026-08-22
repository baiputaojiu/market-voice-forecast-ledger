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
from market_voice_forecast_ledger.domain.discovery import PresenceState
from market_voice_forecast_ledger.domain.enums import JobStatus
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.voice_verification import (
    ReviewAction,
    VoiceManifestSnapshot,
    VoiceProposal,
    VoiceRunResult,
    VoiceSegmentScore,
    build_presence_job_manifest,
)
from market_voice_forecast_ledger.repositories.voice_verification import (
    VoiceVerificationRepository,
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
        "SELECT unit_key, ordinal FROM job_units WHERE job_id=? ORDER BY ordinal",
        (job_id,),
    ).fetchall()
    for row in rows:
        db.execute(
            """
            UPDATE job_units
            SET external_input_hash=NULL, bound_input_hash=?, output_hash=?,
                status='success', error_code=NULL, started_at=?, finished_at=?
            WHERE job_id=? AND unit_key=?
            """,
            (
                sha256_text(f"bound-{row['ordinal']}"),
                (
                    run["output_hash"]
                    if row["unit_key"] == "voice:proposal" and run is not None
                    else sha256_text(f"output-{row['ordinal']}")
                ),
                utc_iso(NOW),
                utc_iso(NOW),
                job_id,
                row["unit_key"],
            ),
        )
    db.execute(
        "UPDATE jobs SET status='succeeded', updated_at=? WHERE id=?",
        (utc_iso(NOW), job_id),
    )


def valid_review(run_id: int, action: ReviewAction) -> ReviewCommand:
    return ReviewCommand(
        run_id=run_id,
        action=action,
        reason="listened to the cited segment",
        actor="local_user",
    )


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

    with pytest.raises(DomainError, match="VOICE_JOB_ARTIFACTS_INVALID"):
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


def test_review_write_requires_caller_transaction(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)

    with pytest.raises(DomainError, match="TRANSACTION_REQUIRED"):
        VoiceVerificationRepository(db).add_review_and_decision(
            valid_review(run_id, ReviewAction.CONFIRM)
        )


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
