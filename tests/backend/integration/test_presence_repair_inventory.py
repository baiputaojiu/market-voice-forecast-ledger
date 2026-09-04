import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.domain.presence_repair import build_presence_repair_preview
from market_voice_forecast_ledger.repositories.presence_repair import PresenceRepairRepository
from tests.backend.integration.test_presence_pilot import db
from tests.backend.presence_repair_fakes import seed_twenty_succeeded_v1_jobs


def test_exact_inventory_is_read_only_and_binds_all_rows(db, tmp_path: Path) -> None:
    creation = seed_twenty_succeeded_v1_jobs(db, tmp_path)
    before = db.total_changes
    target = PresenceRepairRepository(db).read_target("vad-v1", "vad-v2")
    preview = build_presence_repair_preview("vad-v1", "vad-v2", target)

    assert db.total_changes == before
    assert not db.in_transaction
    assert tuple(job.candidate_id for job in target.jobs) == creation.candidate_ids
    assert dict(target.counts) == {
        "jobs": 20, "job_units": 140, "job_unit_attempts": 140, "job_events": 340,
        "video_pipeline_job_binding_sets": 20, "video_pipeline_job_bindings": 20,
        "voice_verification_manifests": 20, "voice_verification_runs": 20,
        "voice_verification_segments": 20, "voice_verification_reviews": 0,
        "local_artifacts": 60,
    }
    assert len(preview.preview_hash) == 64
    drift = replace(target, preserved_fingerprint="f" * 64)
    assert build_presence_repair_preview("vad-v1", "vad-v2", drift).preview_hash != preview.preview_hash


@pytest.mark.parametrize("mutation", ("nineteen", "two_segments", "event_drift", "extra_source_reference", "artifact_exists", "decision_drift", "attempt_count"))
def test_inventory_rejects_noncanonical_targets(db, tmp_path: Path, mutation: str) -> None:
    creation = seed_twenty_succeeded_v1_jobs(db, tmp_path)
    job_id = creation.job_ids[0]
    if mutation == "nineteen":
        db.execute("DROP TRIGGER voice_verification_manifests_no_update")
        db.execute("UPDATE voice_verification_manifests SET vad_contract_version='vad-other' WHERE job_id=?", (job_id,))
    elif mutation == "two_segments":
        db.execute(
            "INSERT INTO voice_verification_segments(run_id, ordinal, start_ms, end_ms, raw_match_score, evidence_hash) "
            "SELECT id, 2, 1900, 2000, 0.8, ? FROM voice_verification_runs WHERE job_id=?",
            ("f" * 64, job_id),
        )
    elif mutation == "event_drift":
        db.execute("DROP TRIGGER job_events_no_update")
        db.execute("UPDATE job_events SET metadata_json='{}' WHERE job_id=? AND event_kind='unit_started'", (job_id,))
    elif mutation == "extra_source_reference":
        db.execute(
            "INSERT INTO jobs(source_job_id, job_kind, manifest_hash, total_units, status, created_at, updated_at) "
            "SELECT id, job_kind, manifest_hash, total_units, 'queued', created_at, updated_at FROM jobs WHERE id=?",
            (job_id,),
        )
    elif mutation == "artifact_exists":
        path = Path(db.execute("SELECT local_path FROM local_artifacts ORDER BY id LIMIT 1").fetchone()[0])
        path.write_bytes(b"synthetic-residual-audio")
    elif mutation == "decision_drift":
        from tests.backend.integration.test_presence_pilot import _set_presence_state
        from market_voice_forecast_ledger.domain.discovery import PresenceState

        _set_presence_state(db, creation.candidate_ids[0], PresenceState.CONFIRMED)
    else:
        db.execute("UPDATE job_units SET attempt_count=2 WHERE job_id=? AND unit_key='voice:vad'", (job_id,))

    before = db.total_changes
    with pytest.raises(DomainError) as caught:
        PresenceRepairRepository(db).read_target("vad-v1", "vad-v2")
    assert caught.value.code == "PRESENCE_REPAIR_TARGET_INVALID"
    assert db.total_changes == before
    assert not db.in_transaction
