import socket
import subprocess
from types import SimpleNamespace

from market_voice_forecast_ledger import cli
from market_voice_forecast_ledger.repositories.voice_verification import VoiceVerificationRepository
from tests.backend.integration.test_presence_pilot import NOW
from tests.backend.integration.test_presence_repair_preview import repair_service
from tests.backend.integration.test_voice_verification_jobs import presence_worker_harness
from tests.backend.presence_repair_fakes import repair_environment


def test_cli_repair_stays_offline_and_a_synthetic_worker_accepts_replacement(repair_environment, tmp_path, monkeypatch, capsys):
    env = repair_environment
    service = repair_service(env)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("repair attempted an external operation")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert cli.run_cli(["presence", "pilot", "repair", "preview"], presence_repair_service_factory=lambda: service) == 0
    token = capsys.readouterr().out.strip().split("preview_hash=")[1]
    assert cli.run_cli(["presence", "pilot", "repair", "apply", "--expected-preview-hash", token], presence_repair_service_factory=lambda: service, presence_worker_runner=forbidden) == 0
    assert "20 queued vad-v2 jobs" in capsys.readouterr().out
    assert env.conn.execute("SELECT COUNT(*) FROM voice_verification_runs").fetchone()[0] == 0
    first = env.conn.execute("SELECT MIN(job_id) FROM voice_verification_manifests").fetchone()[0]
    snapshot = VoiceVerificationRepository(env.conn).get_manifest_for_job(first).snapshot
    assert snapshot.vad_contract_version == "vad-v2"
    # This is a synthetic-only follow-on compatibility check, not repair apply.
    worker_root = tmp_path / "replacement-worker"
    worker_root.mkdir()
    worker = presence_worker_harness(env.conn, worker_root, SimpleNamespace(snapshot=snapshot), clock=lambda: NOW).worker
    result = worker.run_once()
    assert result.job_id == first
    assert result.succeeded_jobs == 1
    assert VoiceVerificationRepository(env.conn).require_job_artifacts(first).run is not None
    assert env.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 19
