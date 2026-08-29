import hashlib
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger.db.connection import open_database
from market_voice_forecast_ledger.db.migrate import apply_migrations
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer import runtime_rebuild as rebuild_module
from market_voice_forecast_ledger.pc_transfer.manifest import (
    OPERATOR_STATE_DESTINATION,
    SCHEMA,
    BundleMember,
    RuntimeModel,
    RuntimeSummary,
    TransferManifest,
    compute_bundle_id,
)
from market_voice_forecast_ledger.pc_transfer.portable import (
    EXPECTED_REQUIREMENT_LINES,
)
from market_voice_forecast_ledger.pc_transfer.runtime_rebuild import (
    RuntimeRebuildDependencies,
    RuntimeRebuildRequest,
    rebuild_voice_runtime,
    verify_rebuilt_runtime,
)
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    create_database_snapshot,
)
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists,
    attest_runtime as real_attest_runtime,
    verify_runtime_startup,
)
from tests.backend.integration.test_pc_transfer_snapshot import (
    repository_migration_names,
    seed_valid_reference_feature,
)
from tests.backend.unit.test_pc_transfer_portable import DEPENDENCY_WHEELS


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def member_destination(data_root: Path, path: str, role: str) -> Path:
    name = PurePosixPath(path).name
    if role == "database":
        return data_root / "ledger.sqlite3"
    if role == "model":
        return data_root / "voice-models" / name
    if role in {"runtime-requirements", "runtime-wheel"}:
        return data_root / "voice-wheelhouse" / name
    if role in {"runtime-tool", "project-wheel"}:
        return data_root / "voice-work/install" / name
    raise AssertionError(role)


@dataclass
class FakeRuntimeBuilder:
    python_version: str = "3.14.6"
    fail_pip: bool = False

    def __post_init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str]] = []
        self.network_attempts: list[tuple[str, ...]] = []

    def build_venv(self, destination: Path) -> None:
        write(destination / "Scripts/python.exe", b"private python")
        write(
            destination / "pyvenv.cfg",
            b"home = C:/private-python\n"
            b"include-system-site-packages = false\n"
            b"version = 3.14.6\n",
        )
        (destination / "Lib/site-packages").mkdir(parents=True)

    def run_process(
        self,
        command: tuple[str, ...],
        working_directory: Path,
        environment,
    ) -> SimpleNamespace:
        assert working_directory.is_dir()
        copied_environment = dict(environment)
        self.commands.append(command)
        self.environments.append(copied_environment)
        forbidden = {
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "pip_index_url",
            "pip_extra_index_url",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        }
        if (
            "--no-index" not in command
            or copied_environment.get("PIP_NO_INDEX") != "1"
            or forbidden & copied_environment.keys()
        ):
            self.network_attempts.append(command)
        if self.fail_pip:
            return SimpleNamespace(returncode=1)
        import_root = (
            Path(command[0]).parents[1] / "Lib" / "site-packages"
        )
        if "--require-hashes" in command:
            write(import_root / "sherpa_onnx/__init__.py", b"# sherpa\n")
        else:
            write(
                import_root
                / "market_voice_forecast_ledger/voice/adapter_main.py",
                b"# adapter\n",
            )
            write(
                import_root / "market_voice_forecast_ledger/__init__.py",
                b"# package\n",
            )
        return SimpleNamespace(returncode=0)

    def version_probe(self, command: tuple[str, ...]) -> str:
        executable = Path(command[0]).name.casefold()
        argument = command[1]
        if executable == Path(sys.executable).name.casefold() and (
            Path(command[0]).resolve() == Path(sys.executable).resolve()
        ):
            return f"Python {self.python_version}"
        if executable == "python.exe" and argument == "--version":
            return "Python 3.14.6"
        if executable == "deno.exe":
            return "deno 2.9.5"
        if executable == "ffmpeg.exe":
            return "ffmpeg version 9.0.1"
        if executable == "yt-dlp.exe":
            return "2026.08.19"
        raise AssertionError(command)


@dataclass
class ImportedTransferFixture:
    data_root: Path
    manifest: TransferManifest
    builder: FakeRuntimeBuilder

    def request(self) -> RuntimeRebuildRequest:
        return RuntimeRebuildRequest(self.data_root, self.manifest)

    def dependencies(self) -> RuntimeRebuildDependencies:
        return RuntimeRebuildDependencies(
            venv_builder=self.builder.build_venv,
            process_runner=self.builder.run_process,
            version_probe=self.builder.version_probe,
        )

    def member(self, role: str) -> BundleMember:
        return next(item for item in self.manifest.members if item.role == role)


@pytest.fixture
def imported_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ImportedTransferFixture:
    data_root = tmp_path / "new-local-data"
    data_root.mkdir()
    source_database = tmp_path / "source.sqlite3"
    connection = open_database(source_database)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    seed_valid_reference_feature(source_database)
    snapshot = create_database_snapshot(
        source_database,
        data_root / "ledger.sqlite3",
        repository_migration_names(),
    )

    file_definitions: list[tuple[str, str, bytes]] = [
        (
            "portable/voice-models/"
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            "model",
            b"campplus model",
        ),
        (
            "portable/voice-models/silero_vad.onnx",
            "model",
            b"silero vad",
        ),
        (
            "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
            "model",
            b"wespeaker model",
        ),
        (
            "portable/voice-wheelhouse/requirements-runtime.txt",
            "runtime-requirements",
            ("\n".join(EXPECTED_REQUIREMENT_LINES) + "\n").encode(),
        ),
        (
            "portable/voice-install/deno.exe",
            "runtime-tool",
            b"deno executable",
        ),
        (
            "portable/voice-install/ffmpeg.exe",
            "runtime-tool",
            b"ffmpeg executable",
        ),
        (
            "portable/voice-install/yt-dlp.exe",
            "runtime-tool",
            b"yt-dlp executable",
        ),
        (
            "portable/voice-install/"
            "market_voice_forecast_ledger-0.1.0-py3-none-any.whl",
            "project-wheel",
            b"project wheel",
        ),
    ]
    for wheel_name in DEPENDENCY_WHEELS:
        file_definitions.append(
            (
                f"portable/voice-wheelhouse/{wheel_name}",
                "runtime-wheel",
                f"wheel:{wheel_name}".encode(),
            )
        )
    members: list[BundleMember] = [
        BundleMember(
            path="data/ledger.sqlite3",
            role="database",
            size_bytes=(data_root / "ledger.sqlite3").stat().st_size,
            sha256=snapshot.database.snapshot_sha256,
        ),
        BundleMember(
            path="operator-state/presence-verification/progress.md",
            role="operator-state",
            size_bytes=10,
            sha256="f" * 64,
        ),
    ]
    for path, role, body in file_definitions:
        destination = member_destination(data_root, path, role)
        write(destination, body)
        members.append(
            BundleMember(
                path=path,
                role=role,
                size_bytes=len(body),
                sha256=sha256(body),
            )
        )
    runtime = RuntimeSummary(
        python_version="3.14.6",
        sherpa_onnx_version="1.13.4",
        yt_dlp_version="2026.08.19",
        deno_version="2.9.5",
        ffmpeg_version="9.0.1",
        vad_version="silero-vad-v5",
        provider="CPUExecutionProvider",
        adapter_contract_version="voice-adapter-v1",
        vad_contract_version="vad-v1",
        models=(
            RuntimeModel(
                lock_name="runtime-lock.campplus.json",
                model_name="3dspeaker",
                model_version="campplus",
                model_member=(
                    "portable/voice-models/"
                    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
                ),
                vad_member="portable/voice-models/silero_vad.onnx",
                active=True,
            ),
            RuntimeModel(
                lock_name="runtime-lock.wespeaker.json",
                model_name="wespeaker",
                model_version="zh-cnceleb-resnet34",
                model_member=(
                    "portable/voice-models/"
                    "wespeaker_zh_cnceleb_resnet34.onnx"
                ),
                vad_member="portable/voice-models/silero_vad.onnx",
                active=False,
            ),
        ),
    )
    manifest = TransferManifest(
        schema=SCHEMA,
        bundle_id="0" * 64,
        created_at_utc="2026-08-29T03:04:05.000000Z",
        repository_url="https://github.com/example/project.git",
        branch="feature/presence-verification",
        commit_sha="1" * 40,
        source_tree_clean=True,
        remote_verified=True,
        schedule_local_time="06:00",
        runtime_rebuild_required=True,
        credential_registration_required=True,
        schedule_install_required=True,
        operator_state_destination=OPERATOR_STATE_DESTINATION,
        database=snapshot.database,
        runtime=runtime,
        members=tuple(sorted(members, key=lambda item: item.path.casefold())),
    )
    manifest = replace(manifest, bundle_id=compute_bundle_id(manifest))
    builder = FakeRuntimeBuilder()
    fixture = ImportedTransferFixture(data_root, manifest, builder)
    tool_hashes = {
        PurePosixPath(member.path).name: member.sha256
        for member in manifest.members
        if member.role == "runtime-tool"
    }
    sherpa_hash = next(
        member.sha256
        for member in manifest.members
        if member.role == "runtime-wheel"
        and PurePosixPath(member.path).name.startswith("sherpa_onnx-1.13.4-")
    )
    allowlists = RuntimeAllowlists(
        yt_dlp_sha256=tool_hashes["yt-dlp.exe"],
        deno_sha256=tool_hashes["deno.exe"],
        sherpa_wheel_sha256=sherpa_hash,
    )

    def attest_with_fixture_allowlists(
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
        attest_with_fixture_allowlists,
    )
    return fixture


def assert_runtime_error(pattern: str, callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code.startswith(pattern)


def test_rebuild_creates_offline_runtime_and_attests_every_lock(
    imported_transfer: ImportedTransferFixture,
) -> None:
    result = rebuild_voice_runtime(
        imported_transfer.request(),
        imported_transfer.dependencies(),
    )

    runtime_root = imported_transfer.data_root / "voice-runtime"
    assert result.runtime_root == runtime_root
    assert result.active_lock == "runtime-lock.json"
    assert {item.lock_name for item in result.attestations} == {
        "runtime-lock.json",
        "runtime-lock.campplus.json",
        "runtime-lock.wespeaker.json",
    }
    assert (runtime_root / "startup-manifest.json").is_file()
    assert imported_transfer.builder.network_attempts == []
    assert len(imported_transfer.builder.commands) == 2
    for item in result.attestations:
        verify_runtime_startup(item.attestation, imported_transfer.data_root)


def test_rebuild_refuses_existing_runtime_without_modifying_it(
    imported_transfer: ImportedTransferFixture,
) -> None:
    runtime_root = imported_transfer.data_root / "voice-runtime"
    runtime_root.mkdir()
    marker = runtime_root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    assert_runtime_error(
        "PC_TRANSFER_RUNTIME_EXISTS",
        lambda: rebuild_voice_runtime(
            imported_transfer.request(),
            imported_transfer.dependencies(),
        ),
    )
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "mutation",
    ("wrong_python_version", "changed_wheel", "missing_project_wheel", "pip_failure"),
)
def test_rebuild_fails_closed_and_never_uses_network(
    imported_transfer: ImportedTransferFixture,
    mutation: str,
) -> None:
    if mutation == "wrong_python_version":
        imported_transfer.builder.python_version = "3.14.7"
    elif mutation == "changed_wheel":
        member = imported_transfer.member("runtime-wheel")
        member_destination(
            imported_transfer.data_root,
            member.path,
            member.role,
        ).write_bytes(b"changed")
    elif mutation == "missing_project_wheel":
        member = imported_transfer.member("project-wheel")
        member_destination(
            imported_transfer.data_root,
            member.path,
            member.role,
        ).unlink()
    else:
        imported_transfer.builder.fail_pip = True

    assert_runtime_error(
        "PC_TRANSFER_RUNTIME_",
        lambda: rebuild_voice_runtime(
            imported_transfer.request(),
            imported_transfer.dependencies(),
        ),
    )
    assert imported_transfer.builder.network_attempts == []
    runtime_root = imported_transfer.data_root / "voice-runtime"
    if mutation == "pip_failure":
        assert runtime_root.is_dir()
    else:
        assert not runtime_root.exists()


def test_verify_rebuilt_runtime_is_read_only_and_detects_model_change(
    imported_transfer: ImportedTransferFixture,
) -> None:
    request = imported_transfer.request()
    dependencies = imported_transfer.dependencies()
    rebuild_voice_runtime(request, dependencies)
    before = {
        path: path.stat().st_mtime_ns
        for path in imported_transfer.data_root.rglob("*")
        if path.is_file()
    }

    verified = verify_rebuilt_runtime(request, dependencies)

    after = {path: path.stat().st_mtime_ns for path in before}
    assert len(verified.attestations) == 3
    assert after == before
    model = imported_transfer.data_root / "voice-models/silero_vad.onnx"
    model.write_bytes(b"changed")
    assert_runtime_error(
        "PC_TRANSFER_RUNTIME_INVALID",
        lambda: verify_rebuilt_runtime(request, dependencies),
    )
