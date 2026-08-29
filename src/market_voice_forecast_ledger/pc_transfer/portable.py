"""Finite inventory of files needed to rebuild the private voice runtime."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer.manifest import (
    EXACT_PROJECT_WHEEL_MEMBER,
    EXACT_RUNTIME_WHEEL_MEMBERS,
    RuntimeModel,
    RuntimeSummary,
)
from market_voice_forecast_ledger.voice.runtime import (
    VersionProbe,
    attest_runtime,
)


MODEL_FILES = (
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "silero_vad.onnx",
    "wespeaker_zh_cnceleb_resnet34.onnx",
)
CANDIDATE_LOCKS = (
    "runtime-lock.campplus.json",
    "runtime-lock.wespeaker.json",
)
TOOL_FILES = (
    (
        Path("voice-work/install/deno-2.9.5/deno.exe"),
        "portable/voice-install/deno.exe",
    ),
    (
        Path(
            "voice-work/install/ffmpeg-9.0.1/"
            "ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe"
        ),
        "portable/voice-install/ffmpeg.exe",
    ),
    (
        Path("voice-work/install/yt-dlp.exe"),
        "portable/voice-install/yt-dlp.exe",
    ),
)
MAX_PORTABLE_FILES = 20_000
MAX_PORTABLE_BYTES = 4 * 1024 * 1024 * 1024
EXPECTED_REQUIREMENT_LINES = (
    "annotated-types==0.8.0 --hash=sha256:"
    "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0",
    "pydantic==2.13.4 --hash=sha256:"
    "45a282cde31d808236fd7ea9d919b128653c8b38b393d1c4ab335c62924d9aba",
    "pydantic-core==2.46.4 --hash=sha256:"
    "811ff8e9c313ab425368bcbb36e5c4ebd7108c2bbf4e4089cfbb0b01eff63fac",
    "typing-extensions==4.16.0 --hash=sha256:"
    "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
    "typing-inspection==0.4.4 --hash=sha256:"
    "65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147",
    "sherpa-onnx==1.13.4 --hash=sha256:"
    "cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06",
    "sherpa-onnx-core==1.13.4 --hash=sha256:"
    "0a6949cf0fd83adb9fbcfdf5c27b8907a57f7b48626db703c7f6037be9b61764",
)


@dataclass(frozen=True, slots=True)
class PortableFile:
    source: Path
    path: str
    role: str


@dataclass(frozen=True, slots=True)
class PortableInventory:
    files: tuple[PortableFile, ...]
    runtime: RuntimeSummary


class ProcessResult(Protocol):
    returncode: int


ProcessRunner = Callable[[tuple[str, ...], Path], ProcessResult]


def run_process(
    command: tuple[str, ...],
    working_directory: Path,
) -> ProcessResult:
    environment = os.environ.copy()
    return subprocess.run(
        command,
        cwd=working_directory,
        env=environment,
        shell=False,
        check=False,
        capture_output=True,
        timeout=300,
    )


def _portable_error() -> DomainError:
    return DomainError(
        "PC_TRANSFER_PORTABLE_INVALID",
        "portable transfer input is invalid",
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


def _require_regular_source(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir() or _is_reparse(resolved_root):
        raise _portable_error()
    raw = path.absolute()
    try:
        relative = raw.relative_to(resolved_root)
    except ValueError:
        raise _portable_error() from None
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if _is_reparse(current):
            raise _portable_error()
    candidate = raw.resolve(strict=True)
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise _portable_error() from None
    if not candidate.is_file():
        raise _portable_error()
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_project_wheel(
    repository_root: Path,
    build_root: Path,
    expected_commit: str,
    runner: ProcessRunner = run_process,
) -> Path:
    try:
        if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
            raise _portable_error()
        repository = repository_root.resolve(strict=True)
        if not repository.is_dir():
            raise _portable_error()
        build_root.mkdir(parents=True, exist_ok=False)
        source_root = build_root / "source"
        output_dir = build_root / "wheel"
        output_dir.mkdir()
        commands = (
            (
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(repository),
                str(source_root),
            ),
            (
                "git",
                "-C",
                str(source_root),
                "checkout",
                "--quiet",
                "--detach",
                expected_commit,
            ),
            (
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(output_dir),
                str(source_root),
            ),
        )
        for command in commands:
            completed = runner(command, build_root)
            if type(completed.returncode) is not int or completed.returncode != 0:
                raise _portable_error()
        wheels = tuple(output_dir.glob("market_voice_forecast_ledger-*.whl"))
        all_outputs = tuple(output_dir.iterdir())
        if (
            len(wheels) != 1
            or len(all_outputs) != 1
            or all_outputs[0] != wheels[0]
            or wheels[0].name != Path(EXACT_PROJECT_WHEEL_MEMBER).name
        ):
            raise _portable_error()
        return _require_regular_source(wheels[0], output_dir)
    except DomainError:
        raise
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise _portable_error() from exc


def _runtime_summary(
    settings: Settings,
    version_probe: VersionProbe,
) -> tuple[RuntimeSummary, tuple[tuple[str, object], ...]]:
    active = attest_runtime(settings, version_probe=version_probe)
    candidates = tuple(
        (
            lock_name,
            attest_runtime(
                settings,
                version_probe=version_probe,
                lock_name=lock_name,
            ),
        )
        for lock_name in CANDIDATE_LOCKS
    )
    active_matches = tuple(
        item.model_sha256 == active.model_sha256
        and item.model_name == active.model_name
        and item.model_version == active.model_version
        and item.vad_sha256 == active.vad_sha256
        and item.adapter_contract_version == active.adapter_contract_version
        and item.vad_contract_version == active.vad_contract_version
        for _, item in candidates
    )
    if active_matches.count(True) != 1:
        raise _portable_error()
    shared = (
        active.python_version,
        active.sherpa_onnx_version,
        active.yt_dlp_version,
        active.deno_version,
        active.ffmpeg_version,
        active.vad_version,
        active.provider,
        active.adapter_contract_version,
        active.vad_contract_version,
        active.yt_dlp_sha256,
        active.deno_sha256,
        active.ffmpeg_sha256,
        active.vad_sha256,
    )
    for _, item in candidates:
        if (
            item.python_version,
            item.sherpa_onnx_version,
            item.yt_dlp_version,
            item.deno_version,
            item.ffmpeg_version,
            item.vad_version,
            item.provider,
            item.adapter_contract_version,
            item.vad_contract_version,
            item.yt_dlp_sha256,
            item.deno_sha256,
            item.ffmpeg_sha256,
            item.vad_sha256,
        ) != shared:
            raise _portable_error()
    models = tuple(
        RuntimeModel(
            lock_name=lock_name,
            model_name=item.model_name,
            model_version=item.model_version,
            model_member=f"portable/voice-models/{item.model_path.name}",
            vad_member=f"portable/voice-models/{item.vad_path.name}",
            active=is_active,
        )
        for (lock_name, item), is_active in zip(
            candidates,
            active_matches,
            strict=True,
        )
    )
    return (
        RuntimeSummary(
            python_version=active.python_version,
            sherpa_onnx_version=active.sherpa_onnx_version,
            yt_dlp_version=active.yt_dlp_version,
            deno_version=active.deno_version,
            ffmpeg_version=active.ffmpeg_version,
            vad_version=active.vad_version,
            provider=active.provider,
            adapter_contract_version=active.adapter_contract_version,
            vad_contract_version=active.vad_contract_version,
            models=models,
        ),
        candidates,
    )


def _operator_files(operator_state_dir: Path) -> tuple[PortableFile, ...]:
    root = operator_state_dir.resolve(strict=True)
    if not root.is_dir() or _is_reparse(root):
        raise _portable_error()
    files: list[PortableFile] = []
    for current, directories, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        if _is_reparse(current_path):
            raise _portable_error()
        for directory in directories:
            if _is_reparse(current_path / directory):
                raise _portable_error()
        for filename in filenames:
            candidate = _require_regular_source(current_path / filename, root)
            relative = candidate.relative_to(root).as_posix()
            files.append(
                PortableFile(
                    source=candidate,
                    path=f"operator-state/presence-verification/{relative}",
                    role="operator-state",
                )
            )
    if not any(
        item.path == "operator-state/presence-verification/progress.md"
        for item in files
    ):
        raise _portable_error()
    return tuple(files)


def collect_portable_inventory(
    settings: Settings,
    repository_root: Path,
    expected_commit: str,
    operator_state_dir: Path,
    build_dir: Path,
    version_probe: VersionProbe,
    runner: ProcessRunner = run_process,
) -> PortableInventory:
    try:
        if not isinstance(settings, Settings) or not callable(version_probe):
            raise _portable_error()
        data_root = settings.data_dir.resolve(strict=True)
        if not data_root.is_dir() or _is_reparse(data_root):
            raise _portable_error()
        runtime, candidates = _runtime_summary(settings, version_probe)
        files: list[PortableFile] = []

        model_sources: dict[str, Path] = {}
        for filename in MODEL_FILES:
            source = _require_regular_source(
                settings.voice_model_dir / filename,
                settings.voice_model_dir,
            )
            model_sources[filename] = source
            files.append(
                PortableFile(
                    source=source,
                    path=f"portable/voice-models/{filename}",
                    role="model",
                )
            )
        for lock_name, attestation in candidates:
            expected_filename = {
                "runtime-lock.campplus.json": MODEL_FILES[0],
                "runtime-lock.wespeaker.json": MODEL_FILES[2],
            }[lock_name]
            if (
                attestation.model_path != model_sources[expected_filename]
                or attestation.model_sha256
                != _file_sha256(model_sources[expected_filename])
                or attestation.vad_path != model_sources[MODEL_FILES[1]]
                or attestation.vad_sha256
                != _file_sha256(model_sources[MODEL_FILES[1]])
            ):
                raise _portable_error()

        wheelhouse = data_root / "voice-wheelhouse"
        expected_wheel_names = {
            Path(path).name for path in EXACT_RUNTIME_WHEEL_MEMBERS
        }
        dependency_wheels = tuple(
            path
            for path in wheelhouse.glob("*.whl")
            if not path.name.startswith("market_voice_forecast_ledger-")
        )
        if {path.name for path in dependency_wheels} != expected_wheel_names:
            raise _portable_error()
        for candidate in dependency_wheels:
            source = _require_regular_source(candidate, wheelhouse)
            files.append(
                PortableFile(
                    source=source,
                    path=f"portable/voice-wheelhouse/{source.name}",
                    role="runtime-wheel",
                )
            )
        requirements = _require_regular_source(
            wheelhouse / "requirements-runtime.txt",
            wheelhouse,
        )
        requirements_text = requirements.read_text(
            encoding="utf-8",
            errors="strict",
        )
        if tuple(requirements_text.splitlines()) != EXPECTED_REQUIREMENT_LINES:
            raise _portable_error()
        files.append(
            PortableFile(
                source=requirements,
                path="portable/voice-wheelhouse/requirements-runtime.txt",
                role="runtime-requirements",
            )
        )

        portable_tools: dict[str, Path] = {}
        for relative, member_path in TOOL_FILES:
            source = _require_regular_source(data_root / relative, data_root)
            portable_tools[Path(member_path).name] = source
            files.append(
                PortableFile(
                    source=source,
                    path=member_path,
                    role="runtime-tool",
                )
            )
        for _, attestation in candidates:
            expected_hashes = (
                (
                    portable_tools["deno.exe"],
                    attestation.deno_sha256,
                ),
                (
                    portable_tools["ffmpeg.exe"],
                    attestation.ffmpeg_sha256,
                ),
                (
                    portable_tools["yt-dlp.exe"],
                    attestation.yt_dlp_sha256,
                ),
            )
            if any(
                _file_sha256(source) != expected_hash
                for source, expected_hash in expected_hashes
            ):
                raise _portable_error()

        project_wheel = build_project_wheel(
            repository_root,
            build_dir,
            expected_commit,
            runner,
        )
        files.append(
            PortableFile(
                source=project_wheel,
                path=EXACT_PROJECT_WHEEL_MEMBER,
                role="project-wheel",
            )
        )
        files.extend(_operator_files(operator_state_dir))
        canonical = tuple(sorted(files, key=lambda item: item.path.casefold()))
        folded_paths = {item.path.casefold() for item in canonical}
        total_bytes = sum(item.source.stat().st_size for item in canonical)
        if (
            not canonical
            or len(canonical) > MAX_PORTABLE_FILES
            or len(folded_paths) != len(canonical)
            or total_bytes > MAX_PORTABLE_BYTES
        ):
            raise _portable_error()
        return PortableInventory(files=canonical, runtime=runtime)
    except DomainError as exc:
        if exc.code == "PC_TRANSFER_PORTABLE_INVALID":
            raise
        raise _portable_error() from None
    except (
        OSError,
        UnicodeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        raise _portable_error() from exc


__all__ = [
    "PortableFile",
    "PortableInventory",
    "ProcessRunner",
    "build_project_wheel",
    "collect_portable_inventory",
    "run_process",
]
