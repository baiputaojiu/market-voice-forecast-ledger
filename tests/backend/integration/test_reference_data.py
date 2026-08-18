import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_reference_people_and_profiles_are_seeded_exactly(db):
    bootstrap_reference_data(db)
    rows = db.execute(
        "SELECT id, canonical_name, is_active FROM analysis_subjects ORDER BY id"
    ).fetchall()
    assert tuple((row["canonical_name"], row["is_active"]) for row in rows) == (
        ("木野内栄治", 1),
        ("大川智宏", 1),
        ("江守哲", 1),
        ("千竈 鉄平", 1),
    )
    profiles = DiscoveryRepository(db)
    expected = {
        "木野内栄治": (("UCJ1DVBLVpe4FvBZZ94kreaQ",), ("木野内栄治",)),
        "大川智宏": ((), ("大川智宏",)),
        "江守哲": (("UCVXka7buS_WptsAzSE0LcKg",), ("江守哲",)),
        "千竈 鉄平": (("UCOfzLmXpI3qmZfV7_Cs1sYA",), ("千竈鉄平", "千竃鉄平")),
    }
    for row in rows:
        profile = profiles.get_current_profile_version(row["id"])
        assert (profile.seed_channel_ids, profile.search_terms) == expected[row["canonical_name"]]


def test_bootstrap_is_idempotent_and_detects_drift(db):
    bootstrap_reference_data(db)
    bootstrap_reference_data(db)
    assert db.execute("SELECT COUNT(*) FROM analysis_subjects").fetchone()[0] == 4
    db.execute("UPDATE analysis_subjects SET canonical_name='drift' WHERE id=1")
    with pytest.raises(DomainError) as caught:
        bootstrap_reference_data(db)
    assert caught.value.code == "BOOTSTRAP_REFERENCE_MISMATCH"
