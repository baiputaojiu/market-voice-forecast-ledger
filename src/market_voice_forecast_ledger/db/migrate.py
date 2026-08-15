import sqlite3
from datetime import datetime, timezone
from importlib import resources

from market_voice_forecast_ledger.db.connection import transaction
from market_voice_forecast_ledger.domain.common import utc_iso


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for character in script:
        statement += character
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def apply_migrations(conn: sqlite3.Connection) -> tuple[str, ...]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "name TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL"
        ")"
    )
    applied = {
        row["name"] for row in conn.execute("SELECT name FROM schema_migrations")
    }
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
            _execute_script(conn, migration_file.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration_name, utc_iso(datetime.now(timezone.utc))),
            )
        newly_applied.append(migration_name)
    return tuple(newly_applied)
