"""One-shot, hash-bound orchestration for the invalid historical pilot."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.presence_repair import (
    PresenceRepairPreview, build_presence_repair_preview,
)
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    SnapshotResult, create_database_snapshot, validate_database_snapshot,
)
from market_voice_forecast_ledger.repositories.presence_repair import PresenceRepairRepository
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists, VersionProbe, _private_file, _private_root, _require_no_reparse,
)


def _error(code: str) -> DomainError:
    return DomainError(code, "presence repair could not be verified")


def repair_migration_names() -> tuple[str, ...]:
    return tuple(sorted(
        item.name for item in resources.files("market_voice_forecast_ledger.db.migrations").iterdir()
        if item.name[:4].isdigit() and item.name[4:5] == "_" and item.name.endswith(".sql")
    ))


def open_repair_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class PresenceRepairService:
    def __init__(
        self, conn: sqlite3.Connection, settings: Settings, *,
        clock: Callable[[], datetime] | None = None, backup_root: Path | None = None,
        version_probe: VersionProbe, allowlists: RuntimeAllowlists = RuntimeAllowlists(),
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._backup_root = backup_root
        self._version_probe = version_probe
        self._allowlists = allowlists
        self._fault_hook = fault_hook or (lambda _stage: None)
        self._repo = PresenceRepairRepository(conn)

    def _validate_database(self) -> tuple[str, ...]:
        root = _private_root(self._settings.data_dir)
        expected_path = _private_file(self._settings.database_path, root)
        # Canonical sorting may initialize SQLite's own temporary database.
        # It is not an attached application database and has no live identity.
        databases = tuple(row for row in self._conn.execute("PRAGMA database_list") if row[1] != "temp")
        if len(databases) != 1 or databases[0][1] != "main" or Path(databases[0][2]).resolve(strict=True) != expected_path:
            raise _error("PRESENCE_REPAIR_TARGET_INVALID")
        migrations = tuple(row[0] + ".sql" for row in self._conn.execute("SELECT name FROM schema_migrations ORDER BY name"))
        expected = repair_migration_names()
        if migrations not in (expected, expected[:-1]) or expected[-1] != "0021_presence_vad_repair.sql":
            raise _error("PRESENCE_REPAIR_TARGET_INVALID")
        if tuple(row[0] for row in self._conn.execute("PRAGMA integrity_check")) != ("ok",) or self._conn.execute("PRAGMA foreign_key_check").fetchall():
            raise _error("PRESENCE_REPAIR_TARGET_INVALID")
        return migrations

    def preview(self) -> PresenceRepairPreview:
        try:
            self._validate_database()
            return build_presence_repair_preview("vad-v1", "vad-v2", self._repo.read_target())
        except DomainError:
            raise
        except Exception:
            raise _error("PRESENCE_REPAIR_TARGET_INVALID") from None

    def _create_verified_database_backup(self, preview: PresenceRepairPreview, backup_path: Path) -> SnapshotResult:
        try:
            if self._conn.in_transaction:
                raise ValueError("snapshot requires a closed write transaction")
            migrations = self._validate_database()
            root = _private_root(self._settings.data_dir)
            path = backup_path.absolute()
            _require_no_reparse(path)
            relative = path.relative_to(root)
            if not relative.parts or ".." in relative.parts or path.exists():
                raise ValueError("invalid backup destination")
            if self.preview() != preview:
                raise ValueError("stale backup target")
            result = create_database_snapshot(self._settings.database_path, path, migrations)
            validate_database_snapshot(path, result.database)
            with closing(open_repair_readonly(path)) as backup_conn:
                target = PresenceRepairRepository(backup_conn).read_target()
                if build_presence_repair_preview("vad-v1", "vad-v2", target) != preview:
                    raise ValueError("backup target changed")
                if backup_conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("invalid backup references")
            validate_database_snapshot(path, result.database)
            if self.preview() != preview:
                raise ValueError("source changed after backup")
            return result
        except Exception:
            raise _error("PRESENCE_REPAIR_BACKUP_FAILED") from None
