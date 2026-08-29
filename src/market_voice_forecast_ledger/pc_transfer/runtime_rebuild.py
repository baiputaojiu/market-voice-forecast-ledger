"""Offline reconstruction and attestation of a transferred voice runtime."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import venv
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.common import canonical_json
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.bundle import imported_member_path
from market_voice_forecast_ledger.pc_transfer.manifest import (
    BundleMember,
    TransferManifest,
    encode_manifest,
)
from market_voice_forecast_ledger.pc_transfer.portable import (
    EXPECTED_REQUIREMENT_LINES,
)
from market_voice_forecast_ledger.pc_transfer.snapshot import (
    validate_database_snapshot,
)
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAttestation,
    VersionProbe,
    attest_runtime,
    verify_runtime_startup,
)


_BLOCK_BYTES = 1024 * 1024
_PROXY_AND_INDEX_KEYS = (
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
)


@dataclass(frozen=True, slots=True)
class RuntimeRebuildRequest:
    data_root: Path
    manifest: TransferManifest


@dataclass(frozen=True, slots=True)
class RebuiltAttestation:
    lock_name: str
    attestation: RuntimeAttestation


@dataclass(frozen=True, slots=True)
class RuntimeRebuildResult:
    runtime_root: Path
    active_lock: str
    attestations: tuple[RebuiltAttestation, ...]


class VenvBuilder(Protocol):
    def __call__(self, destination: Path) -> None: ...


class ProcessResult(Protocol):
    returncode: int


RuntimeProcessRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str]],
    ProcessResult,
]


def build_venv(destination: Path) -> None:
    venv.EnvBuilder(
        with_pip=True,
        clear=False,
        symlinks=False,
    ).create(destination)


def probe_version(command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _runtime_error() from exc
    if completed.returncode != 0:
        raise _runtime_error()
    return (completed.stdout or completed.stderr).strip()


def run_offline_process(
    command: tuple[str, ...],
    working_directory: Path,
    environment: Mapping[str, str],
) -> ProcessResult:
    return subprocess.run(
        command,
        cwd=working_directory,
        env=dict(environment),
        shell=False,
        check=False,
        capture_output=True,
        timeout=600,
    )


@dataclass(frozen=True, slots=True)
class RuntimeRebuildDependencies:
    venv_builder: VenvBuilder = build_venv
    process_runner: RuntimeProcessRunner = run_offline_process
    version_probe: VersionProbe = probe_version


def _runtime_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_RUNTIME_INVALID",
        "transferred voice runtime is invalid",
    )


def _runtime_exists_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_RUNTIME_EXISTS",
        "transferred voice runtime already exists",
    )


def _runtime_incomplete_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_RUNTIME_INCOMPLETE",
        "transferred voice runtime rebuild is incomplete",
    )


def _is_reparse(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        path.is_symlink()
        or bool(is_junction(path))
        or bool(attributes & reparse_flag)
    )


def _reject_reparse(path: Path) -> None:
    if _is_reparse(path):
        raise _runtime_error()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _member_by_role(
    manifest: TransferManifest,
    role: str,
) -> tuple[BundleMember, ...]:
    return tuple(member for member in manifest.members if member.role == role)


def _require_member_file(
    data_root: Path,
    member: BundleMember,
) -> Path:
    source = imported_member_path(data_root, member).absolute()
    try:
        relative = source.relative_to(data_root)
    except ValueError:
        raise _runtime_error() from None
    current = data_root
    for component in relative.parts:
        current = current / component
        _reject_reparse(current)
    candidate = source.resolve(strict=True)
    try:
        candidate.relative_to(data_root)
    except ValueError:
        raise _runtime_error() from None
    if (
        not candidate.is_file()
        or candidate.stat().st_size != member.size_bytes
        or _file_sha256(candidate) != member.sha256
    ):
        raise _runtime_error()
    return candidate


def _member_hash(manifest: TransferManifest, member_path: str) -> str:
    matches = tuple(
        member.sha256
        for member in manifest.members
        if member.path == member_path
    )
    if len(matches) != 1:
        raise _runtime_error()
    return matches[0]


def _model_path(
    data_root: Path,
    manifest: TransferManifest,
    member_path: str,
) -> Path:
    matches = tuple(
        member
        for member in manifest.members
        if member.path == member_path and member.role == "model"
    )
    if len(matches) != 1:
        raise _runtime_error()
    return _require_member_file(data_root, matches[0])


def _sherpa_wheel_hash(manifest: TransferManifest) -> str:
    expected_prefix = f"sherpa_onnx-{manifest.runtime.sherpa_onnx_version}-"
    matches = tuple(
        member.sha256
        for member in manifest.members
        if member.role == "runtime-wheel"
        and PurePosixPath(member.path).name.startswith(expected_prefix)
    )
    if len(matches) != 1:
        raise _runtime_error()
    return matches[0]


def _validate_dependencies(dependencies: RuntimeRebuildDependencies) -> None:
    if (
        type(dependencies) is not RuntimeRebuildDependencies
        or not callable(dependencies.venv_builder)
        or not callable(dependencies.process_runner)
        or not callable(dependencies.version_probe)
    ):
        raise _runtime_error()


def _validate_imported_inputs(
    request: RuntimeRebuildRequest,
    dependencies: RuntimeRebuildDependencies,
    *,
    require_runtime: bool,
) -> tuple[Path, Path]:
    if (
        type(request) is not RuntimeRebuildRequest
        or not isinstance(request.data_root, Path)
        or type(request.manifest) is not TransferManifest
    ):
        raise _runtime_error()
    _validate_dependencies(dependencies)
    try:
        encode_manifest(request.manifest)
        data_root = request.data_root.resolve(strict=True)
        if not data_root.is_dir():
            raise _runtime_error()
        _reject_reparse(data_root)
        runtime_root = data_root / "voice-runtime"
        if require_runtime:
            if (
                not runtime_root.is_dir()
                or _is_reparse(runtime_root)
            ):
                raise _runtime_error()
        elif runtime_root.exists() or runtime_root.is_symlink():
            raise _runtime_exists_error()
        validate_database_snapshot(
            data_root / "ledger.sqlite3",
            request.manifest.database,
        )
        requirements_members = _member_by_role(
            request.manifest,
            "runtime-requirements",
        )
        runtime_wheels = _member_by_role(request.manifest, "runtime-wheel")
        project_wheels = _member_by_role(request.manifest, "project-wheel")
        tools = _member_by_role(request.manifest, "runtime-tool")
        if (
            len(requirements_members) != 1
            or PurePosixPath(requirements_members[0].path).name
            != "requirements-runtime.txt"
            or len(runtime_wheels) != 7
            or any(
                PurePosixPath(member.path).name.startswith(
                    "market_voice_forecast_ledger-"
                )
                for member in runtime_wheels
            )
            or len(project_wheels) != 1
            or {
                PurePosixPath(member.path).name for member in tools
            }
            != {"deno.exe", "ffmpeg.exe", "yt-dlp.exe"}
        ):
            raise _runtime_error()
        for member in request.manifest.members:
            if member.role not in {"database", "operator-state"}:
                _require_member_file(data_root, member)
        requirements = _require_member_file(
            data_root,
            requirements_members[0],
        )
        if tuple(
            requirements.read_text(
                encoding="utf-8",
                errors="strict",
            ).splitlines()
        ) != EXPECTED_REQUIREMENT_LINES:
            raise _runtime_error()
        for model in request.manifest.runtime.models:
            _model_path(data_root, request.manifest, model.model_member)
            _model_path(data_root, request.manifest, model.vad_member)
        expected_python = f"Python {request.manifest.runtime.python_version}"
        if dependencies.version_probe((sys.executable, "--version")) != expected_python:
            raise _runtime_error()
        return data_root, runtime_root
    except DomainError as exc:
        if exc.code in {
            "PC_TRANSFER_RUNTIME_INVALID",
            "PC_TRANSFER_RUNTIME_EXISTS",
        }:
            raise
        raise _runtime_error() from None
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise _runtime_error() from exc


def _offline_install_commands(
    runtime_python: Path,
    wheelhouse: Path,
    requirements: Path,
    project_wheel: Path,
) -> tuple[tuple[str, ...], ...]:
    return (
        (
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "-r",
            str(requirements),
        ),
        (
            str(runtime_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            str(project_wheel),
        ),
    )


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in _PROXY_AND_INDEX_KEYS:
        environment.pop(key, None)
    environment["PIP_NO_INDEX"] = "1"
    return environment


def _reject_startup_hook(relative: str) -> None:
    name = relative.rsplit("/", 1)[-1].casefold()
    if (
        name.endswith(".pth")
        or name.endswith("._pth")
        or name.startswith("sitecustomize.")
        or name.startswith("usercustomize.")
    ):
        raise _runtime_error()


def _startup_inventory(import_root: Path) -> tuple[tuple[str, str], ...]:
    files: list[tuple[str, str]] = []
    for current, directories, filenames in os.walk(
        import_root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        _reject_reparse(current_path)
        directories.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for directory in directories:
            _reject_reparse(current_path / directory)
        for filename in filenames:
            candidate = current_path / filename
            _reject_reparse(candidate)
            if not candidate.is_file():
                raise _runtime_error()
            relative = candidate.relative_to(import_root).as_posix()
            _reject_startup_hook(relative)
            files.append((relative, _file_sha256(candidate)))
    result = tuple(sorted(files))
    names = {path for path, _ in result}
    if (
        "market_voice_forecast_ledger/voice/adapter_main.py" not in names
        or "sherpa_onnx/__init__.py" not in names
    ):
        raise _runtime_error()
    return result


def _write_exclusive_text(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _copy_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=_BLOCK_BYTES)


def _runtime_lock(
    data_root: Path,
    runtime_root: Path,
    runtime_python: Path,
    import_root: Path,
    startup_path: Path,
    manifest: TransferManifest,
    model: object,
) -> dict[str, object]:
    tools = {
        name: runtime_root / name
        for name in ("deno.exe", "ffmpeg.exe", "yt-dlp.exe")
    }
    return {
        "adapter_contract_version": manifest.runtime.adapter_contract_version,
        "deno": {
            "path": str(tools["deno.exe"]),
            "sha256": _file_sha256(tools["deno.exe"]),
            "version": manifest.runtime.deno_version,
        },
        "ffmpeg": {
            "path": str(tools["ffmpeg.exe"]),
            "sha256": _file_sha256(tools["ffmpeg.exe"]),
            "version": manifest.runtime.ffmpeg_version,
        },
        "model": {
            "name": model.model_name,
            "path": str(_model_path(data_root, manifest, model.model_member)),
            "sha256": _member_hash(manifest, model.model_member),
            "version": model.model_version,
        },
        "provider": manifest.runtime.provider,
        "python": {
            "path": str(runtime_python),
            "sha256": _file_sha256(runtime_python),
            "version": manifest.runtime.python_version,
        },
        "python_startup": {
            "import_root": str(import_root),
            "manifest_path": str(startup_path),
            "manifest_sha256": _file_sha256(startup_path),
            "pyvenv_path": str(runtime_root / "pyvenv.cfg"),
            "pyvenv_sha256": _file_sha256(runtime_root / "pyvenv.cfg"),
        },
        "sherpa_onnx": {
            "version": manifest.runtime.sherpa_onnx_version,
            "wheel_sha256": _sherpa_wheel_hash(manifest),
        },
        "vad": {
            "path": str(_model_path(data_root, manifest, model.vad_member)),
            "sha256": _member_hash(manifest, model.vad_member),
            "version": manifest.runtime.vad_version,
        },
        "vad_contract_version": manifest.runtime.vad_contract_version,
        "yt_dlp": {
            "path": str(tools["yt-dlp.exe"]),
            "sha256": _file_sha256(tools["yt-dlp.exe"]),
            "version": manifest.runtime.yt_dlp_version,
        },
    }


def rebuild_voice_runtime(
    request: RuntimeRebuildRequest,
    dependencies: RuntimeRebuildDependencies,
) -> RuntimeRebuildResult:
    runtime_root: Path | None = None
    try:
        data_root, runtime_root = _validate_imported_inputs(
            request,
            dependencies,
            require_runtime=False,
        )
        dependencies.venv_builder(runtime_root)
        if not runtime_root.is_dir() or _is_reparse(runtime_root):
            raise _runtime_error()
        runtime_python = runtime_root / "Scripts/python.exe"
        import_root = runtime_root / "Lib/site-packages"
        pyvenv_path = runtime_root / "pyvenv.cfg"
        for required in (runtime_python, pyvenv_path):
            if not required.is_file() or _is_reparse(required):
                raise _runtime_error()
        if not import_root.is_dir() or _is_reparse(import_root):
            raise _runtime_error()
        requirements_member = _member_by_role(
            request.manifest,
            "runtime-requirements",
        )[0]
        project_member = _member_by_role(request.manifest, "project-wheel")[0]
        requirements = _require_member_file(data_root, requirements_member)
        project_wheel = _require_member_file(data_root, project_member)
        wheelhouse = Settings.for_data_dir(data_root).voice_wheelhouse_dir
        environment = _offline_environment()
        for command in _offline_install_commands(
            runtime_python,
            wheelhouse,
            requirements,
            project_wheel,
        ):
            completed = dependencies.process_runner(
                command,
                data_root,
                environment,
            )
            if type(completed.returncode) is not int or completed.returncode != 0:
                raise _runtime_error()
        inventory = _startup_inventory(import_root)
        startup_path = runtime_root / "startup-manifest.json"
        startup_object = {
            "files": [
                {"path": relative, "sha256": digest}
                for relative, digest in inventory
            ]
        }
        _write_exclusive_text(
            startup_path,
            canonical_json(startup_object) + "\n",
        )
        tool_members = {
            PurePosixPath(member.path).name: member
            for member in _member_by_role(request.manifest, "runtime-tool")
        }
        for name, member in tool_members.items():
            source = _require_member_file(data_root, member)
            destination = runtime_root / name
            _copy_exclusive(source, destination)
            if _file_sha256(destination) != member.sha256:
                raise _runtime_error()
        active_locks: list[Path] = []
        for model in request.manifest.runtime.models:
            lock_path = runtime_root / model.lock_name
            lock = _runtime_lock(
                data_root,
                runtime_root,
                runtime_python,
                import_root,
                startup_path,
                request.manifest,
                model,
            )
            _write_exclusive_text(lock_path, canonical_json(lock) + "\n")
            if model.active:
                active_locks.append(lock_path)
        if len(active_locks) != 1:
            raise _runtime_error()
        _copy_exclusive(active_locks[0], runtime_root / "runtime-lock.json")
        return verify_rebuilt_runtime(request, dependencies)
    except DomainError as exc:
        if exc.code == "PC_TRANSFER_RUNTIME_EXISTS":
            raise
        if runtime_root is not None and runtime_root.exists():
            raise _runtime_incomplete_error() from exc
        if exc.code == "PC_TRANSFER_RUNTIME_INVALID":
            raise
        raise _runtime_error() from None
    except (
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        if runtime_root is not None and runtime_root.exists():
            raise _runtime_incomplete_error() from exc
        raise _runtime_error() from exc


def _require_attestations_match_manifest(
    request: RuntimeRebuildRequest,
    results: tuple[RebuiltAttestation, ...],
) -> None:
    data_root = request.data_root.resolve(strict=True)
    manifest = request.manifest
    runtime = manifest.runtime
    active_models = tuple(model for model in runtime.models if model.active)
    if len(active_models) != 1:
        raise _runtime_error()
    expected_models = {
        "runtime-lock.json": active_models[0],
        **{model.lock_name: model for model in runtime.models},
    }
    if {result.lock_name for result in results} != set(expected_models):
        raise _runtime_error()
    tool_hashes = {
        "deno.exe": _member_hash(
            manifest,
            "portable/voice-install/deno.exe",
        ),
        "ffmpeg.exe": _member_hash(
            manifest,
            "portable/voice-install/ffmpeg.exe",
        ),
        "yt-dlp.exe": _member_hash(
            manifest,
            "portable/voice-install/yt-dlp.exe",
        ),
    }
    runtime_root = data_root / "voice-runtime"
    for result in results:
        model = expected_models[result.lock_name]
        item = result.attestation
        expected = (
            runtime.python_version,
            runtime.sherpa_onnx_version,
            runtime.yt_dlp_version,
            runtime.deno_version,
            runtime.ffmpeg_version,
            runtime.vad_version,
            runtime.provider,
            runtime.adapter_contract_version,
            runtime.vad_contract_version,
            model.model_name,
            model.model_version,
            _model_path(data_root, manifest, model.model_member),
            _model_path(data_root, manifest, model.vad_member),
            _member_hash(manifest, model.model_member),
            _member_hash(manifest, model.vad_member),
            runtime_root / "deno.exe",
            runtime_root / "ffmpeg.exe",
            runtime_root / "yt-dlp.exe",
            tool_hashes["deno.exe"],
            tool_hashes["ffmpeg.exe"],
            tool_hashes["yt-dlp.exe"],
            _sherpa_wheel_hash(manifest),
        )
        actual = (
            item.python_version,
            item.sherpa_onnx_version,
            item.yt_dlp_version,
            item.deno_version,
            item.ffmpeg_version,
            item.vad_version,
            item.provider,
            item.adapter_contract_version,
            item.vad_contract_version,
            item.model_name,
            item.model_version,
            item.model_path,
            item.vad_path,
            item.model_sha256,
            item.vad_sha256,
            item.deno_path,
            item.ffmpeg_path,
            item.yt_dlp_path,
            item.deno_sha256,
            item.ffmpeg_sha256,
            item.yt_dlp_sha256,
            item.sherpa_wheel_sha256,
        )
        if actual != expected:
            raise _runtime_error()


def verify_rebuilt_runtime(
    request: RuntimeRebuildRequest,
    dependencies: RuntimeRebuildDependencies,
) -> RuntimeRebuildResult:
    try:
        data_root, runtime_root = _validate_imported_inputs(
            request,
            dependencies,
            require_runtime=True,
        )
        settings = Settings.for_data_dir(data_root)
        active = attest_runtime(
            settings,
            version_probe=dependencies.version_probe,
        )
        candidates = tuple(
            RebuiltAttestation(
                lock_name=model.lock_name,
                attestation=attest_runtime(
                    settings,
                    version_probe=dependencies.version_probe,
                    lock_name=model.lock_name,
                ),
            )
            for model in request.manifest.runtime.models
        )
        results = (RebuiltAttestation("runtime-lock.json", active), *candidates)
        _require_attestations_match_manifest(request, results)
        for result in results:
            verify_runtime_startup(result.attestation, data_root)
        return RuntimeRebuildResult(
            runtime_root=runtime_root,
            active_lock="runtime-lock.json",
            attestations=results,
        )
    except DomainError as exc:
        if exc.code == "PC_TRANSFER_RUNTIME_INVALID":
            raise
        raise _runtime_error() from None
    except (OSError, TypeError, ValueError) as exc:
        raise _runtime_error() from exc


__all__ = [
    "RebuiltAttestation",
    "RuntimeRebuildDependencies",
    "RuntimeRebuildRequest",
    "RuntimeRebuildResult",
    "build_venv",
    "probe_version",
    "rebuild_voice_runtime",
    "run_offline_process",
    "verify_rebuilt_runtime",
]
