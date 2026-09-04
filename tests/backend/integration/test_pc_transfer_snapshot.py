import hashlib
import sqlite3
import struct
from importlib import resources
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    DatabaseSnapshotGuard,
    IMPORTANT_TABLES,
    create_database_snapshot,
    validate_database_snapshot,
)


NOW = "2026-08-29T00:00:00.000000Z"
FEATURE_BYTES = struct.pack("<4f", 0.25, -0.5, 0.75, 1.0)
FEATURE_HASH = hashlib.sha256(FEATURE_BYTES).hexdigest()


def repository_migration_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            entry.name
            for entry in resources.files(
                "market_voice_forecast_ledger.db.migrations"
            ).iterdir()
            if entry.name[:4].isdigit()
            and entry.name[4:5] == "_"
            and entry.name.endswith(".sql")
        )
    )


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    connection = open_database(path)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    return path


def seed_valid_reference_feature(path: Path) -> None:
    connection = open_database(path)
    try:
        connection.execute(
            "INSERT INTO analysis_subjects(canonical_name, is_active, created_at) "
            "VALUES ('Snapshot subject', 1, ?)",
            (NOW,),
        )
        subject_id = connection.execute(
            "SELECT id FROM analysis_subjects WHERE canonical_name='Snapshot subject'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO speaker_threshold_configs(
                version, model_name, model_version,
                subject_operator, subject_boundary,
                interviewer_operator, interviewer_boundary,
                created_at, is_active
            ) VALUES (
                'snapshot-threshold-v1', 'snapshot-model', '1.0',
                'gte', 0.7, 'lte', 0.2, ?, 1
            )
            """,
            (NOW,),
        )
        profile_id = connection.execute(
            """
            INSERT INTO voice_reference_profiles(
                subject_id, model_name, model_version, adapter_version,
                feature_hash, threshold_config_version, created_at, is_active
            ) VALUES (
                ?, 'snapshot-model', '1.0', 'snapshot-adapter-v1',
                ?, 'snapshot-threshold-v1', ?, 1
            )
            """,
            (subject_id, FEATURE_HASH, NOW),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO voice_reference_features(
                reference_profile_id, encoding_version, float_dtype,
                dimension, embedding_blob, feature_sha256, created_at
            ) VALUES (?, 'speaker-embedding-v1', 'float32', 4, ?, ?, ?)
            """,
            (profile_id, FEATURE_BYTES, FEATURE_HASH, NOW),
        )
    finally:
        connection.close()


def insert_job(path: Path, status: str) -> None:
    connection = open_database(path)
    try:
        connection.execute(
            """
            INSERT INTO jobs(
                job_kind, manifest_hash, total_units, status,
                created_at, updated_at
            ) VALUES ('youtube_sync', ?, 1, ?, ?, ?)
            """,
            (hashlib.sha256(status.encode()).hexdigest(), status, NOW, NOW),
        )
    finally:
        connection.close()


def seed_unsafe_state(path: Path, unsafe_state: str) -> None:
    if unsafe_state == "running_job":
        insert_job(path, "running")
        return
    connection = open_database(path)
    try:
        connection.execute(
            """
            INSERT INTO local_artifacts(
                kind, local_path, status, retry_count,
                safe_error_code, created_at, deleted_at
            ) VALUES ('audio', 'temp-audio/pending.wav', 'pending', 0,
                      NULL, ?, NULL)
            """,
            (NOW,),
        )
    finally:
        connection.close()


def assert_transfer_error(code: str, callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code == code


def test_snapshot_never_overwrites_a_destination_created_after_preflight(migrated_db, tmp_path, monkeypatch):
    seed_valid_reference_feature(migrated_db)
    destination = tmp_path / "new-backup" / "snapshot.sqlite3"
    original_mkdir = Path.mkdir

    def competing_mkdir(path, *args, **kwargs):
        original_mkdir(path, *args, **kwargs)
        if path == destination.parent:
            destination.write_bytes(b"preserve-concurrent-file")

    monkeypatch.setattr(Path, "mkdir", competing_mkdir)
    with pytest.raises(DomainError):
        create_database_snapshot(migrated_db, destination, repository_migration_names())
    assert destination.read_bytes() == b"preserve-concurrent-file"


def test_wal_database_is_restored_as_one_standalone_snapshot(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    writer = open_database(migrated_db)
    try:
        writer.execute(
            """
            INSERT INTO jobs(
                job_kind, manifest_hash, total_units, status,
                created_at, updated_at
            ) VALUES ('youtube_sync', ?, 1, 'succeeded', ?, ?)
            """,
            ("a" * 64, NOW, NOW),
        )
        wal_path = migrated_db.with_name(migrated_db.name + "-wal")
        assert wal_path.is_file()

        destination = tmp_path / "snapshot.sqlite3"
        result = create_database_snapshot(
            migrated_db,
            destination,
            repository_migration_names(),
        )
    finally:
        writer.close()

    assert result.path == destination
    assert result.database.integrity_check == "ok"
    assert result.database.migrations == repository_migration_names()
    assert tuple(name for name, _ in result.database.table_counts) == tuple(
        sorted(IMPORTANT_TABLES)
    )
    assert dict(result.database.table_counts)["jobs"] == 1
    assert result.database.reference_feature_count == 1
    assert result.database.active_artifact_count == 0
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == (
        result.database.snapshot_sha256
    )
    assert not destination.with_name(destination.name + "-wal").exists()
    assert not destination.with_name(destination.name + "-shm").exists()
    assert not destination.with_name(destination.name + "-journal").exists()
    validate_database_snapshot(destination, result.database)


@pytest.mark.parametrize("unsafe_state", ("running_job", "active_artifact"))
def test_snapshot_rejects_non_quiescent_source(
    migrated_db: Path,
    tmp_path: Path,
    unsafe_state: str,
) -> None:
    seed_valid_reference_feature(migrated_db)
    seed_unsafe_state(migrated_db, unsafe_state)

    destination = tmp_path / "snapshot.sqlite3"
    assert_transfer_error(
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
        lambda: create_database_snapshot(
            migrated_db,
            destination,
            repository_migration_names(),
        ),
    )
    assert not destination.exists()


def test_snapshot_rejects_corrupt_reference_feature(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    connection = open_database(migrated_db)
    try:
        connection.execute("DROP TRIGGER voice_reference_features_no_update")
        connection.execute(
            "UPDATE voice_reference_features SET embedding_blob=?",
            (b"corrupt",),
        )
    finally:
        connection.close()

    assert_transfer_error(
        "PC_TRANSFER_DATABASE_INVALID",
        lambda: create_database_snapshot(
            migrated_db,
            tmp_path / "snapshot.sqlite3",
            repository_migration_names(),
        ),
    )


def test_snapshot_rejects_source_commit_during_backup(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    changed = False

    def mutate_once(status: int, remaining: int, total: int) -> None:
        del status, remaining, total
        nonlocal changed
        if changed:
            return
        changed = True
        insert_job(migrated_db, "succeeded")

    destination = tmp_path / "snapshot.sqlite3"
    assert_transfer_error(
        "PC_TRANSFER_SOURCE_CHANGED",
        lambda: create_database_snapshot(
            migrated_db,
            destination,
            repository_migration_names(),
            backup_progress=mutate_once,
        ),
    )
    assert changed is True
    assert not destination.exists()


def test_snapshot_rejects_migration_identity_mismatch(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)

    assert_transfer_error(
        "PC_TRANSFER_DATABASE_INVALID",
        lambda: create_database_snapshot(
            migrated_db,
            tmp_path / "snapshot.sqlite3",
            repository_migration_names()[:-1],
        ),
    )


def test_snapshot_guard_detects_change_after_snapshot(
    migrated_db: Path,
    tmp_path: Path,
) -> None:
    seed_valid_reference_feature(migrated_db)
    destination = tmp_path / "snapshot.sqlite3"

    with DatabaseSnapshotGuard(
        migrated_db,
        repository_migration_names(),
    ) as guard:
        guard.create_snapshot(destination)
        insert_job(migrated_db, "succeeded")
        assert_transfer_error("PC_TRANSFER_SOURCE_CHANGED", guard.verify_unchanged)
