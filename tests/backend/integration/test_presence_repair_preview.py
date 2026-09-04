import hashlib
import sqlite3
from contextlib import closing

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.services.presence_repair import PresenceRepairService
from market_voice_forecast_ledger.repositories.presence_repair import PresenceRepairRepository
from tests.backend.presence_repair_fakes import repair_environment
from tests.backend.integration.test_presence_pilot import NOW


def repair_service(env, **kwargs):
    return PresenceRepairService(
        env.conn, env.settings, clock=lambda: NOW,
        backup_root=env.settings.data_dir / "backups" / "repair-test",
        version_probe=env.probe, allowlists=env.allowlists, **kwargs,
    )


def persistent_files(settings):
    # SQLite's shared-memory read marks are volatile reader coordination, not
    # persisted application data. The database and WAL must remain identical.
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in settings.data_dir.rglob("*") if p.is_file() and not p.name.endswith("-shm")}


def test_preview_writes_nothing_and_backup_is_verified(repair_environment):
    env = repair_environment
    service = repair_service(env)
    before_changes = env.conn.total_changes
    before_files = persistent_files(env.settings)
    preview = service.preview()
    assert preview.target.counts["jobs"] == 20
    assert env.conn.total_changes == before_changes
    assert persistent_files(env.settings) == before_files

    assert service.preview() == preview
    backup = service._create_verified_database_backup(preview, env.settings.data_dir / "backups" / "snapshot.sqlite3")
    assert hashlib.sha256(backup.path.read_bytes()).hexdigest() == backup.database.snapshot_sha256
    with closing(sqlite3.connect(backup.path)) as conn:
        conn.row_factory = sqlite3.Row
        assert PresenceRepairRepository(conn).read_target() == preview.target
        assert tuple(row[0] for row in conn.execute("PRAGMA integrity_check")) == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert env.conn.total_changes == before_changes


@pytest.mark.parametrize("reason", ("collision", "changed"))
def test_backup_failure_leaves_live_database_unchanged(repair_environment, reason):
    env = repair_environment
    service = repair_service(env)
    preview = service.preview()
    destination = env.settings.data_dir / "backups" / "snapshot.sqlite3"
    if reason == "collision":
        destination.parent.mkdir()
        destination.write_bytes(b"keep-existing")
    else:
        env.conn.execute("UPDATE retention_settings SET retention_days=180 WHERE id=1")
    before = PresenceRepairRepository(env.conn).fingerprint_except(())
    with pytest.raises(DomainError) as caught:
        service._create_verified_database_backup(preview, destination)
    assert caught.value.code == "PRESENCE_REPAIR_BACKUP_FAILED"
    assert PresenceRepairRepository(env.conn).fingerprint_except(()) == before
    if reason == "collision":
        assert destination.read_bytes() == b"keep-existing"
