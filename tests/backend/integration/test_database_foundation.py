from datetime import date, datetime, timedelta, timezone

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
