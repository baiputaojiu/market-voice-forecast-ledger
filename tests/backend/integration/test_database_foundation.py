import shutil
import subprocess
import sys
import textwrap
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.common import JST, cutoff_exclusive_utc, utc_iso


def test_settings_keep_runtime_data_outside_repository(tmp_path):
    settings = Settings.for_data_dir(tmp_path / "runtime")
    assert settings.database_path == tmp_path / "runtime" / "ledger.sqlite3"
    assert settings.temp_audio_dir == tmp_path / "runtime" / "temp-audio"


def test_migrations_apply_once_and_connection_enables_safety_pragmas(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert apply_migrations(conn) == ("0001_foundation",)
    assert apply_migrations(conn) == ()


def test_fixed_jst_cutoff_is_next_local_midnight_expressed_in_utc():
    assert JST.utcoffset(None) == timedelta(hours=9)
    assert cutoff_exclusive_utc(date(2026, 8, 14)) == datetime(
        2026, 8, 14, 15, 0, tzinfo=timezone.utc
    )


def test_utc_iso_normalizes_offset_and_uses_fixed_microsecond_precision():
    source = datetime(2026, 8, 15, 0, 0, 0, 123456, tzinfo=JST)
    assert utc_iso(source) == "2026-08-14T15:00:00.123456Z"


def test_built_wheel_contains_and_applies_embedded_migration(tmp_path):
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
    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=build_source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as archive:
        assert (
            "market_voice_forecast_ledger/db/migrations/0001_foundation.sql"
            in archive.namelist()
        )

    migration_result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            textwrap.dedent(
                """
                import sys
                from pathlib import Path

                sys.path.insert(0, sys.argv[1])

                from market_voice_forecast_ledger.db.connection import open_database
                from market_voice_forecast_ledger.db.migrate import apply_migrations

                conn = open_database(Path(sys.argv[2]))
                try:
                    assert apply_migrations(conn) == ("0001_foundation",)
                    exists = conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'app_metadata'"
                    ).fetchone()[0]
                    assert exists == 1
                finally:
                    conn.close()
                print("migration applied from wheel")
                """
            ),
            str(wheel),
            str(tmp_path / "wheel-runtime" / "ledger.sqlite3"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert migration_result.returncode == 0, (
        migration_result.stdout + migration_result.stderr
    )
    assert migration_result.stdout.strip() == "migration applied from wheel"
