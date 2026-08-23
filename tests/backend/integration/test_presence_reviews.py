import json
import os
import queue
import sqlite3
import threading
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, cast, get_type_hints

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text
from market_voice_forecast_ledger.domain.discovery import PresenceState
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import effective_input_hash
from market_voice_forecast_ledger.domain.voice_verification import (
    ReviewAction,
    VoiceProposal,
)
from market_voice_forecast_ledger.services.voice_verification import (
    PresenceVerificationService,
    ReviewCommand,
    ReviewDetail,
    ReviewResult,
    ReviewSegmentDetail,
)
from tests.backend.integration.test_voice_reference_enrollment import (
    FEATURE_BYTES,
    FEATURE_HASH,
    NOW,
    db,
)
from tests.backend.integration.test_voice_verification_jobs import (
    FakePresenceAdapter,
    JobSeed,
    move_candidate_pointer_from_frozen_decision,
    presence_worker_harness,
    seed_job,
)


REVIEW_REASON = "listened to the cited segment"


def review_service(db: sqlite3.Connection) -> PresenceVerificationService:
    return PresenceVerificationService(db, clock=lambda: NOW)


def successful_run(
    db: sqlite3.Connection,
    tmp_path: Path,
    *,
    adapter: FakePresenceAdapter | None = None,
) -> tuple[JobSeed, int]:
    job = seed_job(db)
    summary = presence_worker_harness(
        db,
        tmp_path,
        job,
        adapter=adapter,
    ).worker.run_once()
    assert summary.job_id == job.job_id
    assert summary.succeeded_jobs == 1
    assert summary.failed_jobs == 0
    run_id = db.execute(
        "SELECT id FROM voice_verification_runs WHERE job_id=?",
        (job.job_id,),
    ).fetchone()[0]
    artifacts = tuple(
        db.execute(
            "SELECT local_path, status FROM local_artifacts ORDER BY id"
        )
    )
    assert artifacts
    assert all(item["status"] == "deleted" for item in artifacts)
    assert all(not os.path.lexists(item["local_path"]) for item in artifacts)
    return job, run_id


def command(
    run_id: int,
    action: ReviewAction,
    *,
    reason: str = REVIEW_REASON,
) -> ReviewCommand:
    return ReviewCommand(
        run_id=run_id,
        action=action,
        reason=reason,
        actor="local_user",
    )


def current_presence(
    db: sqlite3.Connection,
    candidate_id: int,
) -> sqlite3.Row:
    row = db.execute(
        """
        SELECT decision.*
        FROM subject_video_candidates AS candidate
        JOIN presence_decisions AS decision
          ON decision.id=candidate.current_presence_decision_id
        WHERE candidate.id=?
        """,
        (candidate_id,),
    ).fetchone()
    assert row is not None
    return row


def assert_no_private_exception_chain(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = repr(error)
    assert "PRIVATE_" not in rendered
    assert "C:/private" not in rendered
    assert "embedding" not in rendered.casefold()


def assert_review_unchanged(
    db: sqlite3.Connection,
    job: JobSeed,
    *,
    decision_count: int = 1,
) -> None:
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0] == decision_count
    assert current_presence(db, job.reference.candidate_id)["id"] == (
        job.reference.decision_id
    )


def replace_cleanup_output_with_current_inventory(
    db: sqlite3.Connection,
    job_id: int,
) -> None:
    artifacts = tuple(
        db.execute(
            "SELECT id, deleted_at, retry_count "
            "FROM local_artifacts ORDER BY id"
        )
    )
    output_hash = sha256_text(
        canonical_json(
            {
                "artifacts": [
                    {
                        "deleted_at": datetime.fromisoformat(
                            item["deleted_at"].replace("Z", "+00:00")
                        ).isoformat(),
                        "id": item["id"],
                        "retry_count": item["retry_count"],
                    }
                    for item in artifacts
                ],
                "schema": "presence-audio-cleanup.v1",
            }
        )
    )
    db.execute("DROP TRIGGER job_unit_attempts_no_update")
    db.execute(
        "UPDATE job_units SET output_hash=? "
        "WHERE job_id=? AND unit_key='audio:cleanup'",
        (output_hash, job_id),
    )
    db.execute(
        "UPDATE job_unit_attempts SET output_hash=? "
        "WHERE job_id=? AND unit_key='audio:cleanup'",
        (output_hash, job_id),
    )


def replace_cleanup_external_hash(
    db: sqlite3.Connection,
    job_id: int,
) -> None:
    external_hash = "f" * 64
    cleanup = db.execute(
        "SELECT declared_input_hash, dependency_keys_json "
        "FROM job_units WHERE job_id=? AND unit_key='audio:cleanup'",
        (job_id,),
    ).fetchone()
    dependency_outputs = tuple(
        db.execute(
            "SELECT output_hash FROM job_units WHERE job_id=? AND unit_key=?",
            (job_id, unit_key),
        ).fetchone()[0]
        for unit_key in json.loads(cleanup["dependency_keys_json"])
    )
    bound_hash = effective_input_hash(
        cleanup["declared_input_hash"],
        dependency_outputs,
        external_hash,
    )
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET external_input_hash=?, bound_input_hash=? "
        "WHERE job_id=? AND unit_key='audio:cleanup'",
        (external_hash, bound_hash, job_id),
    )


def test_model_proposal_never_changes_pointer_and_detail_is_public_safe(
    db,
    tmp_path: Path,
) -> None:
    adapter = FakePresenceAdapter(
        segments=((0, 1_001, 0.912345), (1_001, 1_901, 0.812345))
    )
    job, run_id = successful_run(db, tmp_path, adapter=adapter)

    detail = review_service(db).show_review(run_id)

    assert type(detail) is ReviewDetail
    assert tuple(item.name for item in fields(ReviewDetail)) == (
        "person_display_name",
        "watch_url",
        "youtube_video_id",
        "segments",
        "proposal",
        "model_name",
        "model_version",
        "adapter_version",
        "threshold_version",
    )
    assert tuple(item.name for item in fields(ReviewSegmentDetail)) == (
        "start_ms",
        "end_ms",
        "score",
    )
    assert detail == ReviewDetail(
        person_display_name="木野内栄治",
        watch_url="https://www.youtube.com/watch?v=abcdefghijk",
        youtube_video_id="abcdefghijk",
        segments=(
            ReviewSegmentDetail(start_ms=0, end_ms=1_001, score=0.9123),
            ReviewSegmentDetail(start_ms=1_001, end_ms=1_901, score=0.8123),
        ),
        proposal=VoiceProposal.LIKELY_PRESENT,
        model_name="speaker-model",
        model_version="1.0",
        adapter_version="adapter-v1",
        threshold_version="voice-threshold-v1",
    )
    assert current_presence(db, job.reference.candidate_id)["state"] == (
        PresenceState.UNVERIFIED.value
    )
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0


def test_pending_reviews_are_canonical_details_and_reviewed_runs_leave_queue(
    db,
    tmp_path: Path,
) -> None:
    _job, run_id = successful_run(db, tmp_path)
    service = review_service(db)

    assert service.list_pending_reviews() == (service.show_review(run_id),)

    result = service.review(command(run_id, ReviewAction.HOLD))

    assert result.current_state is PresenceState.UNVERIFIED
    assert service.list_pending_reviews() == ()
    assert service.show_review(run_id).proposal is VoiceProposal.LIKELY_PRESENT


@pytest.mark.parametrize(
    ("action", "expected_state"),
    (
        (ReviewAction.CONFIRM, PresenceState.CONFIRMED),
        (ReviewAction.REJECT, PresenceState.REJECTED),
    ),
)
def test_human_review_atomically_changes_exact_frozen_pointer(
    db,
    tmp_path: Path,
    action: ReviewAction,
    expected_state: PresenceState,
) -> None:
    job, run_id = successful_run(db, tmp_path)

    result = review_service(db).review(command(run_id, action))

    assert type(result) is ReviewResult
    assert tuple(item.name for item in fields(ReviewResult)) == (
        "review_id",
        "run_id",
        "action",
        "current_presence_decision_id",
        "current_state",
    )
    assert result.run_id == run_id
    assert result.action is action
    assert result.current_state is expected_state
    review = db.execute(
        "SELECT * FROM voice_verification_reviews WHERE id=?",
        (result.review_id,),
    ).fetchone()
    current = current_presence(db, job.reference.candidate_id)
    assert review["action"] == action.value
    assert review["actor"] == "local_user"
    assert review["prior_presence_decision_id"] == job.reference.decision_id
    assert review["prior_presence_decision_hash"] == job.reference.decision_hash
    assert current["id"] == result.current_presence_decision_id
    assert current["state"] == expected_state.value
    assert current["decision_origin"] == "voice_verification"
    assert current["evidence_ref"] == str(result.review_id)
    assert current["evidence_hash"] == review["review_hash"]


def test_hold_records_review_without_decision_or_pointer_movement(
    db,
    tmp_path: Path,
) -> None:
    job, run_id = successful_run(db, tmp_path)

    result = review_service(db).review(command(run_id, ReviewAction.HOLD))

    assert result.action is ReviewAction.HOLD
    assert result.current_state is PresenceState.UNVERIFIED
    assert result.current_presence_decision_id == job.reference.decision_id
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT action FROM voice_verification_reviews WHERE id=?",
        (result.review_id,),
    ).fetchone()[0] == "hold"


@pytest.mark.parametrize(
    "first_action",
    (ReviewAction.CONFIRM, ReviewAction.REJECT, ReviewAction.HOLD),
)
def test_duplicate_review_is_stale_and_never_writes_again(
    db,
    tmp_path: Path,
    first_action: ReviewAction,
) -> None:
    _job, run_id = successful_run(db, tmp_path)
    service = review_service(db)
    first = service.review(command(run_id, first_action))
    before_pointer = first.current_presence_decision_id
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(DomainError) as caught:
        service.review(command(run_id, ReviewAction.CONFIRM))

    assert caught.value.code == "PRESENCE_REVIEW_STALE"
    assert str(caught.value) == "presence review is stale"
    assert_no_private_exception_chain(caught.value)
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0] == before_decisions
    assert db.execute(
        "SELECT current_presence_decision_id FROM subject_video_candidates"
    ).fetchone()[0] == before_pointer


def test_stale_pointer_is_rejected_before_review_write(db, tmp_path: Path) -> None:
    job, run_id = successful_run(db, tmp_path)
    stale_id = move_candidate_pointer_from_frozen_decision(db, job)
    before_decisions = db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0]

    with pytest.raises(DomainError) as caught:
        review_service(db).review(command(run_id, ReviewAction.REJECT))

    assert caught.value.code == "PRESENCE_REVIEW_STALE"
    assert_no_private_exception_chain(caught.value)
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM presence_decisions"
    ).fetchone()[0] == before_decisions
    assert current_presence(db, job.reference.candidate_id)["id"] == stale_id


class _Actor(str):
    pass


@pytest.mark.parametrize(
    "invalid_command",
    (
        object(),
        ReviewCommand(
            run_id=True,
            action=ReviewAction.CONFIRM,
            reason=REVIEW_REASON,
            actor="local_user",
        ),
        ReviewCommand(
            run_id=2**63,
            action=ReviewAction.CONFIRM,
            reason=REVIEW_REASON,
            actor="local_user",
        ),
        ReviewCommand(
            run_id=1,
            action=cast(ReviewAction, "confirm"),
            reason=REVIEW_REASON,
            actor="local_user",
        ),
        ReviewCommand(
            run_id=1,
            action=ReviewAction.CONFIRM,
            reason=REVIEW_REASON,
            actor=cast(Literal["local_user"], _Actor("local_user")),
        ),
        ReviewCommand(
            run_id=1,
            action=ReviewAction.CONFIRM,
            reason=REVIEW_REASON,
            actor=cast(Literal["local_user"], "system"),
        ),
    ),
)
def test_review_requires_exact_command_action_and_actor_types(
    db,
    invalid_command: object,
) -> None:
    assert get_type_hints(ReviewCommand)["actor"] == Literal["local_user"]

    with pytest.raises(DomainError) as caught:
        review_service(db).review(cast(ReviewCommand, invalid_command))

    assert caught.value.code == "PRESENCE_REVIEW_INVALID"
    assert str(caught.value) == "presence review input is invalid"
    assert_no_private_exception_chain(caught.value)
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "reason",
    (
        "",
        "   ",
        "x" * 241,
        "C:/private/audio.wav",
        "PRIVATE\u0000CONTROL",
        "audio_path contains private content",
        "file://private/audio.wav",
        "private-location:" + "\\\\" + "server\\share\\audio.wav",
        "Author" + "ization: Bear" + "er synthetic-private-credential-000001",
        "Author" + "ization: Basic c3ludGhldGljOnByaXZhdGU=",
        "Cook" + "ie: session=synthetic-private-cookie",
        "Set-Cook" + "ie: session=synthetic-private-cookie",
        "provider_api_" + "key=synthetic-private-key",
        "provider API " + "key = synthetic-private-key",
        "access_" + "token: synthetic-private-token",
        "cook" + "ie = session=synthetic-private-cookie",
        "password" + "=synthetic-private-password",
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN ENCRYPTED " + "PRIVATE KEY-----",
    ),
)
def test_review_rejects_empty_long_or_unsafe_reason_before_mutation(
    db,
    tmp_path: Path,
    reason: str,
) -> None:
    job, run_id = successful_run(db, tmp_path)

    with pytest.raises(DomainError) as caught:
        review_service(db).review(
            command(run_id, ReviewAction.CONFIRM, reason=reason)
        )

    assert caught.value.code == "PRESENCE_REVIEW_INVALID"
    assert_no_private_exception_chain(caught.value)
    assert_review_unchanged(db, job)


def test_begin_immediate_serializes_competing_review_and_cas_allows_one(
    db,
    tmp_path: Path,
) -> None:
    job, run_id = successful_run(db, tmp_path)
    database_path = Path(db.execute("PRAGMA database_list").fetchone()["file"])
    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    outcomes: queue.Queue[tuple[str, object, tuple[str, ...]]] = queue.Queue()

    def first_review() -> None:
        conn = open_database(database_path)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            service = review_service(conn)
            original = service._voice.add_review_and_decision

            def hold_transaction(value: object) -> int:
                assert conn.in_transaction
                entered.set()
                assert release.wait(10), "first review was not released"
                return original(value)

            service._voice.add_review_and_decision = hold_transaction
            result = service.review(command(run_id, ReviewAction.CONFIRM))
            outcomes.put(("first", result, tuple(statements)))
        except BaseException as cause:
            outcomes.put(("first_error", cause, tuple(statements)))
        finally:
            conn.close()

    def second_review() -> None:
        assert entered.wait(10), "first review did not acquire transaction"
        conn = open_database(database_path)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            result = review_service(conn).review(
                command(run_id, ReviewAction.REJECT)
            )
            outcomes.put(("second", result, tuple(statements)))
        except BaseException as cause:
            outcomes.put(("second_error", cause, tuple(statements)))
        finally:
            second_done.set()
            conn.close()

    first = threading.Thread(target=first_review)
    second = threading.Thread(target=second_review)
    first.start()
    assert entered.wait(10), "first review did not reach mutation boundary"
    second.start()
    assert not second_done.wait(0.2), "competing review was not serialized"
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive()
    assert not second.is_alive()

    collected: dict[str, tuple[object, tuple[str, ...]]] = {}
    for _ in range(2):
        label, value, statements = outcomes.get(timeout=1)
        collected[label] = (value, statements)
    first_value, first_statements = collected["first"]
    second_error, second_statements = collected["second_error"]
    assert type(first_value) is ReviewResult
    assert isinstance(second_error, DomainError)
    assert second_error.code == "PRESENCE_REVIEW_STALE"
    assert "BEGIN IMMEDIATE" in first_statements
    assert "BEGIN IMMEDIATE" in second_statements
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_reviews"
    ).fetchone()[0] == 1
    assert current_presence(db, job.reference.candidate_id)["state"] == (
        PresenceState.CONFIRMED.value
    )


def _corrupt_status(db: sqlite3.Connection, job: JobSeed, _run_id: int) -> None:
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute("UPDATE jobs SET status='PRIVATE_STATUS' WHERE id=?", (job.job_id,))
    db.execute("PRAGMA ignore_check_constraints=OFF")


def _corrupt_unit(db: sqlite3.Connection, job: JobSeed, _run_id: int) -> None:
    db.execute("DROP TRIGGER job_units_input_binding_immutable")
    db.execute(
        "UPDATE job_units SET bound_input_hash=? "
        "WHERE job_id=? AND unit_key='audio:cleanup'",
        ("0" * 64, job.job_id),
    )


def _corrupt_run(db: sqlite3.Connection, _job: JobSeed, run_id: int) -> None:
    db.execute("DROP TRIGGER voice_verification_runs_no_update")
    db.execute(
        "UPDATE voice_verification_runs SET output_hash=? WHERE id=?",
        ("0" * 64, run_id),
    )


def _corrupt_segment(db: sqlite3.Connection, _job: JobSeed, run_id: int) -> None:
    db.execute("DROP TRIGGER voice_verification_segments_no_update")
    db.execute(
        "UPDATE voice_verification_segments SET raw_match_score=0.1234 "
        "WHERE run_id=? AND ordinal=1",
        (run_id,),
    )


def _corrupt_reference(db: sqlite3.Connection, job: JobSeed, _run_id: int) -> None:
    db.execute("DROP TRIGGER voice_reference_features_no_update")
    db.execute(
        "UPDATE voice_reference_features SET embedding_blob=? "
        "WHERE reference_profile_id=?",
        (sqlite3.Binary(b"PRIVATE_FEATURE!"), job.reference.reference_profile_id),
    )


def _corrupt_config(db: sqlite3.Connection, job: JobSeed, _run_id: int) -> None:
    db.execute("DROP TRIGGER speaker_threshold_configs_limited_update")
    db.execute(
        "UPDATE speaker_threshold_configs SET model_version='PRIVATE_CONFIG' "
        "WHERE version=?",
        (job.snapshot.threshold_config_version,),
    )


def _foreign_candidate(db: sqlite3.Connection, _job: JobSeed, run_id: int) -> None:
    db.execute("DROP TRIGGER voice_verification_runs_no_update")
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "UPDATE voice_verification_runs SET candidate_id=9223372036854775000 "
        "WHERE id=?",
        (run_id,),
    )
    db.execute("PRAGMA foreign_keys=ON")


@pytest.mark.parametrize(
    "mutation",
    (
        _corrupt_status,
        _corrupt_unit,
        _corrupt_run,
        _corrupt_segment,
        _corrupt_reference,
        _corrupt_config,
        _foreign_candidate,
    ),
)
def test_corrupt_job_artifact_identity_is_never_shown_or_reviewed(
    db,
    tmp_path: Path,
    mutation: Callable[[sqlite3.Connection, JobSeed, int], None],
) -> None:
    job, run_id = successful_run(db, tmp_path)
    mutation(db, job, run_id)
    service = review_service(db)

    with pytest.raises(DomainError) as detail_error:
        service.show_review(run_id)
    with pytest.raises(DomainError) as review_error:
        service.review(command(run_id, ReviewAction.CONFIRM))

    assert detail_error.value.code == "PRESENCE_REVIEW_UNAVAILABLE"
    assert str(detail_error.value) == "presence review detail is unavailable"
    assert review_error.value.code == "PRESENCE_REVIEW_FAILED"
    assert str(review_error.value) == "presence review could not be saved"
    assert_no_private_exception_chain(detail_error.value)
    assert_no_private_exception_chain(review_error.value)
    assert_review_unchanged(db, job)


@pytest.mark.parametrize(
    "mutation",
    (
        "row_status",
        "row_error_state",
        "row_timestamp_order",
        "duplicate_inventory",
        "missing_inventory",
        "renamed_inventory",
        "foreign_root",
        "row_id_order",
        "external_hash",
        "output_hash",
        "file_resurrection",
        "symlink_resurrection",
    ),
)
def test_review_revalidates_exact_cleanup_receipt_before_any_write(
    db,
    tmp_path: Path,
    mutation: str,
) -> None:
    job, run_id = successful_run(db, tmp_path)
    artifact = db.execute(
        "SELECT id, local_path FROM local_artifacts ORDER BY id LIMIT 1"
    ).fetchone()
    assert artifact is not None
    resurrected_path: Path | None = None
    if mutation == "row_status":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        db.execute(
            """
            UPDATE local_artifacts
            SET status='delete_failed', retry_count=retry_count + 1,
                safe_error_code='AUDIO_DELETE_OS_ERROR', deleted_at=NULL
            WHERE id=?
            """,
            (artifact["id"],),
        )
    elif mutation == "row_error_state":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(
            "UPDATE local_artifacts SET safe_error_code='AUDIO_DELETE_OS_ERROR' "
            "WHERE id=?",
            (artifact["id"],),
        )
        db.execute("PRAGMA ignore_check_constraints=OFF")
    elif mutation == "row_timestamp_order":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        db.execute(
            "UPDATE local_artifacts SET deleted_at=? WHERE id=?",
            ("2020-01-01T00:00:00.000000Z", artifact["id"]),
        )
    elif mutation == "duplicate_inventory":
        db.execute(
            """
            INSERT INTO local_artifacts(
                kind, local_path, status, retry_count, safe_error_code,
                created_at, deleted_at
            )
            SELECT kind, local_path, status, retry_count, safe_error_code,
                   created_at, deleted_at
            FROM local_artifacts WHERE id=?
            """,
            (artifact["id"],),
        )
        replace_cleanup_output_with_current_inventory(db, job.job_id)
    elif mutation == "missing_inventory":
        db.execute("DROP TRIGGER local_artifacts_no_delete")
        db.execute("DELETE FROM local_artifacts WHERE id=?", (artifact["id"],))
        replace_cleanup_output_with_current_inventory(db, job.job_id)
    elif mutation == "renamed_inventory":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        renamed = str(Path(artifact["local_path"]).with_name("renamed.wav"))
        db.execute(
            "UPDATE local_artifacts SET local_path=? WHERE id=?",
            (renamed, artifact["id"]),
        )
    elif mutation == "foreign_root":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        rows = tuple(
            db.execute("SELECT id, local_path FROM local_artifacts ORDER BY id")
        )
        for row in rows:
            source = Path(row["local_path"])
            foreign = (
                tmp_path
                / "foreign-presence-root"
                / source.parent.name
                / source.name
            )
            db.execute(
                "UPDATE local_artifacts SET local_path=? WHERE id=?",
                (str(foreign), row["id"]),
            )
    elif mutation == "row_id_order":
        db.execute("DROP TRIGGER local_artifacts_limited_update")
        db.execute(
            "UPDATE local_artifacts SET id=id + 1000 WHERE id=?",
            (artifact["id"],),
        )
        replace_cleanup_output_with_current_inventory(db, job.job_id)
    elif mutation == "external_hash":
        replace_cleanup_external_hash(db, job.job_id)
    elif mutation == "output_hash":
        db.execute("DROP TRIGGER job_unit_attempts_no_update")
        db.execute(
            "UPDATE job_units SET output_hash=? "
            "WHERE job_id=? AND unit_key='audio:cleanup'",
            ("f" * 64, job.job_id),
        )
        db.execute(
            "UPDATE job_unit_attempts SET output_hash=? "
            "WHERE job_id=? AND unit_key='audio:cleanup'",
            ("f" * 64, job.job_id),
        )
    elif mutation == "file_resurrection":
        resurrected_path = Path(artifact["local_path"])
        resurrected_path.write_bytes(b"synthetic-resurrected-media")
    else:
        resurrected_path = Path(artifact["local_path"])
        target = tmp_path / "missing-synthetic-symlink-target"
        try:
            resurrected_path.symlink_to(target)
        except OSError as cause:
            pytest.skip(f"symlink creation unavailable: {type(cause).__name__}")
    before_review = tuple(db.iterdump())
    statements: list[str] = []
    db.set_trace_callback(statements.append)

    try:
        with pytest.raises(DomainError) as caught:
            review_service(db).review(
                command(run_id, ReviewAction.CONFIRM)
            )
    finally:
        db.set_trace_callback(None)
        if resurrected_path is not None:
            resurrected_path.unlink(missing_ok=True)

    assert caught.value.code == "PRESENCE_REVIEW_FAILED"
    assert str(caught.value) == "presence review could not be saved"
    assert_no_private_exception_chain(caught.value)
    assert "BEGIN IMMEDIATE" in statements
    assert tuple(db.iterdump()) == before_review
    assert_review_unchanged(db, job)


def test_injected_pointer_failure_rolls_back_and_hides_storage_detail(
    db,
    tmp_path: Path,
) -> None:
    job, run_id = successful_run(db, tmp_path)
    db.execute(
        """
        CREATE TRIGGER inject_private_review_failure
        BEFORE UPDATE OF current_presence_decision_id ON subject_video_candidates
        BEGIN
            SELECT RAISE(ABORT, 'PRIVATE_DB_SENTINEL C:/private/ledger.sqlite3');
        END
        """
    )

    with pytest.raises(DomainError) as caught:
        review_service(db).review(command(run_id, ReviewAction.CONFIRM))

    assert caught.value.code == "PRESENCE_REVIEW_FAILED"
    assert str(caught.value) == "presence review could not be saved"
    assert_no_private_exception_chain(caught.value)
    assert_review_unchanged(db, job)


def test_review_detail_omits_provider_body_paths_hashes_features_and_output(
    db,
    tmp_path: Path,
) -> None:
    _job, run_id = successful_run(db, tmp_path)
    db.execute("DROP TRIGGER video_metadata_snapshots_no_update")
    db.execute(
        """
        UPDATE video_metadata_snapshots
        SET title='PRIVATE_COMMAND_OUTPUT_SENTINEL',
            description='PRIVATE_PROVIDER_BODY_SENTINEL'
        """
    )

    detail = review_service(db).show_review(run_id)
    rendered = repr(detail)

    assert "PRIVATE_COMMAND_OUTPUT_SENTINEL" not in rendered
    assert "PRIVATE_PROVIDER_BODY_SENTINEL" not in rendered
    assert "local_path" not in rendered
    assert FEATURE_HASH not in rendered
    assert repr(FEATURE_BYTES) not in rendered
    assert "evidence_hash" not in rendered
    assert "output_hash" not in rendered


@pytest.mark.parametrize("identity", ("person", "video"))
def test_unsafe_public_identity_is_rejected_without_exception_leakage(
    db,
    tmp_path: Path,
    identity: str,
) -> None:
    _job, run_id = successful_run(db, tmp_path)
    if identity == "person":
        db.execute(
            "UPDATE analysis_subjects SET canonical_name=? WHERE id=1",
            ("PRIVATE_PERSON C:/private/name",),
        )
    else:
        db.execute("DROP TRIGGER videos_limited_update")
        db.execute(
            "UPDATE videos SET youtube_video_id=?",
            ("PRIVATE_VIDEO C:/private/id",),
        )

    with pytest.raises(DomainError) as caught:
        review_service(db).show_review(run_id)

    assert caught.value.code == "PRESENCE_REVIEW_UNAVAILABLE"
    assert_no_private_exception_chain(caught.value)


def test_unknown_detail_exception_is_fixed_and_has_no_private_chain(
    db,
    tmp_path: Path,
) -> None:
    _job, run_id = successful_run(db, tmp_path)
    service = review_service(db)

    def fail_artifacts(_job_id: int):
        raise RuntimeError(
            "PRIVATE_NATIVE_SENTINEL C:/private/audio.wav provider body"
        )

    service._voice.require_job_artifacts = fail_artifacts

    with pytest.raises(DomainError) as caught:
        service.show_review(run_id)

    assert caught.value.code == "PRESENCE_REVIEW_UNAVAILABLE"
    assert str(caught.value) == "presence review detail is unavailable"
    assert_no_private_exception_chain(caught.value)
