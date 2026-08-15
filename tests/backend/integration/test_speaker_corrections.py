import json
import re
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    ConfigurationStatus,
    DiscoveryMethod,
    PolicyKind,
    ScopeStatus,
    SubjectKind,
)
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.sources import ChannelPolicy
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
from tests.backend.e2e.synthetic_fixture import (
    create_speaker_correction_fixture,
)
from tests.backend.integration.test_analysis_input_boundaries import (
    _begin,
    _prepare_personal_analysis,
)


FIXED_UTC = datetime(2026, 8, 15, 1, 2, 3, 456789, tzinfo=timezone.utc)
CORRECTION_UTC = datetime(2026, 8, 15, 5, 6, 7, 123456, tzinfo=timezone.utc)
PRIVATE_TRANSCRIPT = "Private synthetic transcript must never enter audit."


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


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


def _insert_frozen_same_video_scope(
    db, *, subject_id, cutoff_day, run_segment, segment_id
):
    scope_id = db.execute(
        """
        INSERT INTO analysis_scopes(
            subject_id, cutoff_day_jst, cutoff_exclusive_utc,
            status, stale_reason
        ) VALUES (?, ?, '2026-08-14T15:00:00.000000Z', 'current', NULL)
        """,
        (subject_id, cutoff_day),
    ).lastrowid
    run_id = db.execute(
        """
        INSERT INTO analysis_runs(
            scope_id, model, reasoning_effort, prompt_version, schema_version,
            information_boundary_version, input_hash, input_contract_hash,
            started_at
        ) VALUES (?, 'gpt-5.6-sol', 'max', 'm2-core-prompt-contract-v1',
                  'm2-analysis-output-v1', 'stored-statements-only-v1',
                  'same-video-input', 'same-video-contract', ?)
        """,
        (scope_id, run_segment["assignment_updated_at"]),
    ).lastrowid
    db.execute(
        """
        INSERT INTO analysis_run_segments(
            run_id, segment_id, ordinal, video_id, published_at, policy_id,
            policy_hash, assignment_kind, assigned_subject_id,
            assignment_updated_at, assignment_evidence_hash
        ) VALUES (?, ?, 1, ?, ?, ?, ?, 'subject', ?, ?,
                  'historical-same-video-assignment')
        """,
        (
            run_id,
            segment_id,
            run_segment["video_id"],
            run_segment["published_at"],
            run_segment["policy_id"],
            run_segment["policy_hash"],
            subject_id,
            run_segment["assignment_updated_at"],
        ),
    )
    return scope_id


def test_speaker_subject_to_hold_is_audited_stale_and_non_destructive(db):
    fixture = create_speaker_correction_fixture(db)
    private_chunk_hash = db.execute(
        "SELECT chunk.input_hash FROM transcription_chunks AS chunk "
        "JOIN transcript_segments AS segment ON segment.chunk_id=chunk.id "
        "WHERE segment.id=?",
        (fixture.segment_id,),
    ).fetchone()[0]
    assert "private-input-hash-path" in private_chunk_hash
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


def test_private_transcript_reason_rolls_back_speaker_assignment_stale_and_audit(db):
    fixture = create_speaker_correction_fixture(db)
    before_assignment = _assignment_row(db, fixture.segment_id)
    before_scope = AnalysisRepository(db).get_scope(fixture.scope_id)
    audit_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    with pytest.raises(DomainError) as error:
        SpeakerCorrectionService(db).correct(
            SpeakerCorrection(
                fixture.segment_id,
                AssignmentKind.HOLD,
                None,
                "user",
                PRIVATE_TRANSCRIPT,
            )
        )

    assert error.value.code == "AUDIT_REASON_PRIVATE"
    assert _assignment_row(db, fixture.segment_id) == before_assignment
    assert AnalysisRepository(db).get_scope(fixture.scope_id) == before_scope
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_before


@pytest.mark.parametrize(
    "initial_kind", [AssignmentKind.INTERVIEWER, AssignmentKind.HOLD]
)
def test_interviewer_or_hold_can_be_corrected_to_valid_subject(db, initial_kind):
    fixture = create_speaker_correction_fixture(db, initial_kind)
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_run_segments AS run_segment "
        "JOIN analysis_runs AS run ON run.id=run_segment.run_id "
        "WHERE run.scope_id=?",
        (fixture.scope_id,),
    ).fetchone()[0] == 0

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
    scope = AnalysisRepository(db).get_scope(fixture.scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "SPEAKER_ASSIGNMENT_CHANGED",
    )
    assert scope.generation == 2


def test_interviewer_context_correction_stales_scope_using_subject_from_same_video(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    interviewer = db.execute(
        """
        SELECT assignment.segment_id
        FROM speaker_assignments AS assignment
        JOIN transcript_segments AS segment ON segment.id=assignment.segment_id
        JOIN analysis_run_segments AS run_segment
            ON run_segment.video_id=segment.video_id
        WHERE run_segment.run_id=?
            AND assignment.assignment_kind='interviewer'
        LIMIT 1
        """,
        (run.id,),
    ).fetchone()
    assert interviewer is not None
    assert db.execute(
        "SELECT 1 FROM analysis_run_segments WHERE run_id=? AND segment_id=?",
        (run.id, interviewer["segment_id"]),
    ).fetchone() is None

    SpeakerCorrectionService(db).correct(
        SpeakerCorrection(
            interviewer["segment_id"],
            AssignmentKind.HOLD,
            None,
            "user",
            "interviewer context changed for the source video",
        )
    )

    scope = AnalysisRepository(db).get_scope(run.scope_id)
    assert (scope.status, scope.stale_reason) == (
        ScopeStatus.STALE,
        "SPEAKER_ASSIGNMENT_CHANGED",
    )
    assert scope.generation == 2


def test_subject_reassignment_stales_old_same_video_and_new_subject_scopes_once(db):
    prepared = _prepare_personal_analysis(db)
    run = _begin(db, prepared)
    corrected_segment_id = prepared.expected_segment_ids[0]
    run_segment = db.execute(
        "SELECT * FROM analysis_run_segments WHERE run_id=? AND segment_id=?",
        (run.id, corrected_segment_id),
    ).fetchone()
    assert run_segment is not None
    same_video_segment_id = db.execute(
        """
        SELECT segment.id
        FROM transcript_segments AS segment
        WHERE segment.video_id=? AND segment.id!=?
        ORDER BY segment.id DESC
        LIMIT 1
        """,
        (run_segment["video_id"], corrected_segment_id),
    ).fetchone()[0]
    same_video_scope_id = _insert_frozen_same_video_scope(
        db,
        subject_id=prepared.subject_id,
        cutoff_day="2026-08-13",
        run_segment=run_segment,
        segment_id=same_video_segment_id,
    )

    sources = SourceRepository(db)
    new_subject_id = sources.create_subject(
        "Synthetic reassignment target", SubjectKind.PERSON
    )
    sources.create_policy(
        new_subject_id,
        ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        ),
    )
    ChannelPolicyService(db).evaluate(
        new_subject_id,
        run_segment["video_id"],
        DiscoveryMethod.MANUAL_URL,
    )
    new_subject_scope_id = db.execute(
        """
        INSERT INTO analysis_scopes(
            subject_id, cutoff_day_jst, cutoff_exclusive_utc,
            status, stale_reason
        ) VALUES (?, '2026-08-14', '2026-08-14T15:00:00.000000Z',
                  'current', NULL)
        """,
        (new_subject_id,),
    ).lastrowid

    SpeakerCorrectionService(db).correct(
        SpeakerCorrection(
            corrected_segment_id,
            AssignmentKind.SUBJECT,
            new_subject_id,
            "user",
            "move the segment to the verified subject",
        )
    )

    scopes = {
        scope_id: AnalysisRepository(db).get_scope(scope_id)
        for scope_id in (run.scope_id, same_video_scope_id, new_subject_scope_id)
    }
    assert {
        scope_id: (scope.status, scope.stale_reason)
        for scope_id, scope in scopes.items()
    } == {
        run.scope_id: (
            ScopeStatus.STALE,
            "SPEAKER_ASSIGNMENT_CHANGED",
        ),
        same_video_scope_id: (
            ScopeStatus.STALE,
            "SPEAKER_ASSIGNMENT_CHANGED",
        ),
        new_subject_scope_id: (
            ScopeStatus.STALE,
            "SPEAKER_ASSIGNMENT_CHANGED",
        ),
    }
    assert {
        scope_id: scope.generation for scope_id, scope in scopes.items()
    } == {
        run.scope_id: 2,
        same_video_scope_id: 1,
        new_subject_scope_id: 1,
    }


def test_speaker_correction_command_is_frozen_and_slotted(db):
    fixture = create_speaker_correction_fixture(db)
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
    fixture = create_speaker_correction_fixture(db)
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
    fixture = create_speaker_correction_fixture(db)
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
    fixture = create_speaker_correction_fixture(db)
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
    fixture = create_speaker_correction_fixture(db)
    before_assignment = _assignment_row(db, fixture.segment_id)
    before_audit_count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    original_stale = AnalysisRepository.mark_scope_ids_stale

    if failure_point == "audit":
        def fail_audit(self, event):
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(AuditRepository, "append", fail_audit)
    else:
        def update_then_fail(self, scope_ids, reason):
            original_stale(self, scope_ids, reason)
            raise RuntimeError("synthetic stale failure")

        monkeypatch.setattr(
            AnalysisRepository,
            "mark_scope_ids_stale",
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
