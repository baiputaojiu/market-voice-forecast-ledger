import hashlib
import importlib
import json
from datetime import datetime, timezone

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.discovery import canonical_profile_hash
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from tests.backend.synthetic_collection_fixture import (
    create_synthetic_collection_candidate,
)


EXPECTED_PROFILES = (
    ("木野内栄治", ("UCXvjRTXoDa8tKwdkTaukGug",), ("木野内栄治",)),
    ("大川智宏", (), ("大川智宏",)),
    ("江守哲", ("UCVXka7buS_WptsAzSE0LcKg",), ("江守哲",)),
    (
        "千竈 鉄平",
        ("UCOfzLmXpI3qmZfV7_Cs1sYA",),
        ("千竈鉄平", "千竃鉄平"),
    ),
)
FIXED_NOW = datetime(2026, 8, 18, 1, 2, 3, tzinfo=timezone.utc)


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


def test_profile_version_change_is_append_only_and_preserves_order(db):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    bootstrap_reference_data(db)
    repository = DiscoveryRepository(db)
    original = repository.get_current_profile_version_by_subject_name(
        "千竈 鉄平"
    )
    changed_terms = ("千竈 鉄平", "千竈鉄平", "千竃鉄平")

    changed = DiscoveryProfileService(
        db, clock=lambda: FIXED_NOW
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=("UCOfzLmXpI3qmZfV7_Cs1sYA",),
            search_terms=changed_terms,
            reason="verified ordered spelling set",
        )
    )

    assert changed.id != original.id
    assert repository.get_profile_version(original.id) == original
    assert changed.search_terms == changed_terms
    assert changed.config_hash == _config_hash(
        changed.seed_channel_ids, changed_terms
    )
    assert repository.get_current_profile_version(original.subject_id) == changed
    audit = db.execute(
        "SELECT * FROM audit_events WHERE entity_type='discovery_profile' "
        "AND entity_id=? ORDER BY id DESC LIMIT 1",
        (str(original.profile_id),),
    ).fetchone()
    assert audit is not None
    assert audit["operation"] == "replace_version"
    assert audit["reason_text"] == "verified ordered spelling set"
    assert json.loads(audit["before_json"]) == {
        "config_hash": original.config_hash,
        "profile_id": original.profile_id,
        "profile_version_id": original.id,
        "subject_id": original.subject_id,
    }
    assert json.loads(audit["after_json"]) == {
        "config_hash": changed.config_hash,
        "profile_id": changed.profile_id,
        "profile_version_id": changed.id,
        "subject_id": changed.subject_id,
    }


def test_identical_profile_configuration_is_a_noop_without_audit(db):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    bootstrap_reference_data(db)
    repository = DiscoveryRepository(db)
    original = repository.get_current_profile_version_by_subject_name(
        "木野内栄治"
    )
    before = tuple(
        db.execute(
            "SELECT id, current_version_id FROM discovery_profiles ORDER BY id"
        )
    )
    audit_count = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    unchanged = DiscoveryProfileService(
        db, clock=lambda: FIXED_NOW
    ).replace_version(
        ReplaceDiscoveryProfileVersion(
            subject_id=original.subject_id,
            seed_channel_ids=original.seed_channel_ids,
            search_terms=original.search_terms,
            reason="confirmed unchanged profile",
        )
    )

    assert unchanged == original
    assert tuple(
        db.execute(
            "SELECT id, current_version_id FROM discovery_profiles ORDER BY id"
        )
    ) == before
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_profile_versions WHERE profile_id=?",
        (original.profile_id,),
    ).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == audit_count


def test_list_active_profile_versions_returns_validated_current_rows(db):
    bootstrap_reference_data(db)

    versions = DiscoveryRepository(db).list_active_profile_versions()

    assert tuple(version.subject_id for version in versions) == tuple(
        row[0] for row in db.execute("SELECT id FROM analysis_subjects ORDER BY id")
    )
    assert tuple(
        (version.seed_channel_ids, version.search_terms) for version in versions
    ) == tuple((seeds, terms) for _, seeds, terms in EXPECTED_PROFILES)


@pytest.mark.parametrize(
    "overrides",
    (
        {"subject_id": True},
        {"seed_channel_ids": ["UCJ1DVBLVpe4FvBZZ94kreaQ"]},
        {"search_terms": ["木野内栄治"]},
        {"seed_channel_ids": ("UCshort",)},
        {
            "seed_channel_ids": (
                "UCJ1DVBLVpe4FvBZZ94kreaQ",
                "UCJ1DVBLVpe4FvBZZ94kreaQ",
            )
        },
        {"search_terms": ()},
        {"search_terms": ("   ",)},
        {"search_terms": ("あ" * 101,)},
        {"search_terms": ("木野内栄治", "木野内栄治")},
        {"reason": 7},
    ),
)
def test_profile_replacement_rejects_noncanonical_exact_types_before_write(
    db, overrides
):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    bootstrap_reference_data(db)
    original = DiscoveryRepository(db).get_current_profile_version_by_subject_name(
        "木野内栄治"
    )
    values = {
        "subject_id": original.subject_id,
        "seed_channel_ids": original.seed_channel_ids,
        "search_terms": ("木野内栄治", "木野内 栄治"),
        "reason": "verified expanded profile",
    }
    values.update(overrides)
    before = tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "discovery_profile_versions",
            "discovery_seed_channels",
            "discovery_search_terms",
            "audit_events",
        )
    )

    with pytest.raises(DomainError) as caught:
        DiscoveryProfileService(db, clock=lambda: FIXED_NOW).replace_version(
            ReplaceDiscoveryProfileVersion(**values)
        )

    assert caught.value.code in {
        "DISCOVERY_PROFILE_INVALID",
        "AUDIT_SCALAR_INVALID",
    }
    assert tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table, _ in before
    ) == before
    assert DiscoveryRepository(db).get_current_profile_version(
        original.subject_id
    ) == original


def test_profile_hash_preserves_order_and_rejects_non_tuple_inputs():
    first = canonical_profile_hash((), ("alpha", "beta"))
    second = canonical_profile_hash((), ("beta", "alpha"))

    assert first == _config_hash((), ("alpha", "beta"))
    assert first != second
    with pytest.raises(DomainError, match="exact tuple"):
        canonical_profile_hash([], ("alpha",))


def test_profile_reader_rejects_noncontiguous_stored_ordinals(db):
    bootstrap_reference_data(db)
    original = DiscoveryRepository(db).get_current_profile_version_by_subject_name(
        "千竈 鉄平"
    )
    db.execute("DROP TRIGGER discovery_search_terms_no_delete")
    db.execute(
        "DELETE FROM discovery_search_terms "
        "WHERE profile_version_id=? AND ordinal=1",
        (original.id,),
    )

    with pytest.raises(DomainError) as caught:
        DiscoveryRepository(db).get_profile_version(original.id)

    assert caught.value.code == "STORED_DISCOVERY_PROFILE_INVALID"


def test_profile_reader_rejects_a_current_version_owned_by_another_profile(db):
    bootstrap_reference_data(db)
    repository = DiscoveryRepository(db)
    original = repository.get_current_profile_version_by_subject_name("大川智宏")
    foreign = repository.get_current_profile_version_by_subject_name("木野内栄治")
    db.execute("DROP TRIGGER discovery_profiles_current_version_owner_update")
    db.execute(
        "UPDATE discovery_profiles SET current_version_id=? WHERE id=?",
        (foreign.id, original.profile_id),
    )

    with pytest.raises(DomainError) as caught:
        repository.get_current_profile_version(original.subject_id)

    assert caught.value.code == "STORED_DISCOVERY_PROFILE_INVALID"


def test_profile_audit_failure_rolls_back_version_children_pointer_and_event(db):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    bootstrap_reference_data(db)
    repository = DiscoveryRepository(db)
    original = repository.get_current_profile_version_by_subject_name("江守哲")
    before = tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "discovery_profile_versions",
            "discovery_seed_channels",
            "discovery_search_terms",
            "audit_events",
        )
    )
    db.execute(
        """
        CREATE TRIGGER synthetic_discovery_profile_audit_failure
        BEFORE INSERT ON audit_events
        BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_AUDIT_FAILURE'); END
        """
    )

    with pytest.raises(DomainError) as caught:
        DiscoveryProfileService(db, clock=lambda: FIXED_NOW).replace_version(
            ReplaceDiscoveryProfileVersion(
                subject_id=original.subject_id,
                seed_channel_ids=original.seed_channel_ids,
                search_terms=("江守哲", "江守 哲"),
                reason="verified expanded spelling set",
            )
        )

    assert caught.value.code == "DISCOVERY_PROFILE_STORAGE_FAILED"
    assert tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table, _ in before
    ) == before
    assert repository.get_current_profile_version(original.subject_id) == original


@pytest.mark.parametrize("reason", (r"C:\private\transcript.txt", "text_body"))
def test_profile_replacement_rejects_path_or_transcript_sentinel_before_write(
    db, reason
):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    bootstrap_reference_data(db)
    original = DiscoveryRepository(db).get_current_profile_version_by_subject_name(
        "大川智宏"
    )
    before = tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "discovery_profile_versions",
            "discovery_seed_channels",
            "discovery_search_terms",
            "audit_events",
        )
    )

    with pytest.raises(DomainError) as caught:
        DiscoveryProfileService(db, clock=lambda: FIXED_NOW).replace_version(
            ReplaceDiscoveryProfileVersion(
                subject_id=original.subject_id,
                seed_channel_ids=original.seed_channel_ids,
                search_terms=("大川智宏", "大川 智宏"),
                reason=reason,
            )
        )

    assert caught.value.code == "AUDIT_REASON_PRIVATE"
    assert tuple(
        (table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table, _ in before
    ) == before


def test_profile_replacement_rejects_a_retained_transcript_reason_before_write(db):
    from market_voice_forecast_ledger.services.discovery_profiles import (
        DiscoveryProfileService,
        ReplaceDiscoveryProfileVersion,
    )

    fixture = create_synthetic_collection_candidate(
        db,
        presence_state="presence_unverified",
        assignment_kind="hold",
        text_body="Private retained discovery transcript sentinel.",
    )
    repository = DiscoveryRepository(db)
    original = repository.get_current_profile_version(fixture.subject_id)
    before = db.execute(
        "SELECT COUNT(*) FROM discovery_profile_versions"
    ).fetchone()[0]

    with pytest.raises(DomainError) as caught:
        DiscoveryProfileService(db, clock=lambda: FIXED_NOW).replace_version(
            ReplaceDiscoveryProfileVersion(
                subject_id=fixture.subject_id,
                seed_channel_ids=(),
                search_terms=("Synthetic Replacement Term",),
                reason="Private retained discovery transcript sentinel.",
            )
        )

    assert caught.value.code == "AUDIT_REASON_PRIVATE"
    assert db.execute(
        "SELECT COUNT(*) FROM discovery_profile_versions"
    ).fetchone()[0] == before
    assert repository.get_current_profile_version(fixture.subject_id) == original
