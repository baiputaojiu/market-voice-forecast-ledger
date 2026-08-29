import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer import bundle as bundle_module
from market_voice_forecast_ledger.pc_transfer.bundle import (
    ImportRequest,
    export_bundle,
    import_bundle,
)
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    validate_database_snapshot,
)
from tests.backend.integration.test_pc_transfer_bundle import (
    TransferSourceFixture,
    transfer_source,
)
from tests.backend.integration.test_pc_transfer_checkpoint import git


@dataclass(frozen=True)
class VerifiedTransferFixture:
    path: Path
    manifest: object

    def clone_at(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            (
                "git",
                "clone",
                "--quiet",
                "--branch",
                self.manifest.branch,
                "--single-branch",
                self.manifest.repository_url,
                str(destination),
            ),
            check=True,
            capture_output=True,
        )
        return destination


@pytest.fixture
def verified_transfer(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
) -> VerifiedTransferFixture:
    export_root = tmp_path / "export"
    export_root.mkdir()
    request = transfer_source.export_request(export_root)
    result = export_bundle(request, transfer_source.dependencies())
    return VerifiedTransferFixture(result.bundle_path, result.manifest)


def assert_transfer_error(code: str, callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code == code


def test_import_restores_data_and_operator_state_at_new_paths(
    verified_transfer: VerifiedTransferFixture,
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "different-user"
    new_repository = verified_transfer.clone_at(user_root / "repo")
    local_app_data = user_root / "LocalAppData"
    local_app_data.mkdir()
    data_root = local_app_data / "MarketVoiceForecastLedger"

    result = import_bundle(
        ImportRequest(
            bundle_path=verified_transfer.path,
            repository_root=new_repository,
            data_root=data_root,
        )
    )

    assert result.data_root == data_root
    assert result.operator_state_dir == (
        new_repository / ".superpowers/sdd/2026-08-22-presence-verification"
    )
    assert (data_root / "ledger.sqlite3").is_file()
    assert (data_root / "voice-models/silero_vad.onnx").is_file()
    assert (
        data_root / "voice-wheelhouse/requirements-runtime.txt"
    ).is_file()
    assert (data_root / "voice-work/install/deno.exe").is_file()
    assert (result.operator_state_dir / "progress.md").is_file()
    assert not (data_root / "voice-runtime").exists()
    assert result.runtime_required is True
    assert result.credential_required is True
    assert result.schedule_required is True
    validate_database_snapshot(
        data_root / "ledger.sqlite3",
        result.manifest.database,
    )


@pytest.mark.parametrize("occupied_target", ("data_root", "operator_state"))
def test_import_refuses_non_empty_destination_without_changes(
    verified_transfer: VerifiedTransferFixture,
    tmp_path: Path,
    occupied_target: str,
) -> None:
    repository = verified_transfer.clone_at(tmp_path / "repo")
    data_parent = tmp_path / "local"
    data_parent.mkdir()
    data_root = data_parent / "data"
    operator = (
        repository / ".superpowers/sdd/2026-08-22-presence-verification"
    )
    target = data_root if occupied_target == "data_root" else operator
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    assert_transfer_error(
        "PC_TRANSFER_DESTINATION_NOT_EMPTY",
        lambda: import_bundle(
            ImportRequest(verified_transfer.path, repository, data_root)
        ),
    )
    assert marker.read_text(encoding="utf-8") == "keep"
    if occupied_target == "data_root":
        assert not operator.exists()


def test_import_rejects_wrong_commit_before_creating_destination(
    verified_transfer: VerifiedTransferFixture,
    tmp_path: Path,
) -> None:
    repository = verified_transfer.clone_at(tmp_path / "repo")
    git(repository, "config", "user.name", "PC Transfer Test")
    git(repository, "config", "user.email", "pc-transfer@example.invalid")
    (repository / "new.txt").write_text("new\n", encoding="utf-8")
    git(repository, "add", "new.txt")
    git(repository, "commit", "-m", "new commit")
    git(repository, "push", "origin", verified_transfer.manifest.branch)
    data_parent = tmp_path / "local"
    data_parent.mkdir()
    data_root = data_parent / "data"

    assert_transfer_error(
        "PC_TRANSFER_GIT_MISMATCH",
        lambda: import_bundle(
            ImportRequest(verified_transfer.path, repository, data_root)
        ),
    )
    assert not data_root.exists()


def test_import_rejects_archive_changed_between_verifications(
    verified_transfer: VerifiedTransferFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = verified_transfer.clone_at(tmp_path / "repo")
    data_parent = tmp_path / "local"
    data_parent.mkdir()
    data_root = data_parent / "data"

    def truncate_bundle() -> None:
        body = verified_transfer.path.read_bytes()
        verified_transfer.path.write_bytes(body[: len(body) // 2])

    monkeypatch.setattr(
        bundle_module,
        "_after_staged_extract",
        truncate_bundle,
    )

    assert_transfer_error(
        "PC_TRANSFER_BUNDLE_INVALID",
        lambda: import_bundle(
            ImportRequest(verified_transfer.path, repository, data_root)
        ),
    )
    assert not data_root.exists()


def test_import_reports_partial_if_operator_placement_fails(
    verified_transfer: VerifiedTransferFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = verified_transfer.clone_at(tmp_path / "repo")
    data_parent = tmp_path / "local"
    data_parent.mkdir()
    data_root = data_parent / "data"

    def fail_operator_placement(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("synthetic operator placement failure")

    monkeypatch.setattr(
        bundle_module,
        "_place_operator_staging",
        fail_operator_placement,
    )

    assert_transfer_error(
        "PC_TRANSFER_IMPORT_PARTIAL",
        lambda: import_bundle(
            ImportRequest(verified_transfer.path, repository, data_root)
        ),
    )
    assert (data_root / "ledger.sqlite3").is_file()
    assert not (data_root / "voice-runtime").exists()
    validate_database_snapshot(
        data_root / "ledger.sqlite3",
        verified_transfer.manifest.database,
    )
    assert not (
        repository / ".superpowers/sdd/2026-08-22-presence-verification"
    ).exists()
