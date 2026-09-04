"""One-shot, hash-bound orchestration for the invalid historical pilot."""

import sqlite3
import json
import re
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from dataclasses import replace
from importlib import resources
from pathlib import Path

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.common import utc_iso
from market_voice_forecast_ledger.domain.presence_repair import (
    PresenceRepairPreview, PresenceRepairResult, build_presence_repair_preview,
)
from market_voice_forecast_ledger.domain.voice_verification import build_presence_job_manifest
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    SnapshotResult, create_database_snapshot, validate_database_snapshot,
)
from market_voice_forecast_ledger.repositories.presence_repair import PresenceRepairRepository
from market_voice_forecast_ledger.repositories.voice_verification import VoiceVerificationRepository
from market_voice_forecast_ledger.services.job_state import JobStateService
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists, VersionProbe, _private_file, _private_root, _require_no_reparse,
    attest_runtime, verify_runtime_startup,
)
from market_voice_forecast_ledger.voice.runtime_upgrade import LOCK_NAMES, backup_runtime_locks, upgrade_runtime_locks


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

    def apply(self, expected_preview_hash: str) -> PresenceRepairResult:
        if type(expected_preview_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_preview_hash) is None:
            raise _error("PRESENCE_REPAIR_PREVIEW_CHANGED")
        if self._conn.in_transaction:
            raise _error("PRESENCE_REPAIR_TARGET_INVALID")
        owns_transaction = False
        committed = False
        try:
            preview = self.preview()
            if preview.preview_hash != expected_preview_hash:
                raise _error("PRESENCE_REPAIR_PREVIEW_CHANGED")
            if self._validate_database() != repair_migration_names():
                raise _error("PRESENCE_REPAIR_TARGET_INVALID")
            completed_at = self._clock()
            backup_root = self._backup_root or (
                self._settings.data_dir / "backups" / ("presence-vad-repair-" + completed_at.strftime("%Y%m%dT%H%M%S%fZ"))
            )
            if backup_root.exists():
                raise _error("PRESENCE_REPAIR_BACKUP_FAILED")
            locks = backup_runtime_locks(
                self._settings, backup_directory=backup_root / "runtime-locks",
                version_probe=self._version_probe, allowlists=self._allowlists,
            )
            active = json.loads(locks.originals[-1][1])
            if any(
                (job.snapshot.model_name, job.snapshot.model_version, job.snapshot.adapter_version)
                != (active["model"]["name"], active["model"]["version"], active["adapter_contract_version"])
                for job in preview.target.jobs
            ):
                raise _error("PRESENCE_REPAIR_RUNTIME_INVALID")
            database = self._create_verified_database_backup(preview, backup_root / "database.sqlite3")
            self._fault_hook("after_backup")
            upgrade_runtime_locks(locks, version_probe=self._version_probe, allowlists=self._allowlists)
            try:
                validate_database_snapshot(database.path, database.database)
            except Exception:
                raise _error("PRESENCE_REPAIR_BACKUP_FAILED") from None
            self._conn.execute("BEGIN IMMEDIATE")
            owns_transaction = True
            if self.preview() != preview:
                raise _error("PRESENCE_REPAIR_PREVIEW_CHANGED")
            next_job_id = self._repo.next_job_id()
            jobs = JobStateService(self._conn, clock=self._clock)
            voice = VoiceVerificationRepository(self._conn)
            recreated = []
            with self._repo.authorize(preview.target.row_identities):
                self._repo.delete_target(preview.target, fault_hook=self._fault_hook)
                for ordinal, old in enumerate(preview.target.jobs):
                    snapshot = replace(old.snapshot, vad_contract_version="vad-v2")
                    job_id = jobs.create_video_pipeline_in_transaction(
                        build_presence_job_manifest(snapshot), (snapshot.candidate_id,), completed_at,
                        requested_job_id=next_job_id + ordinal,
                    )
                    voice.add_manifest(job_id, snapshot, created_at=completed_at)
                    recreated.append(job_id)
                    self._fault_hook(f"after_recreate:{ordinal + 1}")
                new_job_ids = tuple(recreated)
                self._repo.verify_replacement(preview.target, new_job_ids)
                self._fault_hook("before_ledger")
                self._repo.add_completion(
                    preview=preview, new_job_ids=new_job_ids,
                    database_backup_sha256=database.database.snapshot_sha256,
                    runtime_backup_fingerprint=locks.backup_fingerprint, completed_at=utc_iso(completed_at),
                )
                self._fault_hook("before_commit")
            self._conn.commit()
            owns_transaction = False
            committed = True
            self._fault_hook("after_commit")
            with closing(open_repair_readonly(self._settings.database_path)) as reopened:
                verifier = PresenceRepairService(reopened, self._settings, version_probe=self._version_probe)
                if verifier._validate_database() != repair_migration_names():
                    raise _error("PRESENCE_REPAIR_POSTVERIFY_FAILED")
                reopened.execute("BEGIN")
                try:
                    repository = PresenceRepairRepository(reopened)
                    repository.verify_replacement(preview.target, new_job_ids)
                    repository.verify_completion(preview, new_job_ids, database.database.snapshot_sha256, locks.backup_fingerprint)
                finally:
                    reopened.rollback()
            validate_database_snapshot(database.path, database.database)
            for name, original_body in locks.originals:
                attestation = attest_runtime(self._settings, version_probe=self._version_probe, allowlists=self._allowlists, lock_name=name)
                verify_runtime_startup(attestation, self._settings.data_dir)
                if attestation.vad_contract_version != "vad-v2" or json.loads((self._settings.voice_runtime_dir / name).read_bytes()) != dict(json.loads(original_body), vad_contract_version="vad-v2"):
                    raise _error("PRESENCE_REPAIR_POSTVERIFY_FAILED")
                if (locks.backup_directory / name).read_bytes() != original_body:
                    raise _error("PRESENCE_REPAIR_POSTVERIFY_FAILED")
            return PresenceRepairResult(tuple(job.job_id for job in preview.target.jobs), new_job_ids, tuple(job.candidate_id for job in preview.target.jobs), "vad-v2")
        except DomainError as error:
            if committed:
                raise _error("PRESENCE_REPAIR_POSTVERIFY_FAILED") from None
            if error.code.startswith("PRESENCE_REPAIR_"):
                raise
            raise _error("PRESENCE_REPAIR_APPLY_FAILED") from None
        except Exception:
            raise _error("PRESENCE_REPAIR_POSTVERIFY_FAILED" if committed else "PRESENCE_REPAIR_APPLY_FAILED") from None
        finally:
            if owns_transaction:
                self._conn.rollback()

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
