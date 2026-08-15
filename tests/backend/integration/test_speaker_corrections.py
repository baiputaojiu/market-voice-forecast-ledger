import json
import re
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
    JobKind,
    JobStage,
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
from market_voice_forecast_ledger.domain.speakers import (
    ScoreRule,
    SpeakerAssignment,
    SpeakerThresholdConfig,
)
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.audit import AuditRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.corrections import (
    SpeakerCorrection,
    SpeakerCorrectionService,
)
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from market_voice_forecast_ledger.services.job_state import JobStateService


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)
CORRECTION_UTC = datetime(2026, 8, 15, 5, 6, 7, 123456, tzinfo=timezone.utc)
PRIVATE_TRANSCRIPT = "Private synthetic transcript must never enter audit."


@dataclass(frozen=True)
class SpeakerFixture:
    subject_id: int
    wrong_subject_id: int
    inactive_subject_id: int
    segment_id: int
    video_id: int
    scope_id: int


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _speaker_fixture(
    db, initial_kind: AssignmentKind = AssignmentKind.SUBJECT
) -> SpeakerFixture:
    sources = SourceRepository(db)
    subject_id = sources.create_subject(
        "Synthetic Corrected Person", SubjectKind.PERSON
    )
    wrong_subject_id = sources.create_subject(
        "Synthetic Unrelated Person", SubjectKind.PERSON
    )
    inactive_subject_id = sources.create_subject(
        "Synthetic Inactive Person", SubjectKind.PERSON
    )
    db.execute(
        "UPDATE analysis_subjects SET is_active=0 WHERE id=?",
        (inactive_subject_id,),
    )
    sources.create_policy(
        subject_id,
        ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        ),
    )
    policy = sources.get_policy(subject_id)
    video_id = sources.upsert_video(
        VideoInput(
            youtube_video_id="synthetic-speaker-correction",
            youtube_channel_id="UC1000000000000000000000",
            channel_display_name="Synthetic Interview Channel",
            title="Synthetic speaker correction fixture",
            published_at=FIXED_UTC,
            duration_seconds=60,
            live_kind="upload",
        )
    )
    ChannelPolicyService(db).evaluate(
        subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    speakers = SpeakerRepository(db)
    chunk_id = speakers.add_chunk(
        video_id,
        0,
        0,
        60_000,
        "private-input-hash-path-C:/private/audio.wav",
        "private-output-hash",
        UnitStatus.SUCCESS,
    )
    segment_id = speakers.add_segment(
        video_id,
        chunk_id,
        0,
        1_000,
        4_000,
        PRIVATE_TRANSCRIPT,
        "anonymous-speaker-private",
        FIXED_UTC,
        FIXED_UTC + timedelta(days=365),
    )
    config = SpeakerThresholdConfig(
        version="synthetic-threshold-private-v1",
        model_name="synthetic-private-model",
        model_version="private-1.0",
        subject_rule=ScoreRule("gte", 1.5),
        interviewer_rule=ScoreRule("lte", 0.5),
    )
    speakers.add_threshold_config(config, FIXED_UTC, True)
    speakers.save_assignment(
        SpeakerAssignment(
            segment_id=segment_id,
            assignment_kind=initial_kind,
            assigned_subject_id=(
                subject_id if initial_kind is AssignmentKind.SUBJECT else None
            ),
            assignment_origin=AssignmentOrigin.AUTO_VOICE,
            raw_match_score=1.73,
            model_name=config.model_name,
            model_version=config.model_version,
            threshold_config_version=config.version,
            evidence_hash="private-auto-voice-evidence-hash",
            assigned_at=FIXED_UTC,
        )
    )
    scope_id = _add_current_scope(
        db, subject_id, policy, video_id, segment_id, initial_kind
    )
    return SpeakerFixture(
        subject_id,
        wrong_subject_id,
        inactive_subject_id,
        segment_id,
        video_id,
        scope_id,
    )


def _add_current_scope(
    db, subject_id, policy, video_id, segment_id, assignment_kind
) -> int:
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
                  'speaker-input-hash', 'speaker-contract-hash', ?)
        """,
        (scope_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    ).lastrowid
    assignment = db.execute(
        "SELECT * FROM speaker_assignments WHERE segment_id=?", (segment_id,)
    ).fetchone()
    db.execute(
        """
        INSERT INTO analysis_run_segments(
            run_id, segment_id, ordinal, video_id, published_at, policy_id,
            policy_hash, assignment_kind, assigned_subject_id,
            assignment_updated_at, assignment_evidence_hash
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            segment_id,
            video_id,
            FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            policy.id,
            policy.policy_hash,
            assignment_kind.value,
            assignment["assigned_subject_id"],
            assignment["assigned_at"],
            assignment["evidence_hash"],
        ),
    )
    db.execute(
        """
        INSERT INTO analysis_input_snapshots(
            run_id, input_text, metadata_json, input_sha256,
            snapshot_created_at, expires_at, text_deleted_at
        ) VALUES (?, ?, '{}', 'speaker-snapshot-hash', ?, NULL, NULL)
        """,
        (run_id, PRIVATE_TRANSCRIPT, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
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


def _immutable_rows(db):
    return tuple(
        (
            table,
            tuple(
                tuple(row)
                for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ),
        )
        for table in (
            "analysis_runs",
            "analysis_run_segments",
            "analysis_input_snapshots",
            "forecast_projection_batches",
            "current_result_sets",
            "current_statements",
            "current_asset_mappings",
            "current_forecasts",
        )
    )


def _assignment_row(db, segment_id):
    return tuple(
        db.execute(
            "SELECT * FROM speaker_assignments WHERE segment_id=?", (segment_id,)
        ).fetchone()
    )


def test_speaker_subject_to_hold_is_audited_stale_and_non_destructive(db):
    fixture = _speaker_fixture(db)
    immutable_before = _immutable_rows(db)
    current_before = CurrentResultService(db).get_scope(fixture.scope_id)

    result = SpeakerCorrectionService(
        db, clock=lambda: CORRECTION_UTC
    ).correct(
        SpeakerCorrection(
            fixture.segment_id,
            AssignmentKind.HOLD,
            None,
            "user",
            "声が一致しないため保留",
        )
    )

    assert result.assignment_kind is AssignmentKind.HOLD
    assert result.assigned_subject_id is None
    assert result.assignment_origin is AssignmentOrigin.MANUAL
    assert result.raw_match_score is None
    assert result.model_name is None
    assert result.model_version is None
    assert result.threshold_config_version is None
    assert re.fullmatch(r"[0-9a-f]{64}", result.evidence_hash)
    assert result.assigned_at == CORRECTION_UTC
    assert _immutable_rows(db) == immutable_before
    assert CurrentResultService(db).get_scope(fixture.scope_id) == current_before
    scope = AnalysisRepository(db).get_scope(fixture.scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "SPEAKER_ASSIGNMENT_CHANGED",
    )
    event = AuditRepository(db).list_for_entity(
        "speaker_assignment", str(fixture.segment_id)
    )[-1]
    assert (event.operation, event.actor_kind, event.reason_code) == (
        "correct",
        "user",
        "SPEAKER_CORRECTION",
    )
    assert event.reason_text == "声が一致しないため保留"
    safe_keys = {
        "assigned_at",
        "assigned_subject_id",
        "assignment_kind",
        "assignment_origin",
        "evidence_hash",
        "segment_id",
    }
    assert set(event.before) == set(event.after) == safe_keys
    serialized = json.dumps(
        {"before": event.before, "after": event.after}, ensure_ascii=False
    )
    for forbidden in (
        PRIVATE_TRANSCRIPT,
        "text_body",
        "anonymous_speaker_id",
        "private-input-hash-path",
        "raw_match_score",
        "model_name",
        "model_version",
        "threshold_config_version",
        "audio_path",
        "local_path",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "initial_kind", [AssignmentKind.INTERVIEWER, AssignmentKind.HOLD]
)
def test_interviewer_or_hold_can_be_corrected_to_valid_subject(db, initial_kind):
    fixture = _speaker_fixture(db, initial_kind)

    result = SpeakerCorrectionService(db).correct(
        SpeakerCorrection(
            fixture.segment_id,
            AssignmentKind.SUBJECT,
            fixture.subject_id,
            "system",
            "確認済みの対象者へ修正",
        )
    )

    assert result.assignment_kind is AssignmentKind.SUBJECT
    assert result.assigned_subject_id == fixture.subject_id
    assert result.assignment_origin is AssignmentOrigin.MANUAL


def test_speaker_correction_command_is_frozen_and_slotted(db):
    fixture = _speaker_fixture(db)
    command = SpeakerCorrection(
        fixture.segment_id,
        AssignmentKind.HOLD,
        None,
        "user",
        "Synthetic reason",
    )

    assert hasattr(command, "__slots__")
    with pytest.raises(FrozenInstanceError):
        command.reason = "changed"


@pytest.mark.parametrize(
    ("command_factory", "error_code"),
    [
        (
            lambda f: SpeakerCorrection(
                f.segment_id, AssignmentKind.SUBJECT, None, "user", "reason"
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id,
                AssignmentKind.HOLD,
                f.subject_id,
                "user",
                "reason",
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id, "subject", f.subject_id, "user", "reason"
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id, [], f.subject_id, "user", "reason"
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id, AssignmentKind.HOLD, None, "admin", "reason"
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id,
                AssignmentKind.HOLD,
                None,
                "user",
                "\u00a0\u2007\u202f\u3000",
            ),
            "SPEAKER_CORRECTION_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id,
                AssignmentKind.SUBJECT,
                f.inactive_subject_id,
                "user",
                "reason",
            ),
            "SPEAKER_CORRECTION_SUBJECT_INVALID",
        ),
        (
            lambda f: SpeakerCorrection(
                f.segment_id,
                AssignmentKind.SUBJECT,
                f.wrong_subject_id,
                "user",
                "reason",
            ),
            "SPEAKER_CORRECTION_SUBJECT_INVALID",
        ),
    ],
)
def test_speaker_correction_rejects_invalid_shape_actor_reason_and_subject(
    db, command_factory, error_code
):
    fixture = _speaker_fixture(db)
    before = _assignment_row(db, fixture.segment_id)

    with pytest.raises(DomainError) as error:
        SpeakerCorrectionService(db).correct(command_factory(fixture))

    assert error.value.code == error_code
    assert _assignment_row(db, fixture.segment_id) == before
    assert (
        AnalysisRepository(db).get_scope(fixture.scope_id).status
        is ScopeStatus.CURRENT
    )


def test_speaker_correction_distinguishes_missing_segment_and_assignment(db):
    fixture = _speaker_fixture(db)
    speakers = SpeakerRepository(db)
    chunk_id = db.execute(
        "SELECT id FROM transcription_chunks WHERE video_id=?", (fixture.video_id,)
    ).fetchone()["id"]
    unassigned_segment = speakers.add_segment(
        fixture.video_id,
        chunk_id,
        1,
        5_000,
        8_000,
        "Private unassigned body.",
        "anonymous-unassigned",
        FIXED_UTC,
        None,
    )

    with pytest.raises(DomainError) as missing_segment:
        SpeakerCorrectionService(db).correct(
            SpeakerCorrection(
                999_999,
                AssignmentKind.HOLD,
                None,
                "user",
                "reason",
            )
        )
    with pytest.raises(DomainError) as missing_assignment:
        SpeakerCorrectionService(db).correct(
            SpeakerCorrection(
                unassigned_segment,
                AssignmentKind.HOLD,
                None,
                "user",
                "reason",
            )
        )

    assert missing_segment.value.code == "SPEAKER_CORRECTION_SEGMENT_NOT_FOUND"
    assert missing_assignment.value.code == "SPEAKER_ASSIGNMENT_NOT_FOUND"


@pytest.mark.parametrize(
    ("column", "unsafe_value", "ignore_checks"),
    [
        ("evidence_hash", "C:/synthetic-private/audio/evidence.wav", False),
        ("raw_match_score", "not-a-score", False),
        ("model_name", b"not-text", False),
        ("assigned_at", "2026-08-15T01:02:03.456789", False),
        ("assigned_at", "2026-08-15T10:02:03.456789+09:00", False),
        ("assignment_kind", "not-a-kind", True),
        ("assignment_origin", "not-an-origin", True),
    ],
)
def test_speaker_correction_never_reads_body_and_rejects_unsafe_stored_metadata(
    db, column, unsafe_value, ignore_checks
):
    fixture = _speaker_fixture(db)
    if ignore_checks:
        db.execute("PRAGMA ignore_check_constraints=ON")
    try:
        db.execute(
            f"UPDATE speaker_assignments SET {column}=? WHERE segment_id=?",
            (unsafe_value, fixture.segment_id),
        )
    finally:
        if ignore_checks:
            db.execute("PRAGMA ignore_check_constraints=OFF")
    before = _assignment_row(db, fixture.segment_id)
    audit_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    traced_sql: list[str] = []
    db.set_trace_callback(traced_sql.append)
    try:
        with pytest.raises(DomainError) as error:
            SpeakerCorrectionService(db).correct(
                SpeakerCorrection(
                    fixture.segment_id,
                    AssignmentKind.HOLD,
                    None,
                    "user",
                    "unsafe legacy evidence must not enter audit",
                )
            )
    finally:
        db.set_trace_callback(None)

    assert error.value.code == "SPEAKER_ASSIGNMENT_STORED_INVALID"
    assert _assignment_row(db, fixture.segment_id) == before
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_before
    assert (
        AnalysisRepository(db).get_scope(fixture.scope_id).status
        is ScopeStatus.CURRENT
    )
    transcript_reads = tuple(
        statement.lower()
        for statement in traced_sql
        if "from transcript_segments" in statement.lower()
    )
    assert transcript_reads
    assert all("select *" not in statement for statement in transcript_reads)
    assert all("text_body" not in statement for statement in transcript_reads)


@pytest.mark.parametrize("failure_point", ["audit", "stale"])
def test_speaker_failure_rolls_back_assignment_audit_and_stale(
    db, monkeypatch, failure_point
):
    fixture = _speaker_fixture(db)
    before_assignment = _assignment_row(db, fixture.segment_id)
    before_audit_count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    original_stale = AnalysisRepository.mark_scopes_using_segment_stale

    if failure_point == "audit":
        def fail_audit(self, event):
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(AuditRepository, "append", fail_audit)
    else:
        def update_then_fail(self, segment_id, reason):
            original_stale(self, segment_id, reason)
            raise RuntimeError("synthetic stale failure")

        monkeypatch.setattr(
            AnalysisRepository,
            "mark_scopes_using_segment_stale",
            update_then_fail,
        )

    with pytest.raises(RuntimeError, match=f"synthetic {failure_point} failure"):
        SpeakerCorrectionService(db).correct(
            SpeakerCorrection(
                fixture.segment_id,
                AssignmentKind.HOLD,
                None,
                "user",
                "rollback reason",
            )
        )

    assert _assignment_row(db, fixture.segment_id) == before_assignment
    assert (
        db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        == before_audit_count
    )
    assert (
        AnalysisRepository(db).get_scope(fixture.scope_id).status
        is ScopeStatus.CURRENT
    )
