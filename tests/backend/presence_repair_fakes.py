"""Synthetic old-pilot data; no production identifiers or files are used."""

from pathlib import Path
from types import SimpleNamespace

from tests.backend.integration.test_presence_pilot import (
    NOW,
    pilot_service,
    seed_pilot_environment,
)
from tests.backend.integration.test_voice_verification_jobs import (
    FakePresenceAdapter,
    presence_worker_harness,
)


def seed_twenty_succeeded_v1_jobs(conn, tmp_path: Path):
    seed_pilot_environment(conn)
    service = pilot_service(conn, vad_contract_version="vad-v1")
    preview = service.preview_pilot()
    creation = service.create_pilot(preview.preview_hash)
    harness = presence_worker_harness(
        conn,
        tmp_path,
        SimpleNamespace(snapshot=preview.candidates[0].manifest_snapshot),
        adapter=FakePresenceAdapter(segments=((1_000, 1_900, 0.8),)),
        clock=lambda: NOW,
    )
    for job_id in creation.job_ids:
        summary = harness.worker.run_once()
        assert summary.job_id == job_id
        assert summary.succeeded_jobs == 1
    return creation
