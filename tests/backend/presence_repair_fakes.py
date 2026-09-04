"""Synthetic old-pilot data; no production identifiers or files are used."""

from pathlib import Path
from types import SimpleNamespace
import json
from importlib import resources

import pytest

from market_voice_forecast_ledger.bootstrap import bootstrap_reference_data
from market_voice_forecast_ledger.db.connection import open_database, transaction
from market_voice_forecast_ledger.db.migrate import apply_migrations, _execute_script
from market_voice_forecast_ledger.domain.common import utc_iso

from tests.backend.integration.test_presence_pilot import (
    MODEL_NAME,
    MODEL_VERSION,
    NOW,
    pilot_service,
    seed_pilot_environment,
)
from tests.backend.unit.test_voice_runtime_upgrade import three_lock_fixture, LOCK_NAMES
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


@pytest.fixture
def repair_environment(tmp_path: Path, request):
    settings, probe, allowlists = three_lock_fixture(tmp_path)
    for name in (LOCK_NAMES[0], LOCK_NAMES[-1]):
        path = settings.voice_runtime_dir / name
        document = json.loads(path.read_bytes())
        document["model"]["name"] = MODEL_NAME
        document["model"]["version"] = MODEL_VERSION
        path.write_text(json.dumps(document), encoding="utf-8")
    conn = open_database(settings.database_path)
    if getattr(request, "param", None) == "0020":
        conn.execute("CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for item in sorted(resources.files("market_voice_forecast_ledger.db.migrations").iterdir()):
            if item.name.endswith(".sql") and item.name[:4].isdigit() and item.name[:4] <= "0020":
                with transaction(conn):
                    _execute_script(conn, item.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (item.name.removesuffix(".sql"), utc_iso(NOW)))
    else:
        apply_migrations(conn)
    bootstrap_reference_data(conn)
    creation = seed_twenty_succeeded_v1_jobs(conn, tmp_path)
    try:
        yield SimpleNamespace(conn=conn, settings=settings, probe=probe, allowlists=allowlists, creation=creation)
    finally:
        conn.close()
