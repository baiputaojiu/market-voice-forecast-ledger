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
from market_voice_forecast_ledger.domain.speakers import SpeakerAssignment
from market_voice_forecast_ledger.domain.sources import ChannelPolicy, VideoInput
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.repositories.speakers import SpeakerRepository
from market_voice_forecast_ledger.repositories.sources import SourceRepository
from market_voice_forecast_ledger.services.channel_policy import ChannelPolicyService
from market_voice_forecast_ledger.services.current_results import CurrentResultService
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


def _source_segment(db, label: str):
    sources = SourceRepository(db)
    subject_id = sources.create_subject(
        f"Synthetic Stale Subject {label}", SubjectKind.PERSON
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
            youtube_video_id=f"synthetic-stale-{label}",
            youtube_channel_id="UC1000000000000000000000",
            channel_display_name="Synthetic Channel",
            title=f"Synthetic stale source {label}",
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
        f"input-{label}",
        f"output-{label}",
        UnitStatus.SUCCESS,
    )
    segment_id = speakers.add_segment(
        video_id,
        chunk_id,
        0,
        1_000,
        4_000,
        f"Private synthetic transcript {label}.",
        f"anonymous-{label}",
        FIXED_UTC,
        FIXED_UTC + timedelta(days=365),
    )
    speakers.save_assignment(
        SpeakerAssignment(
            segment_id=segment_id,
            assignment_kind=AssignmentKind.SUBJECT,
            assigned_subject_id=subject_id,
            assignment_origin=AssignmentOrigin.MANUAL,
            raw_match_score=None,
            model_name=None,
            model_version=None,
            threshold_config_version=None,
            evidence_hash=f"assignment-{label}",
            assigned_at=FIXED_UTC,
        )
    )
    return subject_id, policy, video_id, segment_id


def _scope_run(
    db,
    *,
    subject_id: int,
    policy,
    video_id: int,
    segment_id: int,
    cutoff_day: date,
):
    cutoff = cutoff_day.isoformat()
    exclusive = datetime.combine(
        cutoff_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(hours=9)
    scope_cursor = db.execute(
        "INSERT INTO analysis_scopes(subject_id, cutoff_day_jst, "
        "cutoff_exclusive_utc, status, stale_reason) VALUES (?, ?, ?, 'current', NULL)",
        (
            subject_id,
            cutoff,
            exclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        ),
    )
    scope_id = scope_cursor.lastrowid
    run_cursor = db.execute(
        """
        INSERT INTO analysis_runs(
            scope_id, model, reasoning_effort, prompt_version, schema_version,
            information_boundary_version, input_hash, input_contract_hash,
            started_at
        ) VALUES (?, 'gpt-5.6-sol', 'max', 'm2-core-prompt-contract-v1',
                  'm2-analysis-output-v1', 'stored-statements-only-v1',
                  ?, ?, ?)
        """,
        (
            scope_id,
            f"input-{scope_id}",
            f"contract-{scope_id}",
            FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        ),
    )
    run_id = run_cursor.lastrowid
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
            assignment["assignment_kind"],
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
        ) VALUES (?, ?, '{}', ?, ?, NULL, NULL)
        """,
        (
            run_id,
            f"Immutable private input for run {run_id}",
            f"snapshot-{run_id}",
            FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        ),
    )
    batch_cursor = db.execute(
        """
        INSERT INTO forecast_projection_batches(
            run_id, trigger_kind, latest_mapping_review_id,
            latest_period_review_id, created_at
        ) VALUES (?, 'initial', NULL, NULL, ?)
        """,
        (run_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    )
    batch_id = batch_cursor.lastrowid
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
    return scope_id, run_id


def _immutable_state(db):
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


def test_segment_stale_is_distinct_deterministic_idempotent_and_non_destructive(db):
    subject_id, policy, video_id, segment_id = _source_segment(db, "shared")
    first_scope, _ = _scope_run(
        db,
        subject_id=subject_id,
        policy=policy,
        video_id=video_id,
        segment_id=segment_id,
        cutoff_day=date(2026, 8, 14),
    )
    second_scope, _ = _scope_run(
        db,
        subject_id=subject_id,
        policy=policy,
        video_id=video_id,
        segment_id=segment_id,
        cutoff_day=date(2026, 8, 15),
    )
    other_subject, other_policy, other_video, other_segment = _source_segment(
        db, "unrelated"
    )
    unrelated_scope, _ = _scope_run(
        db,
        subject_id=other_subject,
        policy=other_policy,
        video_id=other_video,
        segment_id=other_segment,
        cutoff_day=date(2026, 8, 14),
    )
    before = _immutable_state(db)
    current_before = CurrentResultService(db).get_scope(first_scope)
    repository = AnalysisRepository(db)

    with transaction(db):
        first = repository.mark_scopes_using_segment_stale(
            segment_id, "SPEAKER_ASSIGNMENT_CHANGED"
        )
    after_first = _immutable_state(db)
    with transaction(db):
        second = repository.mark_scopes_using_segment_stale(
            segment_id, "SPEAKER_ASSIGNMENT_CHANGED"
        )

    assert first == second == tuple(sorted((first_scope, second_scope)))
    assert _immutable_state(db) == after_first == before
    assert CurrentResultService(db).get_scope(first_scope) == current_before
    for scope_id in (first_scope, second_scope):
        scope = repository.get_scope(scope_id)
        assert (scope.status, scope.stale_reason) == (
            ScopeStatus.STALE,
            "SPEAKER_ASSIGNMENT_CHANGED",
        )
    assert repository.get_scope(unrelated_scope).status is ScopeStatus.CURRENT


def test_policy_stale_uses_policy_id_and_leaves_other_policy_current(db):
    subject_id, policy, video_id, segment_id = _source_segment(db, "policy")
    first_scope, _ = _scope_run(
        db,
        subject_id=subject_id,
        policy=policy,
        video_id=video_id,
        segment_id=segment_id,
        cutoff_day=date(2026, 8, 13),
    )
    second_scope, _ = _scope_run(
        db,
        subject_id=subject_id,
        policy=policy,
        video_id=video_id,
        segment_id=segment_id,
        cutoff_day=date(2026, 8, 14),
    )
    other_subject, other_policy, other_video, other_segment = _source_segment(
        db, "other-policy"
    )
    unrelated_scope, _ = _scope_run(
        db,
        subject_id=other_subject,
        policy=other_policy,
        video_id=other_video,
        segment_id=other_segment,
        cutoff_day=date(2026, 8, 13),
    )
    repository = AnalysisRepository(db)

    with transaction(db):
        scope_ids = repository.mark_scopes_using_policy_stale(
            policy.id, "CHANNEL_POLICY_CHANGED"
        )

    assert scope_ids == tuple(sorted((first_scope, second_scope)))
    assert repository.get_scope(unrelated_scope).status is ScopeStatus.CURRENT


@pytest.mark.parametrize(
    ("column", "index_name"),
    [
        ("segment_id", "analysis_run_segments_segment_run"),
        ("policy_id", "analysis_run_segments_policy_run"),
    ],
)
def test_stale_reverse_lookup_uses_a_dedicated_index(db, column, index_name):
    index_names = {
        row["name"]
        for row in db.execute("PRAGMA index_list('analysis_run_segments')")
    }
    assert index_name in index_names

    plan = " ".join(
        row["detail"]
        for row in db.execute(
            f"""
            EXPLAIN QUERY PLAN
            SELECT DISTINCT run.scope_id
            FROM analysis_run_segments AS run_segment
            JOIN analysis_runs AS run ON run.id=run_segment.run_id
            WHERE run_segment.{column}=?
            """,
            (1,),
        )
    )
    assert index_name in plan


@pytest.mark.parametrize(
    ("method_name", "identifier", "reason"),
    [
        ("mark_scopes_using_segment_stale", 0, "SPEAKER_ASSIGNMENT_CHANGED"),
        ("mark_scopes_using_segment_stale", True, "SPEAKER_ASSIGNMENT_CHANGED"),
        ("mark_scopes_using_policy_stale", -1, "CHANNEL_POLICY_CHANGED"),
        ("mark_scopes_using_policy_stale", 1, ""),
        ("mark_scopes_using_policy_stale", 1, "unsafe reason"),
    ],
)
def test_stale_transition_rejects_invalid_ids_and_reason_codes(
    db, method_name, identifier, reason
):
    method = getattr(AnalysisRepository(db), method_name)

    with transaction(db), pytest.raises(DomainError) as error:
        method(identifier, reason)

    assert error.value.code == "STALE_TRANSITION_INVALID"
