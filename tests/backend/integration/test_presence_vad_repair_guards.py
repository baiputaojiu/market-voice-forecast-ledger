import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import _execute_script, apply_migrations
from market_voice_forecast_ledger.domain.common import canonical_json, utc_iso
from tests.backend.integration.test_voice_verification_jobs import (
    NOW,
    canonical_run,
    db,
    mark_job_succeeded,
    seed_job,
)


def test_new_connection_denies_repair_authorization(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "guard.sqlite3")
    try:
        assert conn.execute(
            "SELECT presence_vad_repair_delete_authorized('jobs', '1')"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_guard_passes_exact_row_identity_to_authorizer(db) -> None:
    job = seed_job(db)
    run_id = canonical_run(db, job)
    segment_id = db.execute(
        "SELECT id FROM voice_verification_segments WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchone()[0]
    seen = []

    def authorize(table, identity):
        seen.append((table, identity))
        return int(
            db.in_transaction
            and (table, identity) == ("voice_verification_segments", str(segment_id))
        )

    db.create_function("presence_vad_repair_delete_authorized", 2, authorize)
    with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_VOICE_RUN"):
        db.execute("DELETE FROM voice_verification_segments WHERE id=?", (segment_id,))
    db.execute("BEGIN IMMEDIATE")
    try:
        assert db.execute(
            "DELETE FROM voice_verification_segments WHERE id=?", (segment_id,)
        ).rowcount == 1
    finally:
        db.rollback()
        db.create_function("presence_vad_repair_delete_authorized", 2, lambda *_: 0)
    assert seen == [("voice_verification_segments", str(segment_id))] * 2
    assert db.execute(
        "SELECT COUNT(*) FROM voice_verification_segments WHERE id=?", (segment_id,)
    ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "identity_column"),
    (
        ("jobs", "id"),
        ("job_units", "job_id"),
        ("job_unit_attempts", "job_id"),
        ("job_events", "job_id"),
        ("video_pipeline_job_bindings", "job_id"),
        ("video_pipeline_job_binding_sets", "job_id"),
        ("voice_verification_manifests", "job_id"),
        ("voice_verification_runs", "job_id"),
    ),
)
def test_ordinary_delete_stays_forbidden(db, table, identity_column) -> None:
    job = seed_job(db)
    canonical_run(db, job)
    mark_job_succeeded(db, job.job_id)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f"DELETE FROM {table} WHERE {identity_column}=?", (job.job_id,))


def _insert_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO voice_vad_repairs("
        "schema_version, from_vad_contract_version, to_vad_contract_version, "
        "preview_hash, target_fingerprint, preserved_fingerprint, candidate_order_hash, "
        "database_backup_sha256, runtime_backup_fingerprint, deleted_counts_json, "
        "candidate_ids_json, old_job_ids_json, new_job_ids_json, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "presence-vad-repair.v1", "vad-v1", "vad-v2",
            "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64,
            canonical_json({"jobs": 20}),
            canonical_json(list(range(1, 21))),
            canonical_json(list(range(21, 41))),
            canonical_json(list(range(41, 61))),
            utc_iso(NOW),
        ),
    )


def test_repair_ledger_is_one_shot_and_append_only(db) -> None:
    _insert_ledger(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ledger(db)
    for sql in (
        "DELETE FROM voice_vad_repairs",
        "UPDATE voice_vad_repairs SET preview_hash='" + "0" * 64 + "'",
        "INSERT OR REPLACE INTO voice_vad_repairs SELECT * FROM voice_vad_repairs",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql)
    assert db.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 1


def test_schema_migration_preserves_existing_presence_data(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "legacy.sqlite3")
    try:
        conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        scripts = sorted(
            item for item in resources.files("market_voice_forecast_ledger.db.migrations").iterdir()
            if item.name.endswith(".sql") and item.name[:4].isdigit() and item.name[:4] <= "0020"
        )
        for script in scripts:
            with transaction(conn):
                _execute_script(conn, script.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (script.name.removesuffix(".sql"), utc_iso(NOW)),
                )
        from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data

        bootstrap_reference_data(conn)
        job = seed_job(conn)
        canonical_run(conn, job)
        mark_job_succeeded(conn, job.job_id)
        tables = tuple(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_migrations' ORDER BY name"
            )
        )
        before = {name: tuple(tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')) for name in tables}

        assert apply_migrations(conn) == ("0021_presence_vad_repair",)

        after = {name: tuple(tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')) for name in tables}
        assert after == before
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
