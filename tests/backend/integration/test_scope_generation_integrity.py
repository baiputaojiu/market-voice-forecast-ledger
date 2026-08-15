import sqlite3
from datetime import date

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.repositories.analysis import AnalysisRepository
from market_voice_forecast_ledger.services.current_results import CurrentResultService
from tests.backend.integration.test_stale_transitions import (
    _scope_run,
    _source_segment,
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _legacy_generation_zero_scope_and_run(db):
    subject_id, policy, video_id, segment_id = _source_segment(
        db, "legacy-generation-zero"
    )
    return _scope_run(
        db,
        subject_id=subject_id,
        policy=policy,
        video_id=video_id,
        segment_id=segment_id,
        cutoff_day=date(2026, 8, 14),
    )


def test_migrated_generation_zero_current_rows_remain_readable(tmp_path):
    conn = open_database(tmp_path / "legacy-ledger.sqlite3")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) "
            "VALUES ('0016_scope_generations', '2026-08-15T01:02:03.456789Z')"
        )
        apply_migrations(conn)
        scope_id, run_id = _legacy_generation_zero_scope_and_run(conn)
        conn.execute(
            "DELETE FROM schema_migrations WHERE name='0016_scope_generations'"
        )

        assert apply_migrations(conn) == ("0016_scope_generations",)
        repository = AnalysisRepository(conn)
        scope = repository.get_scope(scope_id)
        run = repository.get_run(run_id)

        assert scope.generation == 0
        assert run.scope_generation == 0
        assert CurrentResultService(conn).get_scope(scope_id).source_run_id == run_id
    finally:
        conn.close()


def test_generation_columns_reject_raw_rewrite_replace_and_nonmonotonic_scope(db):
    scope_id, run_id = _legacy_generation_zero_scope_and_run(db)
    assert "generation" in {
        row["name"] for row in db.execute("PRAGMA table_info('analysis_scopes')")
    }
    assert "scope_generation" in {
        row["name"] for row in db.execute("PRAGMA table_info('analysis_runs')")
    }

    db.execute(
        "UPDATE analysis_scopes SET generation=generation+1 WHERE id=?",
        (scope_id,),
    )
    assert db.execute(
        "SELECT generation FROM analysis_scopes WHERE id=?", (scope_id,)
    ).fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE analysis_runs SET scope_generation=1 WHERE id=?", (run_id,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_runs(
                id, scope_id, model, reasoning_effort, prompt_version,
                schema_version, information_boundary_version, input_hash,
                input_contract_hash, started_at, scope_generation
            )
            SELECT id, scope_id, model, reasoning_effort, prompt_version,
                   schema_version, information_boundary_version, input_hash,
                   input_contract_hash, started_at, 1
            FROM analysis_runs
            WHERE id=?
            """,
            (run_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT OR REPLACE INTO analysis_scopes(
                id, subject_id, cutoff_day_jst, cutoff_exclusive_utc,
                status, stale_reason, generation
            )
            SELECT id, subject_id, cutoff_day_jst, cutoff_exclusive_utc,
                   status, stale_reason, 0
            FROM analysis_scopes
            WHERE id=?
            """,
            (scope_id,),
        )

    for invalid_generation in (-1, 1, 3, 1.5, "not-a-generation"):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE analysis_scopes SET generation=? WHERE id=?",
                (invalid_generation, scope_id),
            )
        assert db.execute(
            "SELECT generation FROM analysis_scopes WHERE id=?", (scope_id,)
        ).fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO analysis_runs(
                scope_id, model, reasoning_effort, prompt_version,
                schema_version, information_boundary_version, input_hash,
                input_contract_hash, started_at, scope_generation
            ) VALUES (?, 'gpt-5.6-sol', 'max', 'm2-core-prompt-contract-v1',
                      'm2-analysis-output-v1', 'stored-statements-only-v1',
                      'mismatched-input', 'mismatched-contract',
                      '2026-08-15T01:02:03.456789Z', 0)
            """,
            (scope_id,),
        )


def test_stale_scope_updates_chunk_below_the_connection_bind_limit(db):
    subject_id, _, _, _ = _source_segment(db, "stale-bind-limit")
    scope_ids = tuple(
        db.execute(
            """
            INSERT INTO analysis_scopes(
                subject_id, cutoff_day_jst, cutoff_exclusive_utc,
                status, stale_reason
            ) VALUES (?, ?, '2026-08-14T15:00:00.000000Z', 'current', NULL)
            """,
            (subject_id, f"synthetic-bind-limit-{ordinal}"),
        ).lastrowid
        for ordinal in range(7)
    )
    old_limit = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
    try:
        with transaction(db):
            affected = AnalysisRepository(db).mark_scope_ids_stale(
                scope_ids, "SPEAKER_ASSIGNMENT_CHANGED"
            )
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)

    assert affected == scope_ids
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT status, stale_reason, generation "
            "FROM analysis_scopes WHERE id IN ("
            + ",".join("?" for _ in scope_ids)
            + ") ORDER BY id",
            scope_ids,
        )
    ) == (("stale", "SPEAKER_ASSIGNMENT_CHANGED", 1),) * len(scope_ids)
