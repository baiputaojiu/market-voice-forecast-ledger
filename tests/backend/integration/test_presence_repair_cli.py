from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger import cli
from market_voice_forecast_ledger.domain.errors import DomainError
from tests.backend.presence_repair_fakes import repair_environment
from tests.backend.integration.test_presence_repair_preview import persistent_files


TOKEN = "a" * 64


def test_cli_renders_only_repair_summary_and_intentional_preview_token(capsys):
    class Service:
        def preview(self):
            return SimpleNamespace(from_vad_contract_version="vad-v1", to_vad_contract_version="vad-v2", target=SimpleNamespace(counts={"jobs": 20}), preview_hash=TOKEN, private_path="C:/private-sentinel/database.sqlite3")

        def apply(self, token):
            assert token == TOKEN
            return SimpleNamespace(new_job_ids=tuple(range(1, 21)), to_vad_contract_version="vad-v2", private_path="C:/private-sentinel")

    assert cli.run_cli(["presence", "pilot", "repair", "preview"], presence_repair_service_factory=Service) == 0
    assert capsys.readouterr().out == f"Presence repair preview: 20 jobs, vad-v1 -> vad-v2, preview_hash={TOKEN}\n"
    assert cli.run_cli(["presence", "pilot", "repair", "apply", "--expected-preview-hash", TOKEN], presence_repair_service_factory=Service) == 0
    assert capsys.readouterr().out == "Presence repair completed: 20 queued vad-v2 jobs.\n"


@pytest.mark.parametrize("arguments", (
    ["apply"], ["apply", "--expected-preview", TOKEN],
    ["apply", "--expected-preview-hash", TOKEN, "--expected-preview-hash", TOKEN],
    ["apply", "--expected-preview-hash", "A" * 64], ["apply", "--expected-preview-hash", "a" * 63],
    ["preview", "--database", "C:/private-sentinel"],
    ["preview", "--expected-preview-hash", TOKEN], ["apply", "--expected-preview-hash", TOKEN, "extra"],
))
def test_repair_parser_rejects_missing_duplicate_abbreviated_or_unknown_arguments(arguments, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.run_cli(["presence", "pilot", "repair", *arguments])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert "private-sentinel" not in output.err


@pytest.mark.parametrize("code", ("PRESENCE_REPAIR_TARGET_INVALID", "PRESENCE_REPAIR_PREVIEW_CHANGED", "PRESENCE_REPAIR_BACKUP_FAILED", "PRESENCE_REPAIR_ALREADY_APPLIED", "PRESENCE_REPAIR_POSTVERIFY_FAILED"))
def test_repair_errors_print_only_fixed_safe_code(code, capsys):
    class Service:
        def preview(self):
            raise DomainError(code, "C:/private-sentinel/secret.sqlite3")

    assert cli.run_cli(["presence", "pilot", "repair", "preview"], presence_repair_service_factory=Service) == 1
    assert capsys.readouterr().err == code + "\n"


def test_default_cli_preview_uses_readonly_database_and_never_worker(repair_environment, monkeypatch, capsys):
    env = repair_environment
    monkeypatch.setattr(cli, "default_settings", lambda: env.settings)
    before = persistent_files(env.settings)

    def forbidden(*_args):
        pytest.fail("repair preview invoked a worker")

    assert cli.run_cli(["presence", "pilot", "repair", "preview"], worker_runner=forbidden, presence_worker_runner=forbidden) == 0
    output = capsys.readouterr().out
    assert output.startswith("Presence repair preview: 20 jobs, vad-v1 -> vad-v2, preview_hash=")
    assert len(output.rstrip().split("preview_hash=")[1]) == 64
    assert persistent_files(env.settings) == before
    assert env.conn.execute("SELECT COUNT(*) FROM voice_vad_repairs").fetchone()[0] == 0


@pytest.mark.parametrize("repair_environment", ("0020",), indirect=True)
def test_default_cli_apply_installs_only_schema_and_keeps_preview_identity(repair_environment, monkeypatch, capsys):
    from market_voice_forecast_ledger.services import presence_repair
    from tests.backend.integration.test_presence_pilot import NOW

    env = repair_environment
    monkeypatch.setattr(cli, "default_settings", lambda: env.settings)
    original_service = presence_repair.PresenceRepairService

    def synthetic_runtime_service(conn, settings, **_kwargs):
        return original_service(conn, settings, clock=lambda: NOW, version_probe=env.probe, allowlists=env.allowlists,
                                backup_root=env.settings.data_dir / "backups" / "cli-repair")

    monkeypatch.setattr(presence_repair, "PresenceRepairService", synthetic_runtime_service)
    assert cli.run_cli(["presence", "pilot", "repair", "preview"]) == 0
    token = capsys.readouterr().out.strip().split("preview_hash=")[1]
    assert env.conn.execute("SELECT MAX(name) FROM schema_migrations").fetchone()[0].startswith("0020_")
    assert env.conn.execute("SELECT 1 FROM sqlite_master WHERE name='voice_vad_repairs'").fetchone() is None
    assert cli.run_cli(["presence", "pilot", "repair", "apply", "--expected-preview-hash", token]) == 0
    assert capsys.readouterr().out == "Presence repair completed: 20 queued vad-v2 jobs.\n"
    assert env.conn.execute("SELECT MAX(name) FROM schema_migrations").fetchone()[0] == "0021_presence_vad_repair"
    assert env.conn.execute("SELECT preview_hash FROM voice_vad_repairs").fetchone()[0] == token
    assert env.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 20
    assert env.conn.execute("SELECT COUNT(*) FROM voice_verification_runs").fetchone()[0] == 0
