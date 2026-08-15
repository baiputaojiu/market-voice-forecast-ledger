import sqlite3
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    DiscoveryMethod,
    JobKind,
    JobStage,
    JobStatus,
    PolicyKind,
    SubjectKind,
    UnitStatus,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.jobs import (
    ANALYSIS_INPUT_UNIT_KEY,
    FINAL_PROMOTION_UNIT_KEY,
    JobManifest,
    ManifestUnit,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.job_state import JobStateService


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _add_video(db, label: str) -> int:
    return SourceRepository(db).upsert_video(
        VideoInput(
            youtube_video_id=f"synthetic-{label}",
            youtube_channel_id="UC1000000000000000000000",
            channel_display_name="Synthetic Channel",
            title=f"Synthetic {label}",
            published_at=FIXED_UTC,
            duration_seconds=600,
            live_kind="upload",
        )
    )


def _add_eligibility(db, video_id: int, label: str) -> int:
    sources = SourceRepository(db)
    subject_id = sources.create_subject(
        f"Synthetic Subject {label}", SubjectKind.PERSON
    )
    sources.create_policy(
        subject_id,
        ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        ),
    )
    decision = ChannelPolicyService(db).evaluate(
        subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    assert decision.status.value == "eligible"
    row = db.execute(
        "SELECT id FROM subject_video_eligibility "
        "WHERE subject_id=? AND video_id=?",
        (subject_id, video_id),
    ).fetchone()
    assert row is not None
    return row["id"]


def _video_manifest() -> JobManifest:
    return JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "audio:acquire",
                JobStage.AUDIO_ACQUISITION,
                1,
                "synthetic-video-input",
                (),
                "synthetic-audio-contract-v1",
            ),
            ManifestUnit(
                "transcription:chunk:1",
                JobStage.TRANSCRIPTION,
                2,
                None,
                ("audio:acquire",),
                "synthetic-transcription-contract-v1",
            ),
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
                "synthetic-analysis-input",
                (),
                "synthetic-input-contract-v1",
            ),
            ManifestUnit(
                "codex:batch:1",
                JobStage.CODEX_ANALYSIS,
                2,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                "synthetic-codex-contract-v1",
            ),
            ManifestUnit(
                FINAL_PROMOTION_UNIT_KEY,
                JobStage.HEATMAP_UPDATE,
                3,
                None,
                ("codex:batch:1",),
                "synthetic-promotion-contract-v1",
            ),
        ),
    )


def _set_blocked(db, eligibility_id: int) -> None:
    db.execute(
        "UPDATE subject_video_eligibility "
        "SET status='channel_out_of_scope', "
        "decision_reason='FIXED_CHANNEL_MISMATCH' WHERE id=?",
        (eligibility_id,),
    )


def _binding_ids(db, job_id: int) -> tuple[int, ...]:
    return tuple(
        row["eligibility_id"]
        for row in db.execute(
            "SELECT eligibility_id FROM video_pipeline_job_bindings "
            "WHERE job_id=? ORDER BY eligibility_id",
            (job_id,),
        )
    )


def test_binding_schema_allows_same_video_subjects_and_rejects_unsafe_rows(db):
    video_id = _add_video(db, "shared")
    other_video_id = _add_video(db, "other")
    first = _add_eligibility(db, video_id, "first")
    second = _add_eligibility(db, video_id, "second")
    other = _add_eligibility(db, other_video_id, "other")
    jobs = JobStateService(db)
    video_job_id = jobs.create(_video_manifest())
    analysis_job_id = jobs.create(_analysis_manifest())

    db.execute(
        "INSERT INTO video_pipeline_job_binding_sets("
        "job_id, expected_binding_count, is_sealed) VALUES (?, 2, 0)",
        (video_job_id,),
    )
    db.execute(
        "INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id) "
        "VALUES (?, ?), (?, ?)",
        (video_job_id, first, video_job_id, second),
    )
    db.execute(
        "UPDATE video_pipeline_job_binding_sets SET is_sealed=1 WHERE job_id=?",
        (video_job_id,),
    )
    assert _binding_ids(db, video_job_id) == (first, second)

    mixed_job_id = jobs.create(_video_manifest())
    db.execute(
        "INSERT INTO video_pipeline_job_binding_sets("
        "job_id, expected_binding_count, is_sealed) VALUES (?, 2, 0)",
        (mixed_job_id,),
    )
    db.execute(
        "INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id) "
        "VALUES (?, ?)",
        (mixed_job_id, first),
    )
    with pytest.raises(sqlite3.IntegrityError, match="BINDING_VIDEO_MISMATCH"):
        db.execute(
            "INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id) "
            "VALUES (?, ?)",
            (mixed_job_id, other),
        )
    with pytest.raises(sqlite3.IntegrityError, match="VIDEO_PIPELINE_JOB_REQUIRED"):
        db.execute(
            "INSERT INTO video_pipeline_job_binding_sets("
            "job_id, expected_binding_count, is_sealed) VALUES (?, 1, 0)",
            (analysis_job_id,),
        )
    direct_sealed_job_id = jobs.create(_video_manifest())
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "INSERT INTO video_pipeline_job_binding_sets("
            "job_id, expected_binding_count, is_sealed) VALUES (?, 1, 1)",
            (direct_sealed_job_id,),
        )
    missing_job_id = jobs.create(_video_manifest())
    db.execute(
        "INSERT INTO video_pipeline_job_binding_sets("
        "job_id, expected_binding_count, is_sealed) VALUES (?, 1, 0)",
        (missing_job_id,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute(
            "INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id) "
            "VALUES (?, ?)",
            (missing_job_id, 999_999),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "UPDATE video_pipeline_job_binding_sets SET is_sealed=1 "
            "WHERE job_id=?",
            (mixed_job_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "UPDATE video_pipeline_job_bindings SET eligibility_id=? "
            "WHERE job_id=? AND eligibility_id=?",
            (other, video_job_id, first),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "DELETE FROM video_pipeline_job_bindings "
            "WHERE job_id=? AND eligibility_id=?",
            (video_job_id, first),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "INSERT OR REPLACE INTO video_pipeline_job_bindings("
            "job_id, eligibility_id) VALUES (?, ?)",
            (video_job_id, first),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "INSERT OR REPLACE INTO video_pipeline_job_binding_sets("
            "job_id, expected_binding_count, is_sealed) VALUES (?, 2, 0)",
            (video_job_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "UPDATE subject_video_eligibility SET video_id=? WHERE id=?",
            (other_video_id, first),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "DELETE FROM subject_video_eligibility WHERE id=?",
            (first,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "INSERT OR REPLACE INTO subject_video_eligibility("
            "id, subject_id, video_id, discovery_method, status, policy_id, "
            "policy_hash, decision_reason, decided_at) "
            "SELECT id, subject_id, ?, discovery_method, status, policy_id, "
            "policy_hash, decision_reason, decided_at "
            "FROM subject_video_eligibility WHERE id=?",
            (other_video_id, first),
        )


@pytest.mark.parametrize(
    "job_state", ["queued", "running", "succeeded", "successor", "unbound"]
)
def test_sealed_binding_set_rejects_late_members_for_every_job_state(
    db, job_state
):
    video_id = _add_video(db, f"sealed-{job_state}")
    original = _add_eligibility(db, video_id, f"sealed-{job_state}-original")
    late = _add_eligibility(db, video_id, f"sealed-{job_state}-late")
    service = JobStateService(db)

    if job_state == "unbound":
        job_id = service.create(_video_manifest())
    else:
        job_id = service.create_video_pipeline(_video_manifest(), (original,))
        if job_state == "running":
            service.begin_unit(job_id, "audio:acquire")
        elif job_state == "succeeded":
            service.begin_unit(job_id, "audio:acquire")
            service.complete_unit(job_id, "audio:acquire", "sealed-audio")
            service.begin_unit(job_id, "transcription:chunk:1")
            service.complete_unit(
                job_id, "transcription:chunk:1", "sealed-transcript"
            )
            with transaction(db):
                service.succeed_job_in_transaction(job_id)
        elif job_state == "successor":
            service.request_stop(job_id)
            job_id, _ = service.create_successor(
                job_id, _video_manifest(), {}, {}
            )

    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"):
        db.execute(
            "INSERT INTO video_pipeline_job_bindings(job_id, eligibility_id) "
            "VALUES (?, ?)",
            (job_id, late),
        )

    if job_state != "unbound":
        binding_set = db.execute(
            "SELECT expected_binding_count, is_sealed "
            "FROM video_pipeline_job_binding_sets WHERE job_id=?",
            (job_id,),
        ).fetchone()
        assert tuple(binding_set) == (1, 1)
        with pytest.raises(
            sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"
        ):
            db.execute(
                "UPDATE video_pipeline_job_binding_sets SET is_sealed=0 "
                "WHERE job_id=?",
                (job_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="IMMUTABLE_JOB_BINDING"
        ):
            db.execute(
                "DELETE FROM video_pipeline_job_binding_sets WHERE job_id=?",
                (job_id,),
            )


def test_bound_job_creation_is_atomic_and_requires_one_video(db):
    video_id = _add_video(db, "atomic-shared")
    other_video_id = _add_video(db, "atomic-other")
    first = _add_eligibility(db, video_id, "atomic-first")
    second = _add_eligibility(db, video_id, "atomic-second")
    other = _add_eligibility(db, other_video_id, "atomic-other")
    service = JobStateService(db)

    job_id = service.create_video_pipeline(_video_manifest(), (first, second))

    assert _binding_ids(db, job_id) == (first, second)
    counts_before = tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "jobs",
            "job_units",
            "job_events",
            "video_pipeline_job_binding_sets",
            "video_pipeline_job_bindings",
        )
    )
    with pytest.raises(sqlite3.IntegrityError, match="BINDING_VIDEO_MISMATCH"):
        service.create_video_pipeline(_video_manifest(), (first, other))
    assert tuple(
        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "jobs",
            "job_units",
            "job_events",
            "video_pipeline_job_binding_sets",
            "video_pipeline_job_bindings",
        )
    ) == counts_before


def test_incomplete_binding_set_fails_closed_instead_of_looking_unbound(db):
    service = JobStateService(db)
    job_id = service.create(_video_manifest())
    db.execute(
        "INSERT INTO video_pipeline_job_binding_sets("
        "job_id, expected_binding_count, is_sealed) VALUES (?, 1, 0)",
        (job_id,),
    )

    with pytest.raises(DomainError) as error:
        service.begin_unit(job_id, "audio:acquire")

    assert error.value.code == "VIDEO_PIPELINE_BINDINGS_INVALID"
    assert service.status(job_id) is JobStatus.QUEUED
    assert service.unit(job_id, "audio:acquire").status is UnitStatus.PENDING


@pytest.mark.parametrize(
    "bindings_factory",
    [
        lambda eligibility_id: None,
        lambda eligibility_id: "not-a-binding-list",
        lambda eligibility_id: iter((eligibility_id,)),
        lambda eligibility_id: (),
        lambda eligibility_id: (eligibility_id, eligibility_id),
        lambda eligibility_id: (True,),
        lambda eligibility_id: ([eligibility_id],),
    ],
)
def test_bound_creation_rejects_malformed_binding_collections_as_domain_errors(
    db, bindings_factory
):
    video_id = _add_video(db, "invalid-binding-input")
    eligibility_id = _add_eligibility(db, video_id, "invalid-binding-input")
    before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    with pytest.raises(DomainError) as error:
        JobStateService(db).create_video_pipeline(
            _video_manifest(), bindings_factory(eligibility_id)
        )

    assert error.value.code == "INVALID_VIDEO_PIPELINE_BINDINGS"
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before


def test_transaction_internal_stop_rolls_back_with_its_caller(db):
    video_id = _add_video(db, "atomic-stop")
    eligibility_id = _add_eligibility(db, video_id, "atomic-stop")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(_video_manifest(), (eligibility_id,))

    with pytest.raises(DomainError) as outside_error:
        service.request_stop_in_transaction(job_id)
    assert outside_error.value.code == "JOB_TRANSACTION_REQUIRED"

    with pytest.raises(RuntimeError, match="synthetic outer failure"):
        with transaction(db):
            assert (
                service.request_stop_in_transaction(job_id)
                is JobStatus.STOPPED
            )
            raise RuntimeError("synthetic outer failure")

    assert service.status(job_id) is JobStatus.QUEUED
    assert service.request_stop(job_id) is JobStatus.STOPPED


def test_shared_job_remains_runnable_while_one_binding_is_eligible(db):
    video_id = _add_video(db, "shared-runnable")
    blocked = _add_eligibility(db, video_id, "shared-blocked")
    eligible = _add_eligibility(db, video_id, "shared-eligible")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(
        _video_manifest(), (blocked, eligible)
    )
    _set_blocked(db, blocked)

    unit = service.begin_unit(job_id, "audio:acquire")

    assert unit.status is UnitStatus.RUNNING
    assert service.status(job_id) is JobStatus.RUNNING


def test_bound_job_cannot_begin_when_every_binding_is_blocked(db):
    video_id = _add_video(db, "blocked-begin")
    eligibility_id = _add_eligibility(db, video_id, "blocked-begin")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(_video_manifest(), (eligibility_id,))
    _set_blocked(db, eligibility_id)

    with pytest.raises(DomainError) as error:
        service.begin_unit(job_id, "audio:acquire")

    assert error.value.code == "VIDEO_PIPELINE_INELIGIBLE"
    assert service.status(job_id) is JobStatus.QUEUED
    assert service.unit(job_id, "audio:acquire").status is UnitStatus.PENDING


def test_blocked_paused_job_cannot_resume(db):
    video_id = _add_video(db, "blocked-resume")
    eligibility_id = _add_eligibility(db, video_id, "blocked-resume")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(_video_manifest(), (eligibility_id,))
    service.begin_unit(job_id, "audio:acquire")
    service.request_pause(job_id)
    service.complete_unit(job_id, "audio:acquire", "synthetic-audio-output")
    assert service.status(job_id) is JobStatus.PAUSED
    _set_blocked(db, eligibility_id)

    with pytest.raises(DomainError) as error:
        service.resume(job_id, {"audio:acquire": "synthetic-audio-output"})

    assert error.value.code == "VIDEO_PIPELINE_INELIGIBLE"
    assert service.status(job_id) is JobStatus.PAUSED


def test_blocked_running_job_cannot_recover_as_runnable(db):
    video_id = _add_video(db, "blocked-recover")
    eligibility_id = _add_eligibility(db, video_id, "blocked-recover")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(_video_manifest(), (eligibility_id,))
    service.begin_unit(job_id, "audio:acquire")
    _set_blocked(db, eligibility_id)

    with pytest.raises(DomainError) as error:
        service.recover_interrupted(job_id, {})

    assert error.value.code == "VIDEO_PIPELINE_INELIGIBLE"
    assert service.status(job_id) is JobStatus.RUNNING
    assert service.unit(job_id, "audio:acquire").status is UnitStatus.RUNNING


def test_video_successor_inherits_bindings_and_is_rejected_when_blocked(db):
    video_id = _add_video(db, "successor")
    first = _add_eligibility(db, video_id, "successor-first")
    second = _add_eligibility(db, video_id, "successor-second")
    service = JobStateService(db)
    source_job_id = service.create_video_pipeline(
        _video_manifest(), (first, second)
    )
    service.request_stop(source_job_id)

    successor_id, _ = service.create_successor(
        source_job_id, _video_manifest(), {}, {}
    )

    assert _binding_ids(db, successor_id) == (first, second)
    service.request_stop(successor_id)
    _set_blocked(db, first)
    _set_blocked(db, second)
    job_count = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with pytest.raises(DomainError) as error:
        service.create_successor(successor_id, _video_manifest(), {}, {})
    assert error.value.code == "VIDEO_PIPELINE_INELIGIBLE"
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == job_count


def test_running_stop_preserves_current_success_and_never_starts_next_unit(db):
    video_id = _add_video(db, "running-stop")
    eligibility_id = _add_eligibility(db, video_id, "running-stop")
    service = JobStateService(db)
    job_id = service.create_video_pipeline(_video_manifest(), (eligibility_id,))
    service.begin_unit(job_id, "audio:acquire")

    with transaction(db):
        _set_blocked(db, eligibility_id)
        assert (
            service.request_stop_in_transaction(job_id)
            is JobStatus.CANCEL_REQUESTED
        )

    service.complete_unit(job_id, "audio:acquire", "synthetic-audio-output")

    assert service.status(job_id) is JobStatus.STOPPED
    audio = service.unit(job_id, "audio:acquire")
    transcription = service.unit(job_id, "transcription:chunk:1")
    assert (audio.status, audio.output_hash) == (
        UnitStatus.SUCCESS,
        "synthetic-audio-output",
    )
    assert transcription.status is UnitStatus.PENDING
    with pytest.raises(DomainError) as error:
        service.begin_unit(job_id, "transcription:chunk:1")
    assert error.value.code == "VIDEO_PIPELINE_INELIGIBLE"


def test_blocked_cancel_requested_crash_recovers_only_to_stopped(tmp_path):
    db_path = tmp_path / "blocked-cancel-recovery.sqlite3"
    first = open_database(db_path)
    apply_migrations(first)
    video_id = _add_video(first, "blocked-cancel-recovery")
    eligibility_id = _add_eligibility(
        first, video_id, "blocked-cancel-recovery"
    )
    service = JobStateService(first)
    job_id = service.create_video_pipeline(
        _video_manifest(), (eligibility_id,)
    )
    service.begin_unit(job_id, "audio:acquire")
    with transaction(first):
        _set_blocked(first, eligibility_id)
        assert (
            service.request_stop_in_transaction(job_id)
            is JobStatus.CANCEL_REQUESTED
        )
    first.close()

    reopened = open_database(db_path)
    try:
        recovered = JobStateService(reopened)
        with pytest.raises(DomainError) as error:
            recovered.recover_interrupted(job_id, {})

        assert error.value.code == "STOPPED_JOB_REQUIRES_SUCCESSOR"
        assert recovered.status(job_id) is JobStatus.STOPPED
        assert recovered.unit(job_id, "audio:acquire").status is UnitStatus.PENDING
        assert reopened.execute(
            "SELECT result_status FROM job_unit_attempts "
            "WHERE job_id=? AND unit_key='audio:acquire'",
            (job_id,),
        ).fetchone()["result_status"] == "interrupted"
        with pytest.raises(DomainError):
            recovered.begin_unit(job_id, "audio:acquire")
    finally:
        reopened.close()
