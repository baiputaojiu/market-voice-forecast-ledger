import json
import os
import sqlite3
import shutil
import subprocess
import sys
import textwrap
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import (
    JST,
    canonical_json,
    cutoff_exclusive_utc,
    sha256_text,
    to_jst,
    utc_iso,
)
from market_voice_forecast_ledger.domain.errors import DomainError


EXPECTED_MIGRATIONS = (
    "0001_foundation",
    "0002_audit",
    "0003_sources",
    "0004_speakers",
    "0005_jobs",
    "0006_analysis_runs",
    "0007_analysis_outputs",
    "0008_statements",
    "0009_periods",
    "0010_asset_mappings",
    "0011_mapping_reviews",
    "0012_forecast_projections",
    "0013_current_results",
    "0013_video_pipeline_bindings",
    "0014_heatmap",
    "0015_retention",
    "0016_scope_generations",
    "0017_append_only_guards",
    "0018_youtube_discovery_cutover",
    "0019_market_masters_seed_channel",
)


def test_settings_keep_runtime_data_outside_repository(tmp_path):
    settings = Settings.for_data_dir(tmp_path / "runtime")
    assert settings.database_path == tmp_path / "runtime" / "ledger.sqlite3"
    assert settings.temp_audio_dir == tmp_path / "runtime" / "temp-audio"


def test_migrations_apply_once_and_connection_enables_safety_pragmas(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    applied = apply_migrations(conn)
    assert applied[0] == "0001_foundation"
    assert apply_migrations(conn) == ()


def test_connection_enables_wal_timeout_and_named_row_lookup(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        row = conn.execute("SELECT 42 AS answer").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["answer"] == 42
    finally:
        conn.close()


def test_transaction_commits_and_tracks_active_state(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    conn = open_database(database_path)
    try:
        conn.execute("CREATE TABLE committed_values(value TEXT NOT NULL)")
        assert conn.in_transaction is False
        with transaction(conn):
            assert conn.in_transaction is True
            conn.execute("INSERT INTO committed_values(value) VALUES ('saved')")
        assert conn.in_transaction is False
    finally:
        conn.close()

    reopened = open_database(database_path)
    try:
        assert reopened.execute(
            "SELECT value FROM committed_values"
        ).fetchone()["value"] == "saved"
    finally:
        reopened.close()


def test_transaction_rolls_back_exception_and_clears_active_state(tmp_path):
    database_path = tmp_path / "ledger.sqlite3"
    conn = open_database(database_path)
    try:
        conn.execute("CREATE TABLE rolled_back_values(value TEXT NOT NULL)")
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            with transaction(conn):
                assert conn.in_transaction is True
                conn.execute("INSERT INTO rolled_back_values(value) VALUES ('lost')")
                raise RuntimeError("synthetic rollback")
        assert conn.in_transaction is False
    finally:
        conn.close()

    reopened = open_database(database_path)
    try:
        assert reopened.execute(
            "SELECT COUNT(*) FROM rolled_back_values"
        ).fetchone()[0] == 0
    finally:
        reopened.close()


def test_fixed_jst_cutoff_is_next_local_midnight_expressed_in_utc():
    assert JST.utcoffset(None) == timedelta(hours=9)
    assert cutoff_exclusive_utc(date(2026, 8, 14)) == datetime(
        2026, 8, 14, 15, 0, tzinfo=timezone.utc
    )


def test_utc_iso_normalizes_offset_and_uses_fixed_microsecond_precision():
    source = datetime(2026, 8, 15, 0, 0, 0, 123456, tzinfo=JST)
    assert utc_iso(source) == "2026-08-14T15:00:00.123456Z"


def test_canonical_json_is_compact_sorted_and_preserves_non_ascii():
    assert canonical_json({"z": [2, 1], "a": "日本"}) == '{"a":"日本","z":[2,1]}'


def test_sha256_text_matches_known_abc_vector():
    assert sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )


def test_to_jst_uses_fixed_utc_plus_nine_timezone():
    converted = to_jst(datetime(2026, 8, 14, 15, 30, tzinfo=timezone.utc))
    assert converted == datetime(2026, 8, 15, 0, 30, tzinfo=JST)
    assert converted.tzinfo is JST


@pytest.mark.parametrize("converter", [utc_iso, to_jst])
def test_datetime_helpers_reject_naive_values(converter):
    with pytest.raises(ValueError, match="timezone-aware"):
        converter(datetime(2026, 8, 15, 0, 30))


def test_domain_error_preserves_code_message_and_string():
    error = DomainError("SYNTHETIC_CODE", "Synthetic safe message.")
    assert error.code == "SYNTHETIC_CODE"
    assert error.message == "Synthetic safe message."
    assert str(error) == "Synthetic safe message."


def test_offline_wheel_contains_exact_migrations_and_append_only_audit(tmp_path):
    project_root = Path(__file__).resolve().parents[3]
    build_source = tmp_path / "source"
    build_source.mkdir()
    shutil.copy2(project_root / "pyproject.toml", build_source / "pyproject.toml")
    shutil.copytree(
        project_root / "src",
        build_source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    wheel_dir = tmp_path / "wheel"
    offline_environment = os.environ.copy()
    offline_environment.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=build_source,
        capture_output=True,
        text=True,
        check=False,
        env=offline_environment,
    )
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as archive:
        migration_members = tuple(
            sorted(
                name
                for name in archive.namelist()
                if name.startswith(
                    "market_voice_forecast_ledger/db/migrations/"
                )
                and name.endswith(".sql")
            )
        )
        assert migration_members == tuple(
            "market_voice_forecast_ledger/db/migrations/" + name + ".sql"
            for name in EXPECTED_MIGRATIONS
        )

    migration_result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            textwrap.dedent(
                """
                import json
                import sqlite3
                import sys
                from pathlib import Path

                sys.path.insert(0, sys.argv[1])

                from market_voice_forecast_ledger.db.connection import open_database
                from market_voice_forecast_ledger.db.migrate import apply_migrations

                conn = open_database(Path(sys.argv[2]))
                try:
                    applied = apply_migrations(conn)
                    expected = tuple(json.loads(sys.argv[3]))
                    assert applied == expected
                    assert tuple(
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM schema_migrations ORDER BY rowid"
                        )
                    ) == expected
                    assert apply_migrations(conn) == ()
                    assert tuple(
                        row["name"] for row in conn.execute("PRAGMA table_info(audit_events)")
                    ) == (
                        "id",
                        "entity_type",
                        "entity_id",
                        "scope_id",
                        "operation",
                        "actor_kind",
                        "reason_code",
                        "reason_text",
                        "before_json",
                        "after_json",
                        "created_at",
                    )
                    trigger_names = {
                        row["name"]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='trigger' AND tbl_name='audit_events' ORDER BY name"
                        )
                    }
                    assert {
                        "audit_events_no_delete",
                        "audit_events_no_update",
                    } <= trigger_names
                    event_id = conn.execute(
                        "INSERT INTO audit_events("
                        "entity_type, entity_id, scope_id, operation, actor_kind, "
                        "reason_code, reason_text, before_json, after_json, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "synthetic",
                            "1",
                            None,
                            "create",
                            "system",
                            "SYNTHETIC",
                            "Synthetic reason.",
                            None,
                            None,
                            "2026-08-16T00:00:00.000000Z",
                        ),
                    ).lastrowid
                    for statement in (
                        "UPDATE audit_events SET reason_code='changed' WHERE id=?",
                        "DELETE FROM audit_events WHERE id=?",
                    ):
                        try:
                            conn.execute(statement, (event_id,))
                        except sqlite3.IntegrityError as error:
                            assert str(error) == "APPEND_ONLY"
                        else:
                            raise AssertionError("audit history mutation was accepted")
                finally:
                    conn.close()
                print("exact migrations and audit guards verified from wheel")
                """
            ),
            str(wheel),
            str(tmp_path / "wheel-runtime" / "ledger.sqlite3"),
            json.dumps(EXPECTED_MIGRATIONS),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert migration_result.returncode == 0, (
        migration_result.stdout + migration_result.stderr
    )
    assert (
        migration_result.stdout.strip()
        == "exact migrations and audit guards verified from wheel"
    )
