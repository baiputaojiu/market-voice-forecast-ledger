import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    ConfigurationStatus,
    DiscoveryMethod,
    EligibilityStatus,
    JobKind,
    JobStage,
    JobStatus,
    PolicyKind,
    ScopeStatus,
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
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.audit import AuditRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.corrections import (
    ChannelPolicyChange,
    ChannelPolicyCorrectionService,
)
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.job_state import JobStateService


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)
CORRECTION_UTC = datetime(2026, 8, 15, 5, 6, 7, 123456, tzinfo=timezone.utc)
CHANNEL_A = "UCAAAAAAAAAAAAAAAAAAAAAA"
CHANNEL_B = "UCBBBBBBBBBBBBBBBBBBBBBB"
CHANNEL_C = "UCCCCCCCCCCCCCCCCCCCCCCC"


@dataclass(frozen=True)
class EligibilityFixture:
    id: int
    video_id: int
    discovery_method: DiscoveryMethod


@dataclass(frozen=True)
class ChannelFixture:
    subject_id: int
    policy_id: int
    scope_id: int
    auto_a: EligibilityFixture
    manual_a: EligibilityFixture
    auto_b: EligibilityFixture
    manual_c: EligibilityFixture
    queued_job_id: int
    running_job_id: int
    shared_job_id: int
    succeeded_job_id: int
    unrelated_job_id: int


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


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


def _add_video(db, label: str, channel_id: str | None) -> int:
    return SourceRepository(db).upsert_video(
        VideoInput(
            youtube_video_id=f"synthetic-policy-{label}",
            youtube_channel_id=channel_id,
            channel_display_name=f"Private display metadata {label}",
            title=f"Private video title {label}",
            published_at=FIXED_UTC,
            duration_seconds=600,
            live_kind="upload",
        )
    )


def _eligibility(
    db,
    subject_id: int,
    video_id: int,
    discovery_method: DiscoveryMethod,
) -> EligibilityFixture:
    ChannelPolicyService(db).evaluate(subject_id, video_id, discovery_method)
    row = db.execute(
        "SELECT id FROM subject_video_eligibility "
        "WHERE subject_id=? AND video_id=?",
        (subject_id, video_id),
    ).fetchone()
    assert row is not None
    return EligibilityFixture(row["id"], video_id, discovery_method)


def _create_subject_policy(
    db,
    label: str,
    policy_kind: PolicyKind,
    configuration_status: ConfigurationStatus,
    channel_id: str | None,
):
    sources = SourceRepository(db)
    subject_id = sources.create_subject(
        f"Synthetic Policy Subject {label}", SubjectKind.PERSON
    )
    policy_id = sources.create_policy(
        subject_id,
        ChannelPolicy(
            policy_kind=policy_kind,
            configuration_status=configuration_status,
            youtube_channel_id=channel_id,
            channel_display_name=f"Synthetic policy display {label}",
        ),
    )
    return subject_id, policy_id


def _add_current_scope(db, subject_id: int, policy, video_id: int) -> int:
    speakers = SpeakerRepository(db)
    chunk_id = speakers.add_chunk(
        video_id,
        0,
        0,
        60_000,
        "private-channel-input-hash-path",
        "private-channel-output-hash",
        UnitStatus.SUCCESS,
    )
    segment_id = speakers.add_segment(
        video_id,
        chunk_id,
        0,
        1_000,
        4_000,
        "Private channel transcript body must remain immutable.",
        "private-anonymous-speaker",
        FIXED_UTC,
        FIXED_UTC + timedelta(days=365),
    )
    speakers.save_assignment(
        SpeakerAssignment(
            segment_id,
            AssignmentKind.SUBJECT,
            subject_id,
            AssignmentOrigin.MANUAL,
            None,
            None,
            None,
            None,
            "private-channel-assignment-evidence",
            FIXED_UTC,
        )
    )
    scope_id = db.execute(
        """
        INSERT INTO analysis_scopes(
            subject_id, cutoff_day_jst, cutoff_exclusive_utc, status, stale_reason
        ) VALUES (?, '2026-08-14', '2026-08-14T15:00:00.000000Z', 'current', NULL)
        """,
        (subject_id,),
    ).lastrowid
    run_id = db.execute(
        """
        INSERT INTO analysis_runs(
            scope_id, model, reasoning_effort, prompt_version, schema_version,
            information_boundary_version, input_hash, input_contract_hash,
            started_at
        ) VALUES (?, 'gpt-5.6-sol', 'max', 'm2-core-prompt-contract-v1',
                  'm2-analysis-output-v1', 'stored-statements-only-v1',
                  'policy-input-hash', 'policy-contract-hash', ?)
        """,
        (scope_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    ).lastrowid
    db.execute(
        """
        INSERT INTO analysis_run_segments(
            run_id, segment_id, ordinal, video_id, published_at, policy_id,
            policy_hash, assignment_kind, assigned_subject_id,
            assignment_updated_at, assignment_evidence_hash
        ) VALUES (?, ?, 1, ?, ?, ?, ?, 'subject', ?, ?, ?)
        """,
        (
            run_id,
            segment_id,
            video_id,
            FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            policy.id,
            policy.policy_hash,
            subject_id,
            FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "private-channel-assignment-evidence",
        ),
    )
    db.execute(
        """
        INSERT INTO analysis_input_snapshots(
            run_id, input_text, metadata_json, input_sha256,
            snapshot_created_at, expires_at, text_deleted_at
        ) VALUES (?, 'Private immutable policy input', '{}',
                  'policy-snapshot-hash', ?, NULL, NULL)
        """,
        (run_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    )
    batch_id = db.execute(
        """
        INSERT INTO forecast_projection_batches(
            run_id, trigger_kind, latest_mapping_review_id,
            latest_period_review_id, created_at
        ) VALUES (?, 'initial', NULL, NULL, ?)
        """,
        (run_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    ).lastrowid
    db.execute(
        "INSERT INTO current_result_sets(scope_id, source_run_id, projection_batch_id) "
        "VALUES (?, ?, ?)",
        (scope_id, run_id, batch_id),
    )
    job_id = JobStateService(db, clock=lambda: FIXED_UTC).create(
        JobManifest.build(
            JobKind.ANALYSIS_SCOPE,
            (
                ManifestUnit(
                    ANALYSIS_INPUT_UNIT_KEY,
                    JobStage.ANALYSIS_INPUT_EXTRACTION,
                    1,
                    None,
                    (),
                    "synthetic-analysis-fixture-v1",
                ),
                ManifestUnit(
                    FINAL_PROMOTION_UNIT_KEY,
                    JobStage.HEATMAP_UPDATE,
                    2,
                    None,
                    (ANALYSIS_INPUT_UNIT_KEY,),
                    "synthetic-promotion-fixture-v1",
                ),
            ),
        )
    )
    with transaction(db):
        AnalysisRepository(db).insert_job_attempt(
            run_id, job_id, 1, None, FIXED_UTC
        )
    return scope_id


def _full_fixture(db) -> ChannelFixture:
    subject_id, policy_id = _create_subject_policy(
        db,
        "full",
        PolicyKind.FIXED_CHANNEL,
        ConfigurationStatus.CONFIGURED,
        CHANNEL_A,
    )
    sources = SourceRepository(db)
    policy = sources.get_policy(subject_id)
    video_a_auto = _add_video(db, "a-auto", CHANNEL_A)
    video_a_manual = _add_video(db, "a-manual", CHANNEL_A)
    video_b = _add_video(db, "b-auto", CHANNEL_B)
    video_c = _add_video(db, "c-manual", CHANNEL_C)
    auto_a = _eligibility(
        db, subject_id, video_a_auto, DiscoveryMethod.AUTO_SEARCH
    )
    manual_a = _eligibility(
        db, subject_id, video_a_manual, DiscoveryMethod.MANUAL_URL
    )
    auto_b = _eligibility(db, subject_id, video_b, DiscoveryMethod.AUTO_SEARCH)
    manual_c = _eligibility(
        db, subject_id, video_c, DiscoveryMethod.MANUAL_URL
    )
    scope_id = _add_current_scope(db, subject_id, policy, video_a_auto)

    other_subject, _ = _create_subject_policy(
        db,
        "shared",
        PolicyKind.ALL_CHANNELS,
        ConfigurationStatus.CONFIGURED,
        None,
    )
    shared_other = _eligibility(
        db, other_subject, video_a_auto, DiscoveryMethod.AUTO_SEARCH
    )
    unrelated_video = _add_video(db, "unrelated", CHANNEL_C)
    unrelated_eligibility = _eligibility(
        db, other_subject, unrelated_video, DiscoveryMethod.AUTO_SEARCH
    )

    jobs = JobStateService(db)
    queued_job_id = jobs.create_video_pipeline(_video_manifest(), (auto_a.id,))
    running_job_id = jobs.create_video_pipeline(_video_manifest(), (manual_a.id,))
    jobs.begin_unit(running_job_id, "audio:acquire")
    shared_job_id = jobs.create_video_pipeline(
        _video_manifest(), (auto_a.id, shared_other.id)
    )
    succeeded_job_id = jobs.create_video_pipeline(_video_manifest(), (auto_a.id,))
    jobs.begin_unit(succeeded_job_id, "audio:acquire")
    jobs.complete_unit(succeeded_job_id, "audio:acquire", "successful-audio")
    jobs.begin_unit(succeeded_job_id, "transcription:chunk:1")
    jobs.complete_unit(
        succeeded_job_id, "transcription:chunk:1", "successful-transcript"
    )
    with transaction(db):
        jobs.succeed_job_in_transaction(succeeded_job_id)
    unrelated_job_id = jobs.create_video_pipeline(
        _video_manifest(), (unrelated_eligibility.id,)
    )
    return ChannelFixture(
        subject_id,
        policy_id,
        scope_id,
        auto_a,
        manual_a,
        auto_b,
        manual_c,
        queued_job_id,
        running_job_id,
        shared_job_id,
        succeeded_job_id,
        unrelated_job_id,
    )


def _eligibility_rows(db, subject_id):
    return tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM subject_video_eligibility "
            "WHERE subject_id=? ORDER BY video_id",
            (subject_id,),
        )
    )


def _state_bytes(db):
    return tuple(
        (
            table,
            tuple(
                tuple(row)
                for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ),
        )
        for table in (
            "subject_channel_policies",
            "subject_video_eligibility",
            "analysis_scopes",
            "analysis_runs",
            "analysis_run_segments",
            "analysis_input_snapshots",
            "current_result_sets",
            "current_statements",
            "current_asset_mappings",
            "current_forecasts",
            "jobs",
            "job_units",
            "job_unit_attempts",
            "job_events",
            "audit_events",
        )
    )


def _change_to_b(fixture: ChannelFixture) -> ChannelPolicyChange:
    return ChannelPolicyChange(
        fixture.subject_id,
        PolicyKind.FIXED_CHANNEL,
        ConfigurationStatus.CONFIGURED,
        CHANNEL_B,
        "Synthetic changed channel",
        "user",
        "対象チャンネルを変更",
    )


def test_fixed_channel_change_reevaluates_audits_stales_and_stops_only_safe_jobs(db):
    fixture = _full_fixture(db)
    current_before = CurrentResultService(db).get_scope(fixture.scope_id)
    immutable_before = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT * FROM analysis_run_segments ORDER BY id"
        )
    )
    old_policy = SourceRepository(db).get_policy(fixture.subject_id)

    result = ChannelPolicyCorrectionService(
        db, clock=lambda: CORRECTION_UTC
    ).change(_change_to_b(fixture))

    assert result.id == old_policy.id == fixture.policy_id
    assert result.policy_hash != old_policy.policy_hash
    assert result.youtube_channel_id == CHANNEL_B
    rows = {
        row["video_id"]: row
        for row in db.execute(
            "SELECT * FROM subject_video_eligibility WHERE subject_id=?",
            (fixture.subject_id,),
        )
    }
    assert rows[fixture.auto_a.video_id]["status"] == "channel_out_of_scope"
    assert rows[fixture.manual_a.video_id]["status"] == "channel_out_of_scope"
    assert rows[fixture.auto_b.video_id]["status"] == "eligible"
    assert rows[fixture.manual_c.video_id]["status"] == "channel_out_of_scope"
    assert rows[fixture.manual_a.video_id]["discovery_method"] == "manual_url"
    assert rows[fixture.manual_c.video_id]["discovery_method"] == "manual_url"
    assert {row["policy_id"] for row in rows.values()} == {fixture.policy_id}
    assert {row["policy_hash"] for row in rows.values()} == {result.policy_hash}

    jobs = JobStateService(db)
    assert jobs.status(fixture.queued_job_id) is JobStatus.STOPPED
    assert jobs.status(fixture.running_job_id) is JobStatus.CANCEL_REQUESTED
    assert jobs.status(fixture.shared_job_id) is JobStatus.QUEUED
    assert jobs.status(fixture.succeeded_job_id) is JobStatus.SUCCEEDED
    assert jobs.status(fixture.unrelated_job_id) is JobStatus.QUEUED
    jobs.complete_unit(fixture.running_job_id, "audio:acquire", "safe-boundary-audio")
    assert jobs.status(fixture.running_job_id) is JobStatus.STOPPED
    assert (
        jobs.unit(fixture.running_job_id, "transcription:chunk:1").status
        is UnitStatus.PENDING
    )

    scope = AnalysisRepository(db).get_scope(fixture.scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "CHANNEL_POLICY_CHANGED",
    )
    assert CurrentResultService(db).get_scope(fixture.scope_id) == current_before
    assert tuple(
        tuple(row)
        for row in db.execute("SELECT * FROM analysis_run_segments ORDER BY id")
    ) == immutable_before

    policy_event = AuditRepository(db).list_for_entity(
        "subject_channel_policy", str(fixture.policy_id)
    )[-1]
    assert (policy_event.operation, policy_event.reason_code) == (
        "correct",
        "CHANNEL_POLICY_CORRECTION",
    )
    assert set(policy_event.before) == set(policy_event.after) == {
        "channel_display_name",
        "configuration_status",
        "policy_hash",
        "policy_id",
        "policy_kind",
        "subject_id",
        "youtube_channel_id",
    }
    for item in (fixture.auto_a, fixture.manual_a, fixture.auto_b, fixture.manual_c):
        events = AuditRepository(db).list_for_entity(
            "subject_video_eligibility", f"{fixture.subject_id}:{item.video_id}"
        )
        assert events[-1].operation == "update"
        assert events[-1].after["discovery_method"] == item.discovery_method.value
    serialized = json.dumps(
        {"before": policy_event.before, "after": policy_event.after},
        ensure_ascii=False,
    )
    for forbidden in (
        "Private video title",
        "Private channel transcript",
        "text_body",
        "input_text",
        "audio_path",
        "local_path",
    ):
        assert forbidden not in serialized


def test_policy_stop_converges_every_active_state_and_requires_fresh_successor(db):
    subject_id, _ = _create_subject_policy(
        db,
        "status-matrix",
        PolicyKind.FIXED_CHANNEL,
        ConfigurationStatus.CONFIGURED,
        CHANNEL_A,
    )
    video_id = _add_video(db, "status-matrix", CHANNEL_A)
    eligibility = _eligibility(
        db, subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    jobs = JobStateService(db)

    def create_job() -> int:
        return jobs.create_video_pipeline(_video_manifest(), (eligibility.id,))

    queued = create_job()
    running = create_job()
    jobs.begin_unit(running, "audio:acquire")
    running_idle = create_job()
    jobs.begin_unit(running_idle, "audio:acquire")
    jobs.complete_unit(running_idle, "audio:acquire", "idle-audio")
    paused = create_job()
    jobs.begin_unit(paused, "audio:acquire")
    jobs.request_pause(paused)
    jobs.complete_unit(paused, "audio:acquire", "paused-audio")
    pause_requested = create_job()
    jobs.begin_unit(pause_requested, "audio:acquire")
    jobs.request_pause(pause_requested)
    failed = create_job()
    jobs.begin_unit(failed, "audio:acquire")
    jobs.fail_unit(failed, "audio:acquire", "synthetic_retryable")
    failed_live_manifest = JobManifest.build(
        JobKind.VIDEO_PIPELINE,
        (
            ManifestUnit(
                "audio:parallel:1",
                JobStage.AUDIO_ACQUISITION,
                1,
                "parallel-input-1",
                (),
                "parallel-contract-v1",
            ),
            ManifestUnit(
                "audio:parallel:2",
                JobStage.AUDIO_ACQUISITION,
                2,
                "parallel-input-2",
                (),
                "parallel-contract-v1",
            ),
        ),
    )
    failed_live = jobs.create_video_pipeline(
        failed_live_manifest, (eligibility.id,)
    )
    jobs.begin_unit(failed_live, "audio:parallel:1")
    jobs.begin_unit(failed_live, "audio:parallel:2")
    jobs.fail_unit(failed_live, "audio:parallel:1", "synthetic_retryable")
    retrying = create_job()
    jobs.begin_unit(retrying, "audio:acquire")
    jobs.fail_unit(retrying, "audio:acquire", "synthetic_retryable")
    jobs.resume(retrying, {})
    cancel_requested = create_job()
    jobs.begin_unit(cancel_requested, "audio:acquire")
    jobs.request_stop(cancel_requested)
    stopped = create_job()
    jobs.request_stop(stopped)
    succeeded = create_job()
    jobs.begin_unit(succeeded, "audio:acquire")
    jobs.complete_unit(succeeded, "audio:acquire", "succeeded-audio")
    jobs.begin_unit(succeeded, "transcription:chunk:1")
    jobs.complete_unit(
        succeeded, "transcription:chunk:1", "succeeded-transcript"
    )
    with transaction(db):
        jobs.succeed_job_in_transaction(succeeded)

    ChannelPolicyCorrectionService(db).change(
        ChannelPolicyChange(
            subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_B,
            "Blocked status matrix",
            "system",
            "全job状態の停止収束を確認",
        )
    )

    assert {
        "queued": jobs.status(queued),
        "running": jobs.status(running),
        "running_idle": jobs.status(running_idle),
        "paused": jobs.status(paused),
        "pause_requested": jobs.status(pause_requested),
        "failed": jobs.status(failed),
        "failed_live": jobs.status(failed_live),
        "retrying": jobs.status(retrying),
        "cancel_requested": jobs.status(cancel_requested),
        "stopped": jobs.status(stopped),
        "succeeded": jobs.status(succeeded),
    } == {
        "queued": JobStatus.STOPPED,
        "running": JobStatus.CANCEL_REQUESTED,
        "running_idle": JobStatus.STOPPED,
        "paused": JobStatus.STOPPED,
        "pause_requested": JobStatus.CANCEL_REQUESTED,
        "failed": JobStatus.STOPPED,
        "failed_live": JobStatus.CANCEL_REQUESTED,
        "retrying": JobStatus.STOPPED,
        "cancel_requested": JobStatus.CANCEL_REQUESTED,
        "stopped": JobStatus.STOPPED,
        "succeeded": JobStatus.SUCCEEDED,
    }
    assert (
        jobs.unit(failed_live, "audio:parallel:2").status
        is UnitStatus.RUNNING
    )
    assert tuple(
        row["result_status"]
        for row in db.execute(
            "SELECT result_status FROM job_unit_attempts "
            "WHERE job_id=? ORDER BY unit_key",
            (failed_live,),
        )
    ) == ("failed",)

    for job_id, output_hash in (
        (running, "running-boundary"),
        (pause_requested, "pause-boundary"),
        (cancel_requested, "cancel-boundary"),
    ):
        jobs.complete_unit(job_id, "audio:acquire", output_hash)
        assert jobs.status(job_id) is JobStatus.STOPPED
        assert jobs.unit(job_id, "audio:acquire").output_hash == output_hash

    jobs.complete_unit(
        failed_live, "audio:parallel:2", "failed-live-boundary"
    )
    assert jobs.status(failed_live) is JobStatus.STOPPED

    ChannelPolicyCorrectionService(db).change(
        ChannelPolicyChange(
            subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            "Reeligible status matrix",
            "system",
            "再適合後はfresh successorだけを許可",
        )
    )
    with pytest.raises(DomainError) as resume_error:
        jobs.resume(queued, {})
    assert resume_error.value.code == "STOPPED_JOB_REQUIRES_SUCCESSOR"
    successor, _ = jobs.create_successor(queued, _video_manifest(), {}, {})
    assert jobs.status(successor) is JobStatus.QUEUED


@pytest.mark.parametrize(
    (
        "initial_kind",
        "initial_configuration",
        "initial_channel",
        "next_kind",
        "next_configuration",
        "next_channel",
        "expected",
    ),
    [
        (
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            PolicyKind.ALL_CHANNELS,
            ConfigurationStatus.CONFIGURED,
            None,
            ("eligible", "eligible", "channel_unresolved"),
        ),
        (
            PolicyKind.ALL_CHANNELS,
            ConfigurationStatus.CONFIGURED,
            None,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            ("eligible", "channel_out_of_scope", "channel_unresolved"),
        ),
        (
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURATION_REQUIRED,
            None,
            ("configuration_required",) * 3,
        ),
        (
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURATION_REQUIRED,
            None,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            ("eligible", "channel_out_of_scope", "channel_unresolved"),
        ),
    ],
)
def test_policy_boundary_transitions_reevaluate_every_known_video(
    db,
    initial_kind,
    initial_configuration,
    initial_channel,
    next_kind,
    next_configuration,
    next_channel,
    expected,
):
    subject_id, _ = _create_subject_policy(
        db,
        "transition",
        initial_kind,
        initial_configuration,
        initial_channel,
    )
    videos = (
        _add_video(db, "transition-a", CHANNEL_A),
        _add_video(db, "transition-b", CHANNEL_B),
        _add_video(db, "transition-none", None),
    )
    for index, video_id in enumerate(videos):
        _eligibility(
            db,
            subject_id,
            video_id,
            DiscoveryMethod.MANUAL_URL if index == 1 else DiscoveryMethod.AUTO_SEARCH,
        )

    ChannelPolicyCorrectionService(db).change(
        ChannelPolicyChange(
            subject_id,
            next_kind,
            next_configuration,
            next_channel,
            "Transition display",
            "system",
            "Synthetic transition reason",
        )
    )

    statuses = tuple(
        row["status"]
        for row in db.execute(
            "SELECT status FROM subject_video_eligibility "
            "WHERE subject_id=? ORDER BY video_id",
            (subject_id,),
        )
    )
    assert statuses == expected


def test_display_name_only_change_keeps_hash_eligibility_scope_and_jobs_stable(db):
    fixture = _full_fixture(db)
    policy_before = SourceRepository(db).get_policy(fixture.subject_id)
    eligibility_before = _eligibility_rows(db, fixture.subject_id)
    statuses_before = tuple(
        JobStateService(db).status(job_id)
        for job_id in (
            fixture.queued_job_id,
            fixture.running_job_id,
            fixture.shared_job_id,
            fixture.succeeded_job_id,
            fixture.unrelated_job_id,
        )
    )
    audit_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    result = ChannelPolicyCorrectionService(
        db, clock=lambda: CORRECTION_UTC
    ).change(
        ChannelPolicyChange(
            fixture.subject_id,
            policy_before.policy_kind,
            policy_before.configuration_status,
            policy_before.youtube_channel_id,
            "Display metadata only",
            "user",
            "表示名だけを修正",
        )
    )

    assert result.id == policy_before.id
    assert result.policy_hash == policy_before.policy_hash
    assert result.channel_display_name == "Display metadata only"
    assert result.updated_at == CORRECTION_UTC
    assert _eligibility_rows(db, fixture.subject_id) == eligibility_before
    assert (
        AnalysisRepository(db).get_scope(fixture.scope_id).status
        is ScopeStatus.CURRENT
    )
    assert tuple(
        JobStateService(db).status(job_id)
        for job_id in (
            fixture.queued_job_id,
            fixture.running_job_id,
            fixture.shared_job_id,
            fixture.succeeded_job_id,
            fixture.unrelated_job_id,
        )
    ) == statuses_before
    assert (
        db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        == audit_before + 1
    )


def test_channel_policy_change_command_is_frozen_and_slotted(db):
    fixture = _full_fixture(db)
    command = _change_to_b(fixture)

    assert hasattr(command, "__slots__")
    with pytest.raises(FrozenInstanceError):
        command.reason = "changed"


@pytest.mark.parametrize(
    "change_factory",
    [
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            "BAD",
            "display",
            "user",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            "XXAAAAAAAAAAAAAAAAAAAAAA",
            "display",
            "user",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.ALL_CHANNELS,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            "display",
            "user",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            "fixed_channel",
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            "display",
            "user",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.FIXED_CHANNEL,
            "configured",
            CHANNEL_A,
            "display",
            "user",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            "display",
            "admin",
            "reason",
        ),
        lambda f: ChannelPolicyChange(
            f.subject_id,
            PolicyKind.FIXED_CHANNEL,
            ConfigurationStatus.CONFIGURED,
            CHANNEL_A,
            "display",
            "user",
            "\u00a0\u2007\u202f\u3000",
        ),
    ],
)
def test_channel_policy_change_rejects_invalid_types_id_shape_actor_and_reason(
    db, change_factory
):
    fixture = _full_fixture(db)
    before = _state_bytes(db)

    with pytest.raises(DomainError) as error:
        ChannelPolicyCorrectionService(db).change(change_factory(fixture))

    assert error.value.code == "CHANNEL_POLICY_CHANGE_INVALID"
    assert _state_bytes(db) == before


def test_exact_policy_noop_is_rejected_without_audit_or_timestamp_change(db):
    fixture = _full_fixture(db)
    policy = SourceRepository(db).get_policy(fixture.subject_id)
    before = _state_bytes(db)

    with pytest.raises(DomainError) as error:
        ChannelPolicyCorrectionService(db).change(
            ChannelPolicyChange(
                fixture.subject_id,
                policy.policy_kind,
                policy.configuration_status,
                policy.youtube_channel_id,
                policy.channel_display_name,
                "user",
                "No-op is not a correction",
            )
        )

    assert error.value.code == "CHANNEL_POLICY_NO_CHANGE"
    assert _state_bytes(db) == before


@pytest.mark.parametrize("failure_point", ["audit", "stale"])
def test_channel_failure_rolls_back_policy_eligibility_audit_stale_and_job_stop(
    db, monkeypatch, failure_point
):
    fixture = _full_fixture(db)
    before = _state_bytes(db)
    original_stale = AnalysisRepository.mark_scopes_using_policy_stale

    if failure_point == "audit":
        def fail_audit(self, event):
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(AuditRepository, "append", fail_audit)
    else:
        def update_then_fail(self, policy_id, reason):
            original_stale(self, policy_id, reason)
            raise RuntimeError("synthetic stale failure")

        monkeypatch.setattr(
            AnalysisRepository,
            "mark_scopes_using_policy_stale",
            update_then_fail,
        )

    with pytest.raises(RuntimeError, match=f"synthetic {failure_point} failure"):
        ChannelPolicyCorrectionService(db).change(_change_to_b(fixture))

    assert _state_bytes(db) == before
    assert (
        AnalysisRepository(db).get_scope(fixture.scope_id).status
        is ScopeStatus.CURRENT
    )
