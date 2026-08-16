import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.analysis import (
    AnalysisRunSettings,
    BeginAnalysisRun,
)
from market_voice_forecast_ledger.domain.common import sha256_text
from market_voice_forecast_ledger.domain.enums import (
    AssignmentKind,
    AssignmentOrigin,
    ConfigurationStatus,
    DiscoveryMethod,
    JobKind,
    JobStage,
    PolicyKind,
    SubjectKind,
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
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.analysis_runs import AnalysisRunService
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.corrections import (
    SpeakerCorrection,
    SpeakerCorrectionService,
)
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.services.speaker_assignment import (
    SpeakerAssignmentService,
)


CUTOFF_DAY = date(2026, 8, 14)
CUTOFF_EXCLUSIVE = datetime(2026, 8, 14, 15, 0, tzinfo=timezone.utc)
FIXED_UTC = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class PreparedAnalysis:
    subject_id: int
    cutoff_day: date
    expected_segment_ids: tuple[int, ...]
    settings: AnalysisRunSettings
    input_contract: str
    manifest: JobManifest
    job_id: int
    command: BeginAnalysisRun


def _channel_id(index: int) -> str:
    return f"UC{index:022d}"


def _create_subject(
    db,
    name: str,
    kind: SubjectKind,
    *,
    channel_index: int,
) -> int:
    sources = SourceRepository(db)
    subject_id = sources.create_subject(name, kind)
    policy = (
        ChannelPolicy(
            policy_kind=PolicyKind.FIXED_CHANNEL,
            configuration_status=ConfigurationStatus.CONFIGURED,
            youtube_channel_id=_channel_id(channel_index),
            channel_display_name=f"Synthetic channel {channel_index}",
        )
        if kind is SubjectKind.ORGANIZATION
        else ChannelPolicy(
            policy_kind=PolicyKind.ALL_CHANNELS,
            configuration_status=ConfigurationStatus.CONFIGURED,
        )
    )
    sources.create_policy(subject_id, policy)
    return subject_id


def _add_video_with_segments(
    db,
    *,
    subject_id: int,
    youtube_video_id: str,
    published_at: datetime,
    texts: tuple[str, ...],
    channel_index: int,
    transcript_created_at: datetime = FIXED_UTC,
) -> tuple[int, tuple[int, ...]]:
    sources = SourceRepository(db)
    policy = sources.get_policy(subject_id)
    video_id = sources.upsert_video(
        VideoInput(
            youtube_video_id=youtube_video_id,
            youtube_channel_id=policy.youtube_channel_id or _channel_id(channel_index),
            channel_display_name=f"Synthetic channel {channel_index}",
            title=f"Synthetic source {youtube_video_id}",
            published_at=published_at,
            duration_seconds=max(1, len(texts)) * 10,
            live_kind="upload",
        )
    )
    ChannelPolicyService(db).evaluate(
        subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
    )
    speakers = SpeakerRepository(db)
    chunk_id = speakers.add_chunk(
        video_id=video_id,
        chunk_no=0,
        start_ms=0,
        end_ms=max(1, len(texts)) * 10_000,
        input_hash=f"chunk-input-{youtube_video_id}",
        output_hash=f"chunk-output-{youtube_video_id}",
        status=UnitStatus.SUCCESS,
    )
    segment_ids = tuple(
        speakers.add_segment(
            video_id=video_id,
            chunk_id=chunk_id,
            segment_no=segment_no,
            start_ms=segment_no * 10_000,
            end_ms=(segment_no + 1) * 10_000,
            text_body=text,
            anonymous_speaker_id=f"speaker-{segment_no}",
            transcript_created_at=transcript_created_at,
            expires_at=None,
        )
        for segment_no, text in enumerate(texts)
    )
    return video_id, segment_ids


def _save_assignment(
    db,
    *,
    segment_id: int,
    kind: AssignmentKind,
    subject_id: int | None,
    evidence_hash: str,
) -> None:
    SpeakerRepository(db).save_assignment(
        SpeakerAssignment(
            segment_id=segment_id,
            assignment_kind=kind,
            assigned_subject_id=subject_id,
            assignment_origin=AssignmentOrigin.MANUAL,
            raw_match_score=None,
            model_name=None,
            model_version=None,
            threshold_config_version=None,
            evidence_hash=evidence_hash,
            assigned_at=FIXED_UTC,
        )
    )


def _analysis_manifest(
    input_contract: str,
    settings: AnalysisRunSettings | None = None,
) -> JobManifest:
    settings = settings or AnalysisRunSettings.required()
    return JobManifest.build(
        JobKind.ANALYSIS_SCOPE,
        (
            ManifestUnit(
                ANALYSIS_INPUT_UNIT_KEY,
                JobStage.ANALYSIS_INPUT_EXTRACTION,
                1,
                input_contract,
                (),
                "analysis-input-freeze-v1",
            ),
            ManifestUnit(
                "codex:batch:1",
                JobStage.CODEX_ANALYSIS,
                2,
                None,
                (ANALYSIS_INPUT_UNIT_KEY,),
                settings.codex_execution_contract_hash(),
            ),
            ManifestUnit(
                STATEMENT_NORMALIZATION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                3,
                None,
                ("codex:batch:1",),
                "statement-normalization-v1",
            ),
            ManifestUnit(
                PERIOD_NORMALIZATION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                4,
                None,
                (STATEMENT_NORMALIZATION_UNIT_KEY,),
                "period-normalization-v1",
            ),
            ManifestUnit(
                ASSET_MAPPING_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                5,
                None,
                (
                    STATEMENT_NORMALIZATION_UNIT_KEY,
                    PERIOD_NORMALIZATION_UNIT_KEY,
                ),
                "asset-mapping-v1",
            ),
            ManifestUnit(
                FORECAST_PROJECTION_UNIT_KEY,
                JobStage.ASSET_MAPPING,
                6,
                None,
                (ASSET_MAPPING_UNIT_KEY, PERIOD_NORMALIZATION_UNIT_KEY),
                "forecast-projection-v1",
            ),
            ManifestUnit(
                FINAL_PROMOTION_UNIT_KEY,
                JobStage.HEATMAP_UPDATE,
                7,
                None,
                (FORECAST_PROJECTION_UNIT_KEY,),
                "final-promotion-v1",
            ),
        ),
    )


def _create_job_for_input(
    db,
    subject_id: int,
    cutoff_day: date = CUTOFF_DAY,
    *,
    settings: AnalysisRunSettings | None = None,
    input_contract_override: str | None = None,
    manifest: JobManifest | None = None,
) -> PreparedAnalysis:
    settings = settings or AnalysisRunSettings.required()
    input_contract = AnalysisRunService(db).preview_input_contract(
        subject_id, cutoff_day, settings
    )
    manifest = manifest or _analysis_manifest(
        input_contract_override or input_contract, settings
    )
    job_id = JobStateService(db).create(manifest)
    JobStateService(db).begin_unit(job_id, ANALYSIS_INPUT_UNIT_KEY)
    return PreparedAnalysis(
        subject_id=subject_id,
        cutoff_day=cutoff_day,
        expected_segment_ids=(),
        settings=settings,
        input_contract=input_contract,
        manifest=manifest,
        job_id=job_id,
        command=BeginAnalysisRun(subject_id, cutoff_day, job_id, settings),
    )


def _prepare_personal_analysis(db) -> PreparedAnalysis:
    subject_id = _create_subject(
        db, "Synthetic Person", SubjectKind.PERSON, channel_index=1
    )
    other_subject_id = _create_subject(
        db, "Synthetic Other Person", SubjectKind.PERSON, channel_index=2
    )
    _, before_segments = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-before-cutoff",
        published_at=datetime(
            2026, 8, 14, 14, 59, 59, 999999, tzinfo=timezone.utc
        ),
        texts=(
            "Synthetic subject evidence.",
            "米国株",
            "Synthetic held utterance.",
            "Synthetic other-person utterance.",
        ),
        channel_index=1,
        transcript_created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    _save_assignment(
        db,
        segment_id=before_segments[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=subject_id,
        evidence_hash="personal-subject-evidence-v1",
    )
    _save_assignment(
        db,
        segment_id=before_segments[1],
        kind=AssignmentKind.INTERVIEWER,
        subject_id=None,
        evidence_hash="personal-interviewer-evidence-v1",
    )
    _save_assignment(
        db,
        segment_id=before_segments[2],
        kind=AssignmentKind.HOLD,
        subject_id=None,
        evidence_hash="personal-hold-evidence-v1",
    )
    _save_assignment(
        db,
        segment_id=before_segments[3],
        kind=AssignmentKind.SUBJECT,
        subject_id=other_subject_id,
        evidence_hash="other-person-evidence-v1",
    )

    _, post_segments = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-at-exclusive-cutoff",
        published_at=CUTOFF_EXCLUSIVE,
        texts=("Synthetic post-cutoff evidence.",),
        channel_index=1,
        transcript_created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _save_assignment(
        db,
        segment_id=post_segments[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=subject_id,
        evidence_hash="post-cutoff-evidence-v1",
    )

    prepared = _create_job_for_input(db, subject_id)
    return replace(prepared, expected_segment_ids=(before_segments[0],))


def _begin(db, prepared: PreparedAnalysis):
    return AnalysisRunService(db).begin(prepared.command)


def test_required_settings_are_fail_closed_for_preview_and_begin(db):
    subject_id = _create_subject(
        db, "Synthetic Settings Person", SubjectKind.PERSON, channel_index=3
    )
    required = AnalysisRunSettings.required()
    assert required == AnalysisRunSettings(
        model="gpt-5.6-sol",
        reasoning_effort="max",
        prompt_version="m2-core-prompt-contract-v1",
        schema_version="m2-analysis-output-v1",
        information_boundary_version="stored-statements-only-v1",
    )
    altered = replace(required, reasoning_effort="high")

    with pytest.raises(DomainError) as preview_error:
        AnalysisRunService(db).preview_input_contract(
            subject_id, CUTOFF_DAY, altered
        )
    assert preview_error.value.code == "ANALYSIS_SETTINGS_MISMATCH"

    prepared = _create_job_for_input(db, subject_id)
    with pytest.raises(DomainError) as begin_error:
        AnalysisRunService(db).begin(replace(prepared.command, settings=altered))
    assert begin_error.value.code == "ANALYSIS_SETTINGS_MISMATCH"
    assert db.execute("SELECT COUNT(*) FROM analysis_scopes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_person_scope_excludes_interviewer_hold_other_subject_and_post_cutoff(db):
    prepared = _prepare_personal_analysis(db)

    run = _begin(db, prepared)

    segments = AnalysisRepository(db).get_input_segments(run.id)
    assert tuple(item.segment_id for item in segments) == prepared.expected_segment_ids
    assert JobStateService(db).unit(
        prepared.job_id, ANALYSIS_INPUT_UNIT_KEY
    ).status is UnitStatus.SUCCESS


def test_person_interviewer_market_context_is_frozen_safe_and_not_codex_input(db):
    prepared = _prepare_personal_analysis(db)

    run = _begin(db, prepared)

    snapshot = AnalysisRepository(db).get_snapshot(run.id)
    input_payload = json.loads(snapshot.input_text)
    metadata = json.loads(snapshot.metadata_json)
    interviewer = db.execute(
        """
        SELECT assignment.segment_id, segment.video_id
        FROM speaker_assignments AS assignment
        JOIN transcript_segments AS segment ON segment.id=assignment.segment_id
        WHERE assignment.assignment_kind='interviewer'
        """
    ).fetchone()
    assert metadata["interviewer_market_context"] == [
        {
            "assignment_sha256": sha256_text(
                "personal-interviewer-evidence-v1"
            ),
            "market_codes": ["us"],
            "segment_id": interviewer["segment_id"],
            "text_sha256": sha256_text("米国株"),
            "video_id": interviewer["video_id"],
        }
    ]
    assert tuple(
        row["segment_id"] for row in input_payload["segments"]
    ) == prepared.expected_segment_ids
    assert "米国株" not in snapshot.input_text
    assert "米国株" not in snapshot.metadata_json
    assert tuple(
        row.segment_id for row in AnalysisRepository(db).get_input_segments(run.id)
    ) == prepared.expected_segment_ids


def test_begin_rejects_interviewer_context_drift_after_preview(db):
    prepared = _prepare_personal_analysis(db)
    db.execute(
        """
        UPDATE speaker_assignments
        SET evidence_hash=?
        WHERE assignment_kind='interviewer'
        """,
        ("changed-interviewer-evidence",),
    )

    with pytest.raises(DomainError) as error:
        _begin(db, prepared)

    assert error.value.code == "ANALYSIS_JOB_INPUT_MISMATCH"
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_organization_scope_requires_channel_organization_assignments(db):
    subject_id = _create_subject(
        db, "Synthetic Organization", SubjectKind.ORGANIZATION, channel_index=4
    )
    video_id, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-organization-source",
        published_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        texts=(
            "Synthetic presenter statement.",
            "Synthetic guest statement.",
            "Synthetic interviewer statement.",
        ),
        channel_index=4,
    )
    SpeakerAssignmentService(db).assign_organization_video(subject_id, video_id)

    prepared = _create_job_for_input(db, subject_id)
    run = _begin(db, prepared)

    assert tuple(
        item.segment_id
        for item in AnalysisRepository(db).get_input_segments(run.id)
    ) == segment_ids
    assert json.loads(
        AnalysisRepository(db).get_snapshot(run.id).metadata_json
    )["interviewer_market_context"] == []


@pytest.mark.parametrize(
    ("assignment_kind", "uses_organization_subject"),
    (
        pytest.param(AssignmentKind.HOLD, False, id="hold"),
        pytest.param(AssignmentKind.INTERVIEWER, False, id="interviewer"),
        pytest.param(AssignmentKind.SUBJECT, True, id="manual-subject"),
    ),
)
def test_rejected_organization_correction_preserves_every_official_input_segment(
    db, assignment_kind, uses_organization_subject
):
    subject_id = _create_subject(
        db,
        "Synthetic Organization Correction Boundary",
        SubjectKind.ORGANIZATION,
        channel_index=41,
    )
    video_id, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-organization-correction-boundary",
        published_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        texts=(
            "Synthetic presenter organization statement.",
            "Synthetic guest organization statement.",
            "Synthetic interviewer organization statement.",
        ),
        channel_index=41,
    )
    organization_assignments = SpeakerAssignmentService(
        db,
        clock=lambda: FIXED_UTC,
    )
    assert organization_assignments.assign_organization_video(
        subject_id,
        video_id,
    ) == segment_ids

    with pytest.raises(DomainError) as error:
        SpeakerCorrectionService(db).correct(
            SpeakerCorrection(
                segment_ids[1],
                assignment_kind,
                subject_id if uses_organization_subject else None,
                "user",
                "Synthetic prohibited organization correction",
            )
        )

    assert error.value.code == "ORGANIZATION_SPEAKER_CORRECTION_FORBIDDEN"
    assignments = SpeakerRepository(db).list_assignments(segment_ids)
    assert tuple(row.segment_id for row in assignments) == segment_ids
    assert {row.assignment_kind for row in assignments} == {
        AssignmentKind.SUBJECT
    }
    assert {row.assignment_origin for row in assignments} == {
        AssignmentOrigin.CHANNEL_ORGANIZATION
    }

    run = _begin(db, _create_job_for_input(db, subject_id))
    assert tuple(
        row.segment_id for row in AnalysisRepository(db).get_input_segments(run.id)
    ) == segment_ids

    assert organization_assignments.assign_organization_video(
        subject_id,
        video_id,
    ) == segment_ids
    reassigned = SpeakerRepository(db).list_assignments(segment_ids)
    assert tuple(row.segment_id for row in reassigned) == segment_ids
    assert {row.assignment_kind for row in reassigned} == {
        AssignmentKind.SUBJECT
    }
    assert {row.assignment_origin for row in reassigned} == {
        AssignmentOrigin.CHANNEL_ORGANIZATION
    }


def test_organization_scope_excludes_manual_subject_assignment(db):
    subject_id = _create_subject(
        db,
        "Synthetic Organization Manual Boundary",
        SubjectKind.ORGANIZATION,
        channel_index=5,
    )
    _, segment_ids = _add_video_with_segments(
        db,
        subject_id=subject_id,
        youtube_video_id="synthetic-organization-manual",
        published_at=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        texts=("Synthetic manual organization assignment.",),
        channel_index=5,
    )
    _save_assignment(
        db,
        segment_id=segment_ids[0],
        kind=AssignmentKind.SUBJECT,
        subject_id=subject_id,
        evidence_hash="manual-organization-evidence-v1",
    )

    run = _begin(db, _create_job_for_input(db, subject_id))

    assert AnalysisRepository(db).get_input_segments(run.id) == ()


def test_distinct_repost_video_segments_with_identical_text_are_not_deduplicated(db):
    subject_id = _create_subject(
        db, "Synthetic Repost Person", SubjectKind.PERSON, channel_index=6
    )
    inserted = []
    for youtube_video_id in ("synthetic-repost-b", "synthetic-repost-a"):
        _, segment_ids = _add_video_with_segments(
            db,
            subject_id=subject_id,
            youtube_video_id=youtube_video_id,
            published_at=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
            texts=("Identical synthetic evidence.",),
            channel_index=6,
        )
        _save_assignment(
            db,
            segment_id=segment_ids[0],
            kind=AssignmentKind.SUBJECT,
            subject_id=subject_id,
            evidence_hash=f"assignment-{youtube_video_id}",
        )
        inserted.append((youtube_video_id, segment_ids[0]))

    run = _begin(db, _create_job_for_input(db, subject_id))
    segments = AnalysisRepository(db).get_input_segments(run.id)

    expected_by_video_id = tuple(
        segment_id for _, segment_id in sorted(inserted)
    )
    assert tuple(item.segment_id for item in segments) == expected_by_video_id
    assert len(segments) == 2


def test_analysis_job_for_different_input_contract_is_rejected_atomically(db):
    prepared = _prepare_personal_analysis(db)
    mismatched = _create_job_for_input(
        db,
        prepared.subject_id,
        input_contract_override="different-subject-or-cutoff",
    )

    with pytest.raises(DomainError) as error:
        _begin(db, mismatched)

    assert error.value.code == "ANALYSIS_JOB_INPUT_MISMATCH"
    assert db.execute("SELECT COUNT(*) FROM analysis_scopes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM analysis_input_snapshots"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "drift_sql",
    [
        (
            "UPDATE speaker_assignments SET evidence_hash=? "
            "WHERE assigned_subject_id=?",
            "changed-assignment-evidence",
        ),
        (
            "UPDATE subject_channel_policies SET policy_hash=? "
            "WHERE subject_id=?",
            "changed-policy-hash",
        ),
    ],
    ids=["assignment", "policy"],
)
def test_begin_recomputes_current_policy_and_assignment_contract(db, drift_sql):
    prepared = _prepare_personal_analysis(db)
    statement, changed_value = drift_sql
    db.execute(statement, (changed_value, prepared.subject_id))

    with pytest.raises(DomainError) as error:
        _begin(db, prepared)

    assert error.value.code == "ANALYSIS_JOB_INPUT_MISMATCH"
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_unchanged_eligibility_reevaluation_time_does_not_change_input(db):
    prepared = _prepare_personal_analysis(db)
    eligible_video_ids = tuple(
        row["video_id"]
        for row in db.execute(
            """
            SELECT video_id
            FROM subject_video_eligibility
            WHERE subject_id=?
            ORDER BY video_id
            """,
            (prepared.subject_id,),
        )
    )
    for video_id in eligible_video_ids:
        ChannelPolicyService(db).evaluate(
            prepared.subject_id, video_id, DiscoveryMethod.AUTO_SEARCH
        )

    run = _begin(db, prepared)

    assert tuple(
        item.segment_id
        for item in AnalysisRepository(db).get_input_segments(run.id)
    ) == prepared.expected_segment_ids


def test_empty_input_contract_changes_with_current_policy_hash(db):
    subject_id = _create_subject(
        db, "Synthetic Empty Policy Person", SubjectKind.PERSON, channel_index=8
    )
    service = AnalysisRunService(db)
    first = service.preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )
    assert db.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0

    db.execute(
        "UPDATE subject_channel_policies SET policy_hash=? WHERE subject_id=?",
        ("synthetic-empty-policy-v2", subject_id),
    )
    second = service.preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )

    assert second != first


def test_empty_input_contract_ignores_policy_display_and_update_time(db):
    subject_id = _create_subject(
        db,
        "Synthetic Empty Policy Metadata Person",
        SubjectKind.PERSON,
        channel_index=9,
    )
    service = AnalysisRunService(db)
    first = service.preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )

    db.execute(
        """
        UPDATE subject_channel_policies
        SET channel_display_name=?, updated_at=?
        WHERE subject_id=?
        """,
        ("Changed display only", "2026-09-01T00:00:00.000000Z", subject_id),
    )
    second = service.preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )

    assert second == first


def test_configuration_required_policy_rejects_preview_and_begin(db):
    subject_id = _create_subject(
        db,
        "Synthetic Configuration Required Person",
        SubjectKind.PERSON,
        channel_index=10,
    )
    prepared = _create_job_for_input(db, subject_id)
    db.execute(
        """
        UPDATE subject_channel_policies
        SET configuration_status=?, policy_hash=?
        WHERE subject_id=?
        """,
        (
            ConfigurationStatus.CONFIGURATION_REQUIRED.value,
            "synthetic-configuration-required-policy",
            subject_id,
        ),
    )

    with pytest.raises(DomainError) as preview_error:
        AnalysisRunService(db).preview_input_contract(
            subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
        )
    assert preview_error.value.code == "ANALYSIS_POLICY_NOT_CONFIGURED"

    with pytest.raises(DomainError) as begin_error:
        _begin(db, prepared)
    assert begin_error.value.code == "ANALYSIS_POLICY_NOT_CONFIGURED"
    assert db.execute("SELECT COUNT(*) FROM analysis_scopes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_preview_is_read_only(db):
    subject_id = _create_subject(
        db, "Synthetic Preview Person", SubjectKind.PERSON, channel_index=7
    )

    first = AnalysisRunService(db).preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )
    second = AnalysisRunService(db).preview_input_contract(
        subject_id, CUTOFF_DAY, AnalysisRunSettings.required()
    )

    assert first == second
    assert db.execute("SELECT COUNT(*) FROM analysis_scopes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0
