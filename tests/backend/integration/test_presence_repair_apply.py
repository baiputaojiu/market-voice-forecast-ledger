import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.repositories.presence_repair import PresenceRepairRepository
from market_voice_forecast_ledger.repositories.voice_verification import VoiceVerificationRepository
from tests.backend.presence_repair_fakes import repair_environment
from tests.backend.integration.test_presence_repair_preview import repair_service


def test_apply_preserves_every_unrelated_row_and_queues_same_candidates(repair_environment):
    env = repair_environment
    service = repair_service(env)
    preview = service.preview()
    result = service.apply(preview.preview_hash)
    assert result.old_job_ids == env.creation.job_ids
    assert len(result.new_job_ids) == 20
    assert set(result.old_job_ids).isdisjoint(result.new_job_ids)
    assert result.candidate_ids == env.creation.candidate_ids
    assert result.to_vad_contract_version == "vad-v2"
    with closing(open_database(env.settings.database_path)) as reopened:
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
        voice = VoiceVerificationRepository(reopened)
        for old, new_id in zip(preview.target.jobs, result.new_job_ids, strict=True):
            artifacts = voice.require_job_artifacts(new_id)
            assert artifacts.manifest.snapshot == replace(old.snapshot, vad_contract_version="vad-v2")
            assert artifacts.run is None
            assert reopened.execute("SELECT status FROM jobs WHERE id=?", (new_id,)).fetchone()[0] == "queued"
            assert tuple(row[0] for row in reopened.execute("SELECT status FROM job_units WHERE job_id=?", (new_id,))) == ("pending",) * 7
            assert tuple(row[0] for row in reopened.execute("SELECT event_kind FROM job_events WHERE job_id=?", (new_id,))) == ("job_created",)
            assert reopened.execute("SELECT 1 FROM jobs WHERE id=?", (old.job_id,)).fetchone() is None
        for table in ("voice_verification_runs", "voice_verification_segments", "voice_verification_reviews", "local_artifacts", "job_unit_attempts"):
            assert reopened.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        ledger = reopened.execute("SELECT * FROM voice_vad_repairs").fetchall()
        assert len(ledger) == 1
        assert json.loads(ledger[0]["candidate_ids_json"]) == list(result.candidate_ids)
        assert json.loads(ledger[0]["new_job_ids_json"]) == list(result.new_job_ids)
        assert ledger[0]["preserved_fingerprint"] == preview.target.preserved_fingerprint
        # Independently exclude only the new jobs and their creation records.
        rows = PresenceRepairRepository(reopened)._all_rows()
        new_ids = set(result.new_job_ids)
        from market_voice_forecast_ledger.repositories.presence_repair import _identity
        new_rows = tuple(_identity(table, row) for table in ("jobs", "job_units", "job_events", "video_pipeline_job_binding_sets", "video_pipeline_job_bindings", "voice_verification_manifests") for row in rows[table] if row.get("job_id", row.get("id")) in new_ids)
        assert PresenceRepairRepository(reopened).fingerprint_except(new_rows) == preview.target.preserved_fingerprint
        with pytest.raises(sqlite3.IntegrityError):
            reopened.execute("DELETE FROM jobs WHERE id=?", (result.new_job_ids[0],))
    assert (env.settings.data_dir / "backups" / "repair-test" / "database.sqlite3").is_file()
    with pytest.raises(DomainError) as caught:
        service.apply(preview.preview_hash)
    assert caught.value.code == "PRESENCE_REPAIR_ALREADY_APPLIED"


@pytest.mark.parametrize("fault", (
    "after_delete:voice_verification_segments", "after_delete:voice_verification_runs",
    "after_delete:job_events", "after_delete:jobs", "after_recreate:1",
    "after_recreate:20", "before_ledger", "before_commit",
))
def test_transaction_fault_rolls_back_all_rows_and_clears_authorization(repair_environment, fault):
    env = repair_environment

    def inject(stage):
        if stage == fault:
            raise RuntimeError("private-fault-sentinel")

    service = repair_service(env, fault_hook=inject)
    preview = service.preview()
    before = PresenceRepairRepository(env.conn).fingerprint_except(())
    with pytest.raises(DomainError) as caught:
        service.apply(preview.preview_hash)
    assert "private-fault-sentinel" not in str(caught.value)
    assert not env.conn.in_transaction
    assert env.conn.execute("SELECT presence_vad_repair_delete_authorized('jobs', ?)", (str(env.creation.job_ids[0]),)).fetchone()[0] == 0
    with closing(open_database(env.settings.database_path)) as reopened:
        assert PresenceRepairRepository(reopened).fingerprint_except(()) == before
        assert reopened.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            reopened.execute("DELETE FROM jobs WHERE id=?", (env.creation.job_ids[0],))
    assert (env.settings.data_dir / "backups" / "repair-test" / "database.sqlite3").is_file()


def test_wrong_hash_is_rejected_before_backup_or_runtime_writes(repair_environment):
    env = repair_environment
    service = repair_service(env)
    before = PresenceRepairRepository(env.conn).fingerprint_except(())
    with pytest.raises(DomainError) as caught:
        service.apply("f" * 64)
    assert caught.value.code == "PRESENCE_REPAIR_PREVIEW_CHANGED"
    assert not (env.settings.data_dir / "backups").exists()
    assert PresenceRepairRepository(env.conn).fingerprint_except(()) == before


@pytest.mark.parametrize("ending", ("commit", "rollback"))
def test_exact_authorization_is_single_use_and_cannot_cross_transactions(repair_environment, ending):
    env = repair_environment
    repository = PresenceRepairRepository(env.conn)
    target = repository.read_target()
    allowed = tuple(row for row in target.row_identities if row.table == "jobs")[:2]
    with pytest.raises(DomainError):
        with repository.authorize(allowed):
            pytest.fail("authorization was accepted outside a transaction")
    env.conn.execute("BEGIN")
    with repository.authorize(allowed):
        sql = "SELECT presence_vad_repair_delete_authorized(?, ?)"
        assert env.conn.execute(sql, ("wrong_table", allowed[0].identity)).fetchone()[0] == 0
        assert env.conn.execute(sql, ("jobs", allowed[0].identity)).fetchone()[0] == 1
        assert env.conn.execute(sql, ("jobs", allowed[0].identity)).fetchone()[0] == 0
        getattr(env.conn, ending)()
        env.conn.execute("BEGIN")
        assert env.conn.execute(sql, ("jobs", allowed[1].identity)).fetchone()[0] == 0
        env.conn.rollback()
    assert repository.read_target() == target


def test_drift_after_backup_is_rejected_inside_transaction(repair_environment):
    env = repair_environment

    def drift(stage):
        if stage == "after_backup":
            env.conn.execute("UPDATE retention_settings SET retention_days=180 WHERE id=1")

    service = repair_service(env, fault_hook=drift)
    preview = service.preview()
    with pytest.raises(DomainError) as caught:
        service.apply(preview.preview_hash)
    assert caught.value.code == "PRESENCE_REPAIR_PREVIEW_CHANGED"
    assert len(PresenceRepairRepository(env.conn).read_target().jobs) == 20
    assert env.conn.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 0


def test_corrupted_backup_is_rejected_before_database_mutation(repair_environment):
    env = repair_environment

    def corrupt(stage):
        if stage == "after_backup":
            (env.settings.data_dir / "backups" / "repair-test" / "database.sqlite3").write_bytes(b"synthetic-damage")

    service = repair_service(env, fault_hook=corrupt)
    preview = service.preview()
    before = PresenceRepairRepository(env.conn).fingerprint_except(())
    with pytest.raises(DomainError):
        service.apply(preview.preview_hash)
    assert PresenceRepairRepository(env.conn).fingerprint_except(()) == before
    assert env.conn.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 0


def test_postcommit_failure_keeps_result_and_backup_without_automatic_restore(repair_environment):
    env = repair_environment

    def fail(stage):
        if stage == "after_commit":
            raise RuntimeError("private-postcommit-sentinel")

    service = repair_service(env, fault_hook=fail)
    preview = service.preview()
    with pytest.raises(DomainError) as caught:
        service.apply(preview.preview_hash)
    assert caught.value.code == "PRESENCE_REPAIR_POSTVERIFY_FAILED"
    assert "private-postcommit-sentinel" not in str(caught.value)
    assert env.conn.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM voice_verification_manifests WHERE vad_contract_version='vad-v2'").fetchone()[0] == 20
    assert (env.settings.data_dir / "backups" / "repair-test" / "database.sqlite3").is_file()
