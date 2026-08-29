import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_voice_forecast_ledger.pc_transfer.bundle import (
    ExportDependencies,
    ExportResult,
    ImportResult,
    VerifiedBundle,
)
from market_voice_forecast_ledger.pc_transfer.cli import (
    CliDependencies,
    main,
)
from market_voice_forecast_ledger.pc_transfer.runtime_rebuild import (
    RuntimeRebuildDependencies,
    RuntimeRebuildResult,
)
from tests.backend.unit.test_pc_transfer_manifest import sample_manifest


@dataclass
class CliFixture:
    repository_root: Path
    data_root: Path
    drive_dir: Path
    bundle_path: Path
    export_error: Exception | None = None

    def dependencies(self) -> CliDependencies:
        manifest = sample_manifest()

        def export_service(request, dependencies):
            del dependencies
            if self.export_error is not None:
                raise self.export_error
            assert request.repository_root == self.repository_root
            assert request.settings.data_dir == self.data_root
            assert request.destination_dir == self.drive_dir
            self.bundle_path.write_bytes(b"completed bundle")
            return ExportResult(self.bundle_path, manifest)

        def verify_service(path: Path) -> VerifiedBundle:
            assert path == self.bundle_path
            return VerifiedBundle(path, manifest)

        def import_service(request) -> ImportResult:
            assert request.repository_root == self.repository_root
            assert request.data_root == self.data_root
            return ImportResult(
                data_root=self.data_root,
                operator_state_dir=(
                    self.repository_root / manifest.operator_state_destination
                ),
                manifest=manifest,
                runtime_required=True,
                credential_required=True,
                schedule_required=True,
            )

        def runtime_service(request, dependencies) -> RuntimeRebuildResult:
            del dependencies
            assert request.data_root == self.data_root
            return RuntimeRebuildResult(
                runtime_root=self.data_root / "voice-runtime",
                active_lock="runtime-lock.json",
                attestations=(),
            )

        return CliDependencies(
            repository_root=self.repository_root,
            clock=lambda: datetime(
                2026,
                8,
                29,
                3,
                4,
                5,
                tzinfo=timezone.utc,
            ),
            export_dependencies=ExportDependencies(
                version_probe=lambda _command: "unused"
            ),
            runtime_dependencies=RuntimeRebuildDependencies(),
            export_service=export_service,
            verify_service=verify_service,
            import_service=import_service,
            rebuild_service=runtime_service,
            runtime_verify_service=runtime_service,
        )


@pytest.fixture
def cli_fixture(tmp_path: Path) -> CliFixture:
    repository = tmp_path / "repo-日本語"
    repository.mkdir()
    data_root = tmp_path / "local-data"
    drive = tmp_path / "ordinary-folder"
    drive.mkdir()
    return CliFixture(
        repository_root=repository,
        data_root=data_root,
        drive_dir=drive,
        bundle_path=drive / "transfer.zip",
    )


def test_export_cli_reports_completed_bundle(
    cli_fixture: CliFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        (
            "export",
            "--destination",
            str(cli_fixture.drive_dir),
            "--schedule-local-time",
            "06:00",
            "--repository-root",
            str(cli_fixture.repository_root),
            "--data-root",
            str(cli_fixture.data_root),
        ),
        cli_fixture.dependencies(),
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert output["status"] == "exported"
    assert output["bundle_id"]
    assert Path(output["bundle_path"]).is_file()
    assert output["schedule_local_time"] == "06:00"


def test_cli_failure_does_not_print_private_exception_or_path(
    cli_fixture: CliFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = str(cli_fixture.data_root)
    cli_fixture.export_error = OSError(f"failed at {private_path}")

    exit_code = main(
        (
            "export",
            "--destination",
            str(cli_fixture.drive_dir),
            "--schedule-local-time",
            "06:00",
            "--data-root",
            private_path,
        ),
        cli_fixture.dependencies(),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "failed",
        "error_code": "PC_TRANSFER_COMMAND_FAILED",
    }
    assert private_path not in captured.err


@pytest.mark.parametrize(
    ("command", "expected_status"),
    (
        (("verify", "--bundle", "{bundle}"), "verified"),
        (("import", "--bundle", "{bundle}"), "imported"),
        (("rebuild-runtime", "--bundle", "{bundle}"), "runtime-rebuilt"),
        (("verify-runtime", "--bundle", "{bundle}"), "runtime-verified"),
    ),
)
def test_each_transfer_command_returns_canonical_json(
    cli_fixture: CliFixture,
    capsys: pytest.CaptureFixture[str],
    command: tuple[str, ...],
    expected_status: str,
) -> None:
    argv = tuple(
        value.format(bundle=cli_fixture.bundle_path) for value in command
    )
    if command[0] != "verify":
        argv += ("--data-root", str(cli_fixture.data_root))
    if command[0] == "import":
        argv += ("--repository-root", str(cli_fixture.repository_root))

    assert main(argv, cli_fixture.dependencies()) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["status"] == expected_status


@pytest.mark.parametrize(
    "argv",
    (
        (
            "export",
            "--destination",
            "one",
            "--destination",
            "two",
            "--schedule-local-time",
            "06:00",
        ),
        (
            "export",
            "--destination",
            "one",
            "--schedule-local-time",
            "25:00",
        ),
        ("verify", "--bun", "transfer.zip"),
    ),
)
def test_cli_rejects_duplicate_invalid_or_abbreviated_arguments_safely(
    cli_fixture: CliFixture,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    assert main(argv, cli_fixture.dependencies()) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid arguments" in captured.err
    assert str(cli_fixture.data_root) not in captured.err


def test_cli_requires_localappdata_only_when_data_root_is_omitted(
    cli_fixture: CliFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert main(
        ("verify-runtime", "--bundle", str(cli_fixture.bundle_path)),
        cli_fixture.dependencies(),
    ) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.err) == {
        "status": "failed",
        "error_code": "PC_TRANSFER_LOCAL_DATA_UNAVAILABLE",
    }


def test_repository_bootstrap_script_exists() -> None:
    root = Path(__file__).resolve().parents[3]
    assert (root / "scripts/pc-transfer/pc-transfer.py").is_file()
