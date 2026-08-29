"""Create and validate a closed, standalone SQLite transfer snapshot."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.manifest import DatabaseSummary


IMPORTANT_TABLES = (
    "analysis_subjects",
    "videos",
    "subject_video_candidates",
    "presence_decisions",
    "jobs",
    "job_units",
    "job_unit_attempts",
    "job_events",
    "video_pipeline_job_binding_sets",
    "video_pipeline_job_bindings",
    "voice_reference_profiles",
    "voice_reference_clips",
    "voice_reference_features",
    "voice_reference_calibrations",
    "voice_verification_manifests",
    "voice_verification_runs",
    "voice_verification_segments",
    "voice_verification_reviews",
    "local_artifacts",
)
ACTIVE_JOB_STATES = ("running", "pause_requested", "cancel_requested")
_SIDE_CAR_SUFFIXES = ("-wal", "-shm", "-journal")
_COPY_BLOCK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    path: Path
    database: DatabaseSummary


def _database_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_DATABASE_INVALID",
        "transfer database is invalid",
    )


def _quiescence_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_SOURCE_NOT_QUIESCENT",
        "transfer source is not quiescent",
    )


def _source_changed_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_SOURCE_CHANGED",
        "transfer source changed during snapshot",
    )


def _strict_scalar_count(
    connection: sqlite3.Connection,
    table: str,
) -> int:
    if table not in IMPORTANT_TABLES:
        raise _database_error()
    quoted = '"' + table.replace('"', '""') + '"'
    row = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        raise _database_error()
    return row[0]


def _inspect_connection(
    connection: sqlite3.Connection,
    expected_migrations: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...], int, int]:
    integrity_rows = tuple(
        row[0] for row in connection.execute("PRAGMA integrity_check")
    )
    if integrity_rows != ("ok",):
        raise _database_error()
    stored_migrations = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        )
    )
    if not all(type(name) is str for name in stored_migrations):
        raise _database_error()
    migrations = tuple(f"{name}.sql" for name in stored_migrations)
    if migrations != expected_migrations:
        raise _database_error()
    table_counts = tuple(
        (table, _strict_scalar_count(connection, table))
        for table in sorted(IMPORTANT_TABLES)
    )
    active_jobs_row = connection.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE status IN ('running', 'pause_requested', 'cancel_requested')"
    ).fetchone()
    active_artifacts_row = connection.execute(
        "SELECT COUNT(*) FROM local_artifacts "
        "WHERE status != 'deleted' OR deleted_at IS NULL"
    ).fetchone()
    if (
        active_jobs_row is None
        or len(active_jobs_row) != 1
        or type(active_jobs_row[0]) is not int
        or active_jobs_row[0] < 0
        or active_artifacts_row is None
        or len(active_artifacts_row) != 1
        or type(active_artifacts_row[0]) is not int
        or active_artifacts_row[0] < 0
    ):
        raise _database_error()
    feature_rows = tuple(
        connection.execute(
            "SELECT embedding_blob, feature_sha256 "
            "FROM voice_reference_features ORDER BY id"
        )
    )
    if not feature_rows:
        raise _database_error()
    for embedding_blob, feature_sha256 in feature_rows:
        if (
            type(embedding_blob) is not bytes
            or type(feature_sha256) is not str
            or hashlib.sha256(embedding_blob).hexdigest() != feature_sha256
        ):
            raise _database_error()
    active_jobs = active_jobs_row[0]
    active_artifacts = active_artifacts_row[0]
    if active_jobs or active_artifacts:
        raise _quiescence_error()
    return migrations, table_counts, len(feature_rows), active_artifacts


def _read_only_uri(path: Path) -> str:
    return path.resolve(strict=True).as_uri() + "?mode=ro"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(_COPY_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _side_car_paths(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in _SIDE_CAR_SUFFIXES)


def _summarize_closed_snapshot(
    path: Path,
    expected_migrations: tuple[str, ...],
) -> DatabaseSummary:
    uri = _read_only_uri(path)
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = None
        connection.execute("PRAGMA query_only=ON")
        migrations, table_counts, feature_count, active_artifacts = (
            _inspect_connection(connection, expected_migrations)
        )
    if any(side_car.exists() for side_car in _side_car_paths(path)):
        raise _database_error()
    return DatabaseSummary(
        integrity_check="ok",
        migrations=migrations,
        table_counts=table_counts,
        reference_feature_count=feature_count,
        active_artifact_count=active_artifacts,
        snapshot_sha256=_hash_file(path),
    )


def _remove_incomplete_snapshot(path: Path) -> None:
    for candidate in (path, *_side_car_paths(path)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def create_database_snapshot(
    source: Path,
    destination: Path,
    expected_migrations: tuple[str, ...],
    backup_progress: Callable[[int, int, int], None] | None = None,
) -> SnapshotResult:
    if (
        type(expected_migrations) is not tuple
        or not expected_migrations
        or expected_migrations != tuple(sorted(set(expected_migrations)))
        or destination.exists()
        or destination.is_symlink()
        or not source.is_file()
    ):
        raise _database_error()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_uri = _read_only_uri(source)
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            source_connection.row_factory = None
            source_connection.execute("PRAGMA query_only=ON")
            before_version_row = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()
            if (
                before_version_row is None
                or len(before_version_row) != 1
                or type(before_version_row[0]) is not int
            ):
                raise _database_error()
            before_version = before_version_row[0]
            before = _inspect_connection(source_connection, expected_migrations)
            with closing(sqlite3.connect(destination)) as snapshot_connection:
                source_connection.backup(
                    snapshot_connection,
                    pages=128,
                    progress=backup_progress,
                    sleep=0.0,
                )
                journal_mode = snapshot_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
                if journal_mode != ("delete",):
                    raise _database_error()
            after_version_row = source_connection.execute(
                "PRAGMA data_version"
            ).fetchone()
            if (
                after_version_row is None
                or len(after_version_row) != 1
                or type(after_version_row[0]) is not int
            ):
                raise _database_error()
            after = _inspect_connection(source_connection, expected_migrations)
        if before_version != after_version_row[0] or before != after:
            raise _source_changed_error()
        database = _summarize_closed_snapshot(
            destination,
            expected_migrations,
        )
        return SnapshotResult(path=destination, database=database)
    except DomainError:
        _remove_incomplete_snapshot(destination)
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        _remove_incomplete_snapshot(destination)
        raise _database_error() from exc


def validate_database_snapshot(
    path: Path,
    expected: DatabaseSummary,
) -> None:
    try:
        if type(expected) is not DatabaseSummary or not path.is_file():
            raise _database_error()
        actual = _summarize_closed_snapshot(path, expected.migrations)
        if actual != expected:
            raise _database_error()
    except DomainError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise _database_error() from exc
