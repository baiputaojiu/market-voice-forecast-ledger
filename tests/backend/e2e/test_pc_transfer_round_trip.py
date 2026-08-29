import subprocess
from pathlib import Path, PurePosixPath

import pytest

from market_voice_forecast_ledger.pc_transfer import (
    runtime_rebuild as rebuild_module,
)
from market_voice_forecast_ledger.pc_transfer.bundle import (
    ImportRequest,
    export_bundle,
    import_bundle,
    verify_bundle,
)
from market_voice_forecast_ledger.pc_transfer.runtime_rebuild import (
    RuntimeRebuildDependencies,
    RuntimeRebuildRequest,
    rebuild_voice_runtime,
)
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists,
    attest_runtime as real_attest_runtime,
)
from tests.backend.integration.test_pc_transfer_bundle import (
    TransferSourceFixture,
    transfer_source,
)
from tests.backend.unit.test_pc_transfer_runtime_rebuild import (
    FakeRuntimeBuilder,
)


def test_transfer_round_trip_recreates_resumable_state(
    transfer_source: TransferSourceFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export_root = tmp_path / "google-drive-transport"
    export_root.mkdir()
    source_request = transfer_source.export_request(export_root)
    progress_bytes = (
        transfer_source.operator_state_dir / "progress.md"
    ).read_bytes()

    exported = export_bundle(
        source_request,
        transfer_source.dependencies(),
    )
    verified = verify_bundle(exported.bundle_path)

    new_user_root = tmp_path / "new-user"
    new_repository = new_user_root / "repository"
    new_repository.parent.mkdir(parents=True)
    subprocess.run(
        (
            "git",
            "clone",
            "--quiet",
            "--branch",
            verified.manifest.branch,
            "--single-branch",
            verified.manifest.repository_url,
            str(new_repository),
        ),
        check=True,
        capture_output=True,
    )
    local_app_data = new_user_root / "LocalAppData"
    local_app_data.mkdir()
    new_data_root = local_app_data / "MarketVoiceForecastLedger"
    imported = import_bundle(
        ImportRequest(
            bundle_path=exported.bundle_path,
            repository_root=new_repository,
            data_root=new_data_root,
        )
    )

    tool_hashes = {
        PurePosixPath(member.path).name: member.sha256
        for member in imported.manifest.members
        if member.role == "runtime-tool"
    }
    sherpa_hash = next(
        member.sha256
        for member in imported.manifest.members
        if member.role == "runtime-wheel"
        and PurePosixPath(member.path).name.startswith(
            f"sherpa_onnx-{imported.manifest.runtime.sherpa_onnx_version}-"
        )
    )
    allowlists = RuntimeAllowlists(
        yt_dlp_sha256=tool_hashes["yt-dlp.exe"],
        deno_sha256=tool_hashes["deno.exe"],
        sherpa_wheel_sha256=sherpa_hash,
    )

    def attest_with_transfer_allowlists(
        settings,
        *,
        version_probe,
        lock_name="runtime-lock.json",
    ):
        return real_attest_runtime(
            settings,
            version_probe=version_probe,
            allowlists=allowlists,
            lock_name=lock_name,
        )

    monkeypatch.setattr(
        rebuild_module,
        "attest_runtime",
        attest_with_transfer_allowlists,
    )
    builder = FakeRuntimeBuilder()
    rebuild_dependencies = RuntimeRebuildDependencies(
        venv_builder=builder.build_venv,
        process_runner=builder.run_process,
        version_probe=builder.version_probe,
    )
    rebuilt = rebuild_voice_runtime(
        RuntimeRebuildRequest(imported.data_root, imported.manifest),
        rebuild_dependencies,
    )

    assert imported.manifest.bundle_id == exported.manifest.bundle_id
    assert imported.manifest.commit_sha == transfer_source.commit_sha
    assert imported.manifest.database == exported.manifest.database
    assert len(rebuilt.attestations) == 3
    assert builder.network_attempts == []
    assert (imported.operator_state_dir / "progress.md").read_bytes() == (
        progress_bytes
    )
    assert imported.data_root != transfer_source.settings.data_dir
