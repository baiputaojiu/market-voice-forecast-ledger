"""Read and fingerprint the exact repair set; ordinary writers stay unchanged."""

import hashlib
import sqlite3
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from market_voice_forecast_ledger.domain.common import canonical_json, sha256_text, utc_iso
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.presence_repair import (
    PresenceRepairJob,
    PresenceRepairTarget,
    PresenceRepairPreview,
    RepairRowIdentity,
)
from market_voice_forecast_ledger.domain.voice_verification import PRESENCE_UNITS
from market_voice_forecast_ledger.repositories.discovery import DiscoveryRepository
from market_voice_forecast_ledger.repositories.retention import RetentionRepository
from market_voice_forecast_ledger.repositories.voice_verification import VoiceVerificationRepository


REPAIR_COUNTS = {
    "jobs": 20, "job_units": 140, "job_unit_attempts": 140, "job_events": 340,
    "video_pipeline_job_binding_sets": 20, "video_pipeline_job_bindings": 20,
    "voice_verification_manifests": 20, "voice_verification_runs": 20,
    "voice_verification_segments": 20, "voice_verification_reviews": 0,
    "local_artifacts": 60,
}
_IDENTITY_COLUMNS = {
    "job_units": ("job_id", "unit_key"),
    "video_pipeline_job_bindings": ("job_id", "candidate_id"),
    "video_pipeline_job_binding_sets": ("job_id",),
    **{name: ("id",) for name in REPAIR_COUNTS if name not in {
        "job_units", "video_pipeline_job_bindings", "video_pipeline_job_binding_sets"
    }},
}
_DELETE_SQL = {
    "voice_verification_reviews": "DELETE FROM voice_verification_reviews WHERE id=?",
    "voice_verification_segments": "DELETE FROM voice_verification_segments WHERE id=?",
    "voice_verification_runs": "DELETE FROM voice_verification_runs WHERE id=?",
    "voice_verification_manifests": "DELETE FROM voice_verification_manifests WHERE id=?",
    "job_events": "DELETE FROM job_events WHERE id=?",
    "job_unit_attempts": "DELETE FROM job_unit_attempts WHERE id=?",
    "video_pipeline_job_bindings": "DELETE FROM video_pipeline_job_bindings WHERE job_id=? AND candidate_id=?",
    "video_pipeline_job_binding_sets": "DELETE FROM video_pipeline_job_binding_sets WHERE job_id=?",
    "job_units": "DELETE FROM job_units WHERE job_id=? AND unit_key=?",
    "local_artifacts": "DELETE FROM local_artifacts WHERE id=?",
    "jobs": "DELETE FROM jobs WHERE id=?",
}


def _invalid() -> DomainError:
    return DomainError("PRESENCE_REPAIR_TARGET_INVALID", "presence repair target is invalid")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _identity(table: str, row: dict) -> RepairRowIdentity:
    return RepairRowIdentity(table, ":".join(str(row[key]) for key in _IDENTITY_COLUMNS[table]))


def _row_hash(row: dict) -> str:
    values = {}
    for key, value in row.items():
        if type(value) is bytes:
            body = (len(value), hashlib.sha256(value).hexdigest())
        elif type(value) is float:
            body = value.hex()
        else:
            body = value
        values[key] = (type(value).__name__, body)
    return sha256_text(canonical_json(values))


def _fingerprint(rows: dict[str, tuple[dict, ...]]) -> str:
    return sha256_text(canonical_json({name: [_row_hash(row) for row in values] for name, values in rows.items()}))


class PresenceRepairRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._authorizing = False

    def next_job_id(self) -> int:
        if not self._conn.in_transaction:
            raise _invalid()
        return self._conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM jobs").fetchone()[0]

    @contextmanager
    def authorize(self, rows: tuple[RepairRowIdentity, ...]):
        if not self._conn.in_transaction or self._authorizing or len(rows) != len(set(rows)):
            raise _invalid()
        for row in rows:
            if row.table not in _DELETE_SQL or type(row.identity) is not str:
                raise _invalid()
            values = row.identity.split(":", len(_IDENTITY_COLUMNS[row.table]) - 1)
            if len(values) != len(_IDENTITY_COLUMNS[row.table]) or re.fullmatch(r"[1-9][0-9]*", values[0]) is None:
                raise _invalid()
            if row.table == "job_units" and values[1] not in {key for key, _ in PRESENCE_UNITS}:
                raise _invalid()
            if row.table == "video_pipeline_job_bindings" and re.fullmatch(r"[1-9][0-9]*", values[1]) is None:
                raise _invalid()
        remaining = set(rows)

        def allow_once(table, identity):
            row = RepairRowIdentity(table, identity)
            if not self._conn.in_transaction or row not in remaining:
                return 0
            remaining.remove(row)
            return 1

        def revoke_at_transaction_end(action, first, _second, _database, _source):
            if action == sqlite3.SQLITE_TRANSACTION and first in {"COMMIT", "ROLLBACK"}:
                remaining.clear()
            return sqlite3.SQLITE_OK

        self._authorizing = True
        try:
            self._conn.set_authorizer(revoke_at_transaction_end)
            self._conn.create_function("presence_vad_repair_delete_authorized", 2, allow_once)
            yield
        finally:
            remaining.clear()
            self._conn.create_function("presence_vad_repair_delete_authorized", 2, lambda *_: 0)
            self._conn.set_authorizer(None)
            self._authorizing = False

    def delete_target(self, target: PresenceRepairTarget, *, fault_hook: Callable[[str], None]) -> None:
        if not self._conn.in_transaction or not self._authorizing or dict(target.counts) != REPAIR_COUNTS:
            raise _invalid()
        for table, sql in _DELETE_SQL.items():
            identities = tuple(row for row in target.row_identities if row.table == table)
            if len(identities) != target.counts[table]:
                raise _invalid()
            for row in identities:
                values = row.identity.split(":", len(_IDENTITY_COLUMNS[table]) - 1)
                if self._conn.execute(sql, values).rowcount != 1:
                    raise _invalid()
            fault_hook("after_delete:" + table)

    def verify_replacement(self, target: PresenceRepairTarget, new_job_ids: tuple[int, ...]) -> None:
        old_ids = {job.job_id for job in target.jobs}
        if len(new_job_ids) != 20 or len(set(new_job_ids)) != 20 or old_ids.intersection(new_job_ids):
            raise _invalid()
        rows = self._all_rows()
        if any(row["id"] in old_ids for row in rows["jobs"]) or any(row["vad_contract_version"] == "vad-v1" for row in rows["voice_verification_manifests"]):
            raise _invalid()
        voice = VoiceVerificationRepository(self._conn)
        identities = []
        expected_counts = {name: 0 for name in REPAIR_COUNTS}
        expected_counts.update(jobs=20, job_units=140, job_events=20, video_pipeline_job_binding_sets=20, video_pipeline_job_bindings=20, voice_verification_manifests=20)
        for table, count in expected_counts.items():
            owned = tuple(row for row in rows[table] if (
                row["id"] in new_job_ids if table == "jobs" else row.get("job_id") in new_job_ids
            ))
            if len(owned) != count:
                raise _invalid()
            identities.extend(_identity(table, row) for row in owned)
        for old, job_id in zip(target.jobs, new_job_ids, strict=True):
            artifacts = voice.require_job_artifacts(job_id)
            job = self._one(rows["jobs"], "id", job_id)
            if artifacts.manifest.snapshot != replace(old.snapshot, vad_contract_version="vad-v2") or artifacts.run is not None or job["status"] != "queued" or job["source_job_id"] is not None:
                raise _invalid()
            units = tuple(row for row in rows["job_units"] if row["job_id"] == job_id)
            if any(row["status"] != "pending" or row["attempt_count"] != 0 for row in units):
                raise _invalid()
            event = self._one(rows["job_events"], "job_id", job_id)
            if event["unit_key"] is not None or event["event_kind"] != "job_created" or event["metadata_json"] != canonical_json({"source_job_id": None}) or event["created_at"] != job["created_at"]:
                raise _invalid()
        if self.fingerprint_except(tuple(identities), all_rows=rows) != target.preserved_fingerprint:
            raise _invalid()

    def add_completion(self, *, preview: PresenceRepairPreview, new_job_ids: tuple[int, ...], database_backup_sha256: str, runtime_backup_fingerprint: str, completed_at: str) -> None:
        if not self._conn.in_transaction:
            raise _invalid()
        target = preview.target
        self._conn.execute(
            "INSERT INTO voice_vad_repairs(schema_version, from_vad_contract_version, to_vad_contract_version, "
            "preview_hash, target_fingerprint, preserved_fingerprint, candidate_order_hash, database_backup_sha256, "
            "runtime_backup_fingerprint, deleted_counts_json, candidate_ids_json, old_job_ids_json, new_job_ids_json, completed_at) "
            "VALUES ('presence-vad-repair.v1', 'vad-v1', 'vad-v2', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (preview.preview_hash, target.target_fingerprint, target.preserved_fingerprint, target.candidate_order_hash,
             database_backup_sha256, runtime_backup_fingerprint, canonical_json(dict(target.counts)),
             canonical_json([job.candidate_id for job in target.jobs]), canonical_json([job.job_id for job in target.jobs]),
             canonical_json(list(new_job_ids)), completed_at),
        )

    def verify_completion(self, preview: PresenceRepairPreview, new_job_ids: tuple[int, ...], database_backup_sha256: str, runtime_backup_fingerprint: str) -> None:
        rows = self._conn.execute("SELECT * FROM voice_vad_repairs").fetchall()
        expected = {
            "schema_version": "presence-vad-repair.v1", "from_vad_contract_version": "vad-v1", "to_vad_contract_version": "vad-v2",
            "preview_hash": preview.preview_hash, "target_fingerprint": preview.target.target_fingerprint,
            "preserved_fingerprint": preview.target.preserved_fingerprint, "candidate_order_hash": preview.target.candidate_order_hash,
            "database_backup_sha256": database_backup_sha256, "runtime_backup_fingerprint": runtime_backup_fingerprint,
            "deleted_counts_json": canonical_json(dict(preview.target.counts)),
            "candidate_ids_json": canonical_json([job.candidate_id for job in preview.target.jobs]),
            "old_job_ids_json": canonical_json([job.job_id for job in preview.target.jobs]), "new_job_ids_json": canonical_json(list(new_job_ids)),
        }
        if len(rows) != 1 or any(rows[0][key] != value for key, value in expected.items()):
            raise _invalid()

    def read_target(self, from_contract: str = "vad-v1", to_contract: str = "vad-v2") -> PresenceRepairTarget:
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN")
        try:
            if (from_contract, to_contract) != ("vad-v1", "vad-v2"):
                raise _invalid()
            return self._read_target()
        except DomainError as error:
            if error.code == "PRESENCE_REPAIR_ALREADY_APPLIED":
                raise
            raise _invalid() from None
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OSError):
            raise _invalid() from None
        finally:
            if owns_transaction:
                self._conn.rollback()

    def _read_target(self) -> PresenceRepairTarget:
        if self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='voice_vad_repairs'"
        ).fetchone() is not None and self._conn.execute("SELECT 1 FROM voice_vad_repairs LIMIT 1").fetchone() is not None:
            raise DomainError("PRESENCE_REPAIR_ALREADY_APPLIED", "presence repair was already applied")
        if self._conn.execute("PRAGMA foreign_key_check").fetchall():
            raise _invalid()
        all_rows = self._all_rows()
        manifests = tuple(row for row in all_rows["voice_verification_manifests"] if row["vad_contract_version"] == "vad-v1")
        manifests = tuple(sorted(manifests, key=lambda row: row["job_id"]))
        if len(manifests) != 20 or len({row["candidate_id"] for row in manifests}) != 20:
            raise _invalid()
        voice = VoiceVerificationRepository(self._conn)
        retention = RetentionRepository(self._conn)
        jobs = []
        selected: dict[str, list[dict]] = {name: [] for name in REPAIR_COUNTS}
        for row in manifests:
            job_id = row["job_id"]
            artifacts = voice.require_job_artifacts(job_id)
            snapshot = artifacts.manifest.snapshot
            job_row = self._one(all_rows["jobs"], "id", job_id)
            candidate = self._one(all_rows["subject_video_candidates"], "id", snapshot.candidate_id)
            decision = DiscoveryRepository(self._conn).get_presence_decision(candidate["current_presence_decision_id"])
            if (
                job_row["source_job_id"] is not None or job_row["status"] != "succeeded"
                or artifacts.run is None or len(artifacts.run.segments) != 1
                or not artifacts.reference.is_active
                or snapshot.presence_decision_id != candidate["current_presence_decision_id"]
                or decision.state.value != "presence_unverified"
                or snapshot.selection_contract_version != "presence-pilot-selection-v1"
            ):
                raise _invalid()
            selected["jobs"].append(job_row)
            for table in ("job_units", "job_unit_attempts", "job_events", "video_pipeline_job_binding_sets", "video_pipeline_job_bindings", "voice_verification_manifests", "voice_verification_runs"):
                selected[table].extend(row for row in all_rows[table] if row["job_id"] == job_id)
            run_id = artifacts.run.id
            for table in ("voice_verification_segments", "voice_verification_reviews"):
                selected[table].extend(row for row in all_rows[table] if row["run_id"] == run_id)
            units = tuple(sorted((row for row in all_rows["job_units"] if row["job_id"] == job_id), key=lambda row: row["ordinal"]))
            attempts = tuple(row for row in all_rows["job_unit_attempts"] if row["job_id"] == job_id)
            if len(units) != 7 or len(attempts) != 7 or any(row["status"] != "success" or row["attempt_count"] != 1 for row in units):
                raise _invalid()
            if any(row["attempt_no"] != 1 or row["result_status"] != "success" for row in attempts):
                raise _invalid()
            events = tuple(row for row in all_rows["job_events"] if row["job_id"] == job_id)
            self._validate_events(job_row, units, events)
            cleanup = units[-1]
            cleanup_artifacts = retention.require_presence_cleanup_receipt(
                job_id, manifest_hash=artifacts.manifest.manifest_hash,
                expected_external_input_hash=cleanup["external_input_hash"],
                expected_output_hash=cleanup["output_hash"],
            )
            for artifact in cleanup_artifacts:
                selected["local_artifacts"].append(self._one(all_rows["local_artifacts"], "id", artifact.id))
            jobs.append(PresenceRepairJob(job_id, snapshot.candidate_id, snapshot, artifacts.manifest.manifest_hash))
        counts = {name: len(rows) for name, rows in selected.items()}
        if counts != REPAIR_COUNTS:
            raise _invalid()
        self._validate_active_identity(tuple(jobs), voice)
        target_rows = {name: tuple(sorted(rows, key=lambda row: _identity(name, row).identity)) for name, rows in selected.items()}
        identities = tuple(_identity(name, row) for name in sorted(target_rows) for row in target_rows[name])
        if len(set(identities)) != len(identities):
            raise _invalid()
        self._reject_external_references(all_rows, target_rows, frozenset(identities))
        candidate_ids = [job.candidate_id for job in jobs]
        return PresenceRepairTarget(
            jobs=tuple(jobs), row_identities=identities, row_counts=tuple(sorted(counts.items())),
            candidate_order_hash=sha256_text(canonical_json(candidate_ids)),
            target_fingerprint=_fingerprint(target_rows),
            preserved_fingerprint=self.fingerprint_except(identities, all_rows=all_rows),
        )

    def _all_rows(self) -> dict[str, tuple[dict, ...]]:
        tables = tuple(row[0] for row in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('schema_migrations', 'voice_vad_repairs') ORDER BY name"
        ))
        return {name: tuple(dict(row) for row in self._conn.execute(f"SELECT * FROM {_quote(name)} ORDER BY rowid")) for name in tables}

    def fingerprint_except(self, identities: tuple[RepairRowIdentity, ...], *, all_rows: dict | None = None) -> str:
        excluded = frozenset(identities)
        rows = self._all_rows() if all_rows is None else all_rows
        preserved = {
            name: tuple(row for row in values if name not in _IDENTITY_COLUMNS or _identity(name, row) not in excluded)
            for name, values in rows.items()
        }
        return _fingerprint(preserved)

    @staticmethod
    def _one(rows: tuple[dict, ...], key: str, value: object) -> dict:
        matches = tuple(row for row in rows if row[key] == value)
        if len(matches) != 1:
            raise _invalid()
        return matches[0]

    @staticmethod
    def _validate_events(job: dict, units: tuple[dict, ...], events: tuple[dict, ...]) -> None:
        if len(events) != 17 or tuple(row["unit_key"] for row in units) != tuple(key for key, _ in PRESENCE_UNITS):
            raise _invalid()
        expected = [
            (None, "job_created", canonical_json({"source_job_id": None}), job["created_at"]),
            (None, "job_status_changed", canonical_json({"from_status": "queued", "to_status": "running"}), events[1]["created_at"]),
        ]
        for unit in units:
            expected.extend((
                (unit["unit_key"], "unit_started", canonical_json({"attempt_no": 1}), unit["started_at"]),
                (unit["unit_key"], "unit_succeeded", canonical_json({"attempt_no": 1, "output_hash": unit["output_hash"], "result_status": "success"}), unit["finished_at"]),
            ))
        expected.append((None, "job_status_changed", canonical_json({"from_status": "running", "to_status": "succeeded"}), job["updated_at"]))
        actual = [(row["unit_key"], row["event_kind"], row["metadata_json"], row["created_at"]) for row in events]
        if actual != expected:
            raise _invalid()
        timestamps = [row["created_at"] for row in events]
        if timestamps != sorted(timestamps) or any(utc_iso(datetime.fromisoformat(value.replace("Z", "+00:00"))) != value for value in timestamps):
            raise _invalid()

    def _validate_active_identity(self, jobs: tuple[PresenceRepairJob, ...], voice: VoiceVerificationRepository) -> None:
        profiles = DiscoveryRepository(self._conn).list_active_profile_versions()
        if Counter(job.snapshot.profile_id for job in jobs) != {profile.profile_id: 5 for profile in profiles} or len(profiles) != 4:
            raise _invalid()
        if set(voice.list_active_reference_profile_ids()) != {job.snapshot.reference_profile_id for job in jobs}:
            raise _invalid()
        thresholds = {job.snapshot.threshold_config_version for job in jobs}
        if len(thresholds) != 1:
            raise _invalid()
        threshold = next(iter(thresholds))
        row = self._conn.execute("SELECT is_active FROM speaker_threshold_configs WHERE version=?", (threshold,)).fetchone()
        if row is None or row[0] != 1 or not threshold.startswith("voice-calibration-"):
            raise _invalid()
        identity = voice.get_calibration_identity(threshold.removeprefix("voice-calibration-"))
        if identity.threshold_config_version != threshold:
            raise _invalid()

    def _reject_external_references(self, all_rows: dict, target_rows: dict, allowed: frozenset[RepairRowIdentity]) -> None:
        for source_table, source_rows in all_rows.items():
            foreign_keys = defaultdict(list)
            for row in self._conn.execute(f"PRAGMA foreign_key_list({_quote(source_table)})"):
                foreign_keys[row["id"]].append(dict(row))
            for group in foreign_keys.values():
                group.sort(key=lambda row: row["seq"])
                target_table = group[0]["table"]
                if target_table not in target_rows:
                    continue
                referenced = {tuple(row[key["to"]] for key in group) for row in target_rows[target_table]}
                for row in source_rows:
                    values = tuple(row[key["from"]] for key in group)
                    if None not in values and values in referenced and (
                        source_table not in _IDENTITY_COLUMNS or _identity(source_table, row) not in allowed
                    ):
                        raise _invalid()
