from datetime import date, datetime, timedelta, timezone

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from tests.backend.synthetic_collection_fixture import create_synthetic_collection_candidate


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
    fixture = create_synthetic_collection_candidate(
        db, presence_state="presence_confirmed", assignment_kind="subject",
        youtube_video_id=f"stale-{label}"[-11:],
    )
    return fixture.subject_id, fixture, fixture.video_id, fixture.segment_id


def _scope_run(db, *, subject_id, policy, video_id, segment_id, cutoff_day: date):
    exclusive = datetime.combine(
        cutoff_day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ) - timedelta(hours=9)
    scope_id = db.execute(
        "INSERT INTO analysis_scopes(subject_id, cutoff_day_jst, cutoff_exclusive_utc, status, stale_reason) "
        "VALUES (?, ?, ?, 'current', NULL)",
        (subject_id, cutoff_day.isoformat(), exclusive.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    ).lastrowid
    run_id = db.execute(
        """
        INSERT INTO analysis_runs(
            scope_id, model, reasoning_effort, prompt_version, schema_version,
            information_boundary_version, input_hash, input_contract_hash, started_at
        ) VALUES (?, 'gpt-5.6-sol', 'max', 'm2-core-prompt-contract-v1',
                  'm2-analysis-output-v1', 'stored-statements-only-v1', ?, ?, ?)
        """,
        (scope_id, f"input-{scope_id}", f"contract-{scope_id}", FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    ).lastrowid
    assignment = db.execute(
        "SELECT * FROM speaker_assignments WHERE segment_id=?", (segment_id,)
    ).fetchone()
    db.execute(
        """
        INSERT INTO analysis_run_segments(
            run_id, segment_id, ordinal, video_id, published_at,
            metadata_snapshot_id, metadata_snapshot_hash,
            presence_decision_id, presence_decision_hash, speaker_assignment_id,
            assignment_kind, assigned_subject_id, assignment_updated_at,
            assignment_evidence_hash
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, segment_id, video_id, FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            policy.metadata_snapshot_id, policy.metadata_snapshot_hash,
            policy.presence_decision_id, policy.presence_decision_hash,
            assignment["id"], assignment["assignment_kind"], assignment["assigned_subject_id"],
            assignment["assigned_at"], assignment["evidence_hash"],
        ),
    )
    db.execute(
        "INSERT INTO analysis_input_snapshots(run_id,input_text,metadata_json,input_sha256,snapshot_created_at,expires_at,text_deleted_at) "
        "VALUES (?, ?, '{}', ?, ?, NULL, NULL)",
        (run_id, f"Immutable input {run_id}", f"snapshot-{run_id}", FIXED_UTC.strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    )
    return scope_id, run_id


def test_speaker_stale_is_deterministic_idempotent_and_non_destructive(db):
    subject_id, fixture, video_id, segment_id = _source_segment(db, "shared")
    scope_id, run_id = _scope_run(
        db, subject_id=subject_id, policy=fixture, video_id=video_id,
        segment_id=segment_id, cutoff_day=date(2026, 8, 14),
    )
    before = db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
    with transaction(db):
        affected = AnalysisRepository(db).mark_scope_ids_stale(
            (scope_id, scope_id), "SPEAKER_ASSIGNMENT_CHANGED"
        )
    assert affected == (scope_id,)
    assert db.execute("SELECT status FROM analysis_scopes WHERE id=?", (scope_id,)).fetchone()[0] == "stale"
    assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == before
    assert db.execute("SELECT id FROM analysis_runs WHERE id=?", (run_id,)).fetchone() is not None


def test_stale_transition_rejects_invalid_ids_and_reason_codes(db):
    with transaction(db):
        with pytest.raises(DomainError):
            AnalysisRepository(db).mark_scope_ids_stale((0,), "SPEAKER_ASSIGNMENT_CHANGED")
        with pytest.raises(DomainError):
            AnalysisRepository(db).mark_scope_ids_stale((1,), "unsafe reason")
