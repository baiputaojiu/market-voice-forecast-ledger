import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.enums import (
    ConfigurationStatus,
    PolicyKind,
    SubjectKind,
)
from market_voice_forecast_ledger.repositories.sources import SourceRepository


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "ledger.sqlite3")
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_confirmed_channel_ids_are_seeded(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    assert (
        repo.get_policy_by_subject_name("江守哲").youtube_channel_id
        == "UCVXka7buS_WptsAzSE0LcKg"
    )
    assert (
        repo.get_policy_by_subject_name("暁投資顧問").youtube_channel_id
        == "UCOfzLmXpI3qmZfV7_Cs1sYA"
    )
    assert (
        repo.get_policy_by_subject_name("木野内栄治").policy_kind
        is PolicyKind.ALL_CHANNELS
    )
    assert (
        repo.get_policy_by_subject_name("大川智宏").policy_kind
        is PolicyKind.ALL_CHANNELS
    )


def test_reference_subjects_kinds_and_aliases_are_seeded_exactly(db):
    bootstrap_reference_data(db)

    subjects = {
        row["canonical_name"]: (row["subject_kind"], row["is_active"])
        for row in db.execute(
            "SELECT canonical_name, subject_kind, is_active FROM analysis_subjects"
        )
    }
    aliases = {
        (row["canonical_name"], row["alias"])
        for row in db.execute(
            """
            SELECT subject.canonical_name, alias.alias
            FROM subject_aliases AS alias
            JOIN analysis_subjects AS subject ON subject.id = alias.subject_id
            """
        )
    }

    assert subjects == {
        "木野内栄治": (SubjectKind.PERSON.value, 1),
        "暁投資顧問": (SubjectKind.ORGANIZATION.value, 1),
        "江守哲": (SubjectKind.PERSON.value, 1),
        "大川智宏": (SubjectKind.PERSON.value, 1),
    }
    assert aliases == {("木野内栄治", "木野内英二"), ("大川智宏", "大川智ひろ")}


def test_bootstrap_is_idempotent_and_aliases_resolve_to_the_same_policy(db):
    bootstrap_reference_data(db)
    bootstrap_reference_data(db)
    repo = SourceRepository(db)

    counts = {
        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "analysis_subjects",
            "subject_aliases",
            "subject_channel_policies",
        )
    }
    assert counts == {
        "analysis_subjects": 4,
        "subject_aliases": 2,
        "subject_channel_policies": 4,
    }
    assert (
        repo.get_policy_by_subject_name("木野内英二").id
        == repo.get_policy_by_subject_name("木野内栄治").id
    )
    assert (
        repo.get_policy_by_subject_name("大川智ひろ").id
        == repo.get_policy_by_subject_name("大川智宏").id
    )


def test_bootstrap_does_not_overwrite_a_user_modified_policy(db):
    bootstrap_reference_data(db)
    repo = SourceRepository(db)
    original = repo.get_policy_by_subject_name("江守哲")
    user_hash = "user-modified-policy-hash"
    db.execute(
        """
        UPDATE subject_channel_policies
        SET configuration_status = ?, youtube_channel_id = ?,
            channel_display_name = ?, policy_hash = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            ConfigurationStatus.CONFIGURED.value,
            "UC1234567890123456789012",
            "User Selected Channel",
            user_hash,
            "2026-08-15T12:34:56.000000Z",
            original.id,
        ),
    )

    bootstrap_reference_data(db)

    modified = repo.get_policy_by_subject_name("江守哲")
    assert modified.youtube_channel_id == "UC1234567890123456789012"
    assert modified.channel_display_name == "User Selected Channel"
    assert modified.policy_hash == user_hash
    assert modified.updated_at.isoformat() == "2026-08-15T12:34:56+00:00"
