import sqlite3
from datetime import datetime, timezone
from importlib import resources

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError


COLLECTION_CUTOVER_MIGRATION = "0018_youtube_discovery_cutover"
_ALLOWED_CUTOVER_SEEDS = {
    "retention_settings": ((1, 365),),
}


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for character in script:
        statement += character
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def _schema_migrations_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is not None


def _has_noninternal_schema_objects(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def _read_applied_migrations(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        row["name"] for row in conn.execute("SELECT name FROM schema_migrations")
    )


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "name TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL"
        ")"
    )


def _assert_collection_cutover_source_is_empty(
    conn: sqlite3.Connection,
) -> None:
    table_names = tuple(
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name!='schema_migrations' ORDER BY name"
        )
    )
    for table_name in table_names:
        quoted = '"' + table_name.replace('"', '""') + '"'
        rows = tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {quoted}"))
        expected = _ALLOWED_CUTOVER_SEEDS.get(table_name, ())
        if rows != expected:
            raise DomainError(
                "COLLECTION_MODEL_RESET_REQUIRED",
                "COLLECTION_MODEL_RESET_REQUIRED: the local database must be archived "
                "and recreated for the collection model",
            )


def apply_migrations(conn: sqlite3.Connection) -> tuple[str, ...]:
    had_ledger = _schema_migrations_exists(conn)
    preexisting = _read_applied_migrations(conn) if had_ledger else frozenset()
    if not had_ledger and _has_noninternal_schema_objects(conn):
        raise DomainError(
            "COLLECTION_MODEL_RESET_REQUIRED",
            "COLLECTION_MODEL_RESET_REQUIRED: the local database must be archived and "
            "recreated for the collection model",
        )
    if had_ledger and COLLECTION_CUTOVER_MIGRATION not in preexisting:
        raise DomainError(
            "COLLECTION_MODEL_RESET_REQUIRED",
            "COLLECTION_MODEL_RESET_REQUIRED: the local database must be archived and "
            "recreated for the collection model",
        )

    _ensure_schema_migrations(conn)
    applied = set(preexisting)
    migration_files = sorted(
        (
            resource
            for resource in resources.files(
                "market_voice_forecast_ledger.db.migrations"
            ).iterdir()
            if resource.name[:4].isdigit()
            and resource.name[4:5] == "_"
            and resource.name.endswith(".sql")
        ),
        key=lambda resource: resource.name,
    )
    newly_applied: list[str] = []
    for migration_file in migration_files:
        migration_name = migration_file.name.removesuffix(".sql")
        if migration_name in applied:
            continue
        with transaction(conn):
            if migration_name == COLLECTION_CUTOVER_MIGRATION:
                _assert_collection_cutover_source_is_empty(conn)
            _execute_script(conn, migration_file.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration_name, utc_iso(datetime.now(timezone.utc))),
            )
        newly_applied.append(migration_name)
    return tuple(newly_applied)
