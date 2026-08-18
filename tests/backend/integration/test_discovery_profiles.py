import hashlib
import importlib
import json

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError


EXPECTED_PROFILES = (
    ("木野内栄治", ("UCJ1DVBLVpe4FvBZZ94kreaQ",), ("木野内栄治",)),
    ("大川智宏", (), ("大川智宏",)),
    ("江守哲", ("UCVXka7buS_WptsAzSE0LcKg",), ("江守哲",)),
    (
        "千竈 鉄平",
        ("UCOfzLmXpI3qmZfV7_Cs1sYA",),
        ("千竈鉄平", "千竃鉄平"),
    ),
)


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _config_hash(seed_channel_ids: tuple[str, ...], search_terms: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "schema": "youtube-discovery-profile.v1",
            "search_terms": list(search_terms),
            "seed_channel_ids": list(seed_channel_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_domain_exposes_only_the_approved_collection_values():
    discovery = importlib.import_module(
        "market_voice_forecast_ledger.domain.discovery"
    )
    enums = importlib.import_module("market_voice_forecast_ledger.domain.enums")

    assert tuple(item.value for item in discovery.DiscoverySourceKind) == (
        "seed_uploads",
        "cross_channel_search",
        "manual_url",
    )
    assert tuple(item.value for item in discovery.PresenceState) == (
        "presence_unverified",
        "presence_confirmed",
        "presence_rejected",
    )
    assert tuple(item.value for item in discovery.PresenceOrigin) == (
        "collection_initial",
        "voice_verification",
    )
    assert tuple(item.value for item in discovery.LiveState) == (
        "not_live",
        "live",
        "upcoming",
    )
    assert tuple(item.value for item in enums.JobKind) == (
        "video_pipeline",
        "analysis_scope",
        "youtube_sync",
    )
    for obsolete in (
        "SubjectKind",
        "PolicyKind",
        "ConfigurationStatus",
        "DiscoveryMethod",
        "EligibilityStatus",
    ):
        assert not hasattr(enums, obsolete)
    assert not hasattr(enums.AssignmentOrigin, "CHANNEL_ORGANIZATION")


def test_bootstrap_creates_exact_four_person_discovery_profiles(db):
    bootstrap_reference_data(db)

    subjects = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT canonical_name, is_active FROM analysis_subjects ORDER BY id"
        )
    )
    assert subjects == tuple((name, 1) for name, _, _ in EXPECTED_PROFILES)
    assert tuple(
        row[0] for row in db.execute("SELECT alias FROM subject_aliases ORDER BY alias")
    ) == ()

    actual = []
    for row in db.execute(
        """
        SELECT subject.canonical_name, profile.id, profile.current_version_id,
               version.config_hash
        FROM analysis_subjects AS subject
        JOIN discovery_profiles AS profile ON profile.subject_id=subject.id
        JOIN discovery_profile_versions AS version
          ON version.id=profile.current_version_id
        ORDER BY subject.id
        """
    ):
        seeds = tuple(
            item[0]
            for item in db.execute(
                "SELECT youtube_channel_id FROM discovery_seed_channels "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (row["current_version_id"],),
            )
        )
        terms = tuple(
            item[0]
            for item in db.execute(
                "SELECT search_term FROM discovery_search_terms "
                "WHERE profile_version_id=? ORDER BY ordinal",
                (row["current_version_id"],),
            )
        )
        actual.append((row["canonical_name"], seeds, terms, row["config_hash"]))

    assert tuple(actual) == tuple(
        (name, seeds, terms, _config_hash(seeds, terms))
        for name, seeds, terms in EXPECTED_PROFILES
    )
    serialized = json.dumps(actual, ensure_ascii=False)
    for rejected in ("木野内英二", "大川智ひろ", "暁投資顧問 千竈", "暁投資顧問 千竃"):
        assert rejected not in serialized


def test_bootstrap_is_idempotent_and_verifies_stored_rows(db):
    bootstrap_reference_data(db)
    before = tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "analysis_subjects",
            "discovery_profiles",
            "discovery_profile_versions",
            "discovery_seed_channels",
            "discovery_search_terms",
        )
    )

    bootstrap_reference_data(db)

    after = tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table, _ in before
    )
    assert after == before


def test_bootstrap_rejects_a_mismatched_current_profile_without_rewriting_it(db):
    bootstrap_reference_data(db)
    profile_id = db.execute(
        """
        SELECT profile.id
        FROM discovery_profiles AS profile
        JOIN analysis_subjects AS subject ON subject.id=profile.subject_id
        WHERE subject.canonical_name='木野内栄治'
        """
    ).fetchone()[0]
    cursor = db.execute(
        "INSERT INTO discovery_profile_versions(profile_id, config_hash, created_at) "
        "VALUES (?, 'user-config', '2026-08-18T00:00:00.000000Z')",
        (profile_id,),
    )
    db.execute(
        "UPDATE discovery_profiles SET current_version_id=? WHERE id=?",
        (cursor.lastrowid, profile_id),
    )
    before = tuple(
        tuple(row)
        for row in db.execute(
            "SELECT id, current_version_id FROM discovery_profiles ORDER BY id"
        )
    )

    with pytest.raises(DomainError) as caught:
        bootstrap_reference_data(db)

    assert caught.value.code == "BOOTSTRAP_REFERENCE_MISMATCH"
    assert tuple(
        tuple(row)
        for row in db.execute(
            "SELECT id, current_version_id FROM discovery_profiles ORDER BY id"
        )
    ) == before


def test_discovery_repository_reads_the_immutable_current_version(db):
    bootstrap_reference_data(db)
    repository_module = importlib.import_module(
        "market_voice_forecast_ledger.repositories.discovery"
    )
    subject_id = db.execute(
        "SELECT id FROM analysis_subjects WHERE canonical_name='千竈 鉄平'"
    ).fetchone()[0]

    version = repository_module.DiscoveryRepository(
        db
    ).get_current_profile_version(subject_id)

    assert version.profile_id > 0
    assert version.seed_channel_ids == ("UCOfzLmXpI3qmZfV7_Cs1sYA",)
    assert version.search_terms == ("千竈鉄平", "千竃鉄平")
    assert version.config_hash == _config_hash(
        version.seed_channel_ids, version.search_terms
    )
