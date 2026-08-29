import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.pc_transfer import portable
from market_voice_forecast_ledger.pc_transfer.portable import (
    collect_portable_inventory,
)


DEPENDENCY_WHEELS = (
    "annotated_types-0.8.0-py3-none-any.whl",
    "pydantic-2.13.4-py3-none-any.whl",
    "pydantic_core-2.46.4-cp314-cp314-win_amd64.whl",
    "sherpa_onnx-1.13.4-cp314-cp314-win_amd64.whl",
    "sherpa_onnx_core-1.13.4-py3-none-win_amd64.whl",
    "typing_extensions-4.16.0-py3-none-any.whl",
    "typing_inspection-0.4.4-py3-none-any.whl",
)
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
EXPECTED_MEMBER_PATHS = {
    "portable/voice-models/"
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "portable/voice-models/silero_vad.onnx",
    "portable/voice-models/wespeaker_zh_cnceleb_resnet34.onnx",
    "portable/voice-wheelhouse/requirements-runtime.txt",
    "portable/voice-install/deno.exe",
    "portable/voice-install/ffmpeg.exe",
    "portable/voice-install/yt-dlp.exe",
    "operator-state/presence-verification/progress.md",
}


def write_file(path: Path, body: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


class SuccessfulBuildRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        working_directory: Path,
    ) -> SimpleNamespace:
        assert working_directory.is_dir()
        self.commands.append(command)
        if "--wheel-dir" in command:
            wheel_dir = Path(command[command.index("--wheel-dir") + 1])
            write_file(
                wheel_dir
                / "market_voice_forecast_ledger-0.1.0-py3-none-any.whl",
                b"new project wheel",
            )
        return SimpleNamespace(returncode=0)


@dataclass
class PortableSourceFixture:
    settings: Settings
    repository_root: Path
    operator_state_dir: Path
    commit_sha: str
    runner: SuccessfulBuildRunner
    attestations: dict[str, SimpleNamespace]
    deno_path: Path


@pytest.fixture
def portable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> PortableSourceFixture:
    settings = Settings.for_data_dir(tmp_path / "private-data")
    settings.data_dir.mkdir(parents=True)
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    operator_state_dir = tmp_path / "operator-state"
    write_file(operator_state_dir / "progress.md", b"# Current progress\n")
    write_file(operator_state_dir / "rulings.md", b"# Rulings\n")

    model_names = {
        "runtime-lock.campplus.json": (
            "3dspeaker",
            "campplus",
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        ),
        "runtime-lock.wespeaker.json": (
            "wespeaker",
            "zh-cnceleb-resnet34",
            "wespeaker_zh_cnceleb_resnet34.onnx",
        ),
    }
    model_hashes: dict[str, str] = {}
    for lock_name, (_, _, filename) in model_names.items():
        model_hashes[lock_name] = write_file(
            settings.voice_model_dir / filename,
            f"model:{lock_name}".encode(),
        )
    vad_path = settings.voice_model_dir / "silero_vad.onnx"
    vad_hash = write_file(vad_path, b"fixed vad")

    runtime_tools = {
        "deno": ("deno.exe", b"deno"),
        "ffmpeg": ("ffmpeg.exe", b"ffmpeg"),
        "yt_dlp": ("yt-dlp.exe", b"yt-dlp"),
    }
    runtime_tool_paths: dict[str, Path] = {}
    runtime_tool_hashes: dict[str, str] = {}
    for name, (filename, body) in runtime_tools.items():
        path = settings.voice_runtime_dir / filename
        runtime_tool_paths[name] = path
        runtime_tool_hashes[name] = write_file(path, body)

    deno_path = settings.voice_work_dir / "install/deno-2.9.5/deno.exe"
    write_file(deno_path, b"deno")
    write_file(
        settings.voice_work_dir
        / "install/ffmpeg-9.0.1/"
        "ffmpeg-9.0.1-essentials_build/bin/ffmpeg.exe",
        b"ffmpeg",
    )
    write_file(settings.voice_work_dir / "install/yt-dlp.exe", b"yt-dlp")

    wheelhouse = settings.data_dir / "voice-wheelhouse"
    for filename in DEPENDENCY_WHEELS:
        write_file(wheelhouse / filename, f"wheel:{filename}".encode())
    write_file(
        wheelhouse / "requirements-runtime.txt",
        ("\n".join(EXPECTED_REQUIREMENT_LINES) + "\n").encode(),
    )

    attestations: dict[str, SimpleNamespace] = {}
    for lock_name, (model_name, model_version, filename) in model_names.items():
        attestations[lock_name] = SimpleNamespace(
            python_version="3.14.6",
            sherpa_onnx_version="1.13.4",
            yt_dlp_version="2026.08.19",
            deno_version="2.9.5",
            ffmpeg_version="9.0.1",
            vad_version="silero-vad-v5",
            provider="CPUExecutionProvider",
            adapter_contract_version="voice-adapter-v1",
            vad_contract_version="vad-v1",
            model_path=(settings.voice_model_dir / filename).resolve(),
            model_sha256=model_hashes[lock_name],
            model_name=model_name,
            model_version=model_version,
            vad_path=vad_path.resolve(),
            vad_sha256=vad_hash,
            deno_path=runtime_tool_paths["deno"].resolve(),
            deno_sha256=runtime_tool_hashes["deno"],
            ffmpeg_path=runtime_tool_paths["ffmpeg"].resolve(),
            ffmpeg_sha256=runtime_tool_hashes["ffmpeg"],
            yt_dlp_path=runtime_tool_paths["yt_dlp"].resolve(),
            yt_dlp_sha256=runtime_tool_hashes["yt_dlp"],
        )
    attestations["runtime-lock.json"] = attestations[
        "runtime-lock.campplus.json"
    ]

    def fake_attest_runtime(
        candidate_settings: Settings,
        *,
        version_probe,
        lock_name: str = "runtime-lock.json",
    ) -> SimpleNamespace:
        assert candidate_settings == settings
        assert callable(version_probe)
        return attestations[lock_name]

    monkeypatch.setattr(portable, "attest_runtime", fake_attest_runtime)
    return PortableSourceFixture(
        settings=settings,
        repository_root=repository_root,
        operator_state_dir=operator_state_dir,
        commit_sha="1" * 40,
        runner=SuccessfulBuildRunner(),
        attestations=attestations,
        deno_path=deno_path,
    )


def collect_fixture_inventory(
    fixture: PortableSourceFixture,
    tmp_path: Path,
):
    return collect_portable_inventory(
        settings=fixture.settings,
        repository_root=fixture.repository_root,
        expected_commit=fixture.commit_sha,
        operator_state_dir=fixture.operator_state_dir,
        build_dir=tmp_path / "build",
        version_probe=lambda _command: "unused",
        runner=fixture.runner,
    )


def assert_portable_invalid(callable_under_test) -> None:
    with pytest.raises(DomainError) as error:
        callable_under_test()
    assert error.value.code == "PC_TRANSFER_PORTABLE_INVALID"


def test_inventory_contains_only_rebuild_inputs(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    inventory = collect_fixture_inventory(portable_source, tmp_path)

    paths = {item.path for item in inventory.files}
    assert EXPECTED_MEMBER_PATHS <= paths
    assert sum(path.endswith(".whl") for path in paths) == 8
    assert not any("voice-runtime" in path for path in paths)
    assert not any("archive" in path for path in paths)
    assert not any("task11-work" in path for path in paths)
    assert not any("project-wheel-old" in path for path in paths)
    assert len(
        [item for item in inventory.files if item.role == "project-wheel"]
    ) == 1
    assert len([model for model in inventory.runtime.models if model.active]) == 1
    assert tuple(item.path for item in inventory.files) == tuple(
        sorted(paths, key=str.casefold)
    )


def test_inventory_rejects_reparse_or_symlink_source(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = portable_source.settings.voice_model_dir / "silero_vad.onnx"
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original(path),
    )

    assert_portable_invalid(
        lambda: collect_fixture_inventory(portable_source, tmp_path)
    )


def test_inventory_ignores_redundant_and_stale_install_material(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    forbidden = (
        portable_source.settings.voice_work_dir
        / "install/project-wheel-old/old.whl"
    )
    write_file(forbidden, b"old")
    write_file(portable_source.settings.data_dir / "archive/old.sqlite3", b"old")

    inventory = collect_fixture_inventory(portable_source, tmp_path)

    sources = {item.source for item in inventory.files}
    assert forbidden.resolve() not in sources
    assert not any("archive" in source.parts for source in sources)


def test_inventory_excludes_nonportable_operator_artifacts(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    excluded = (
        portable_source.operator_state_dir / "__pycache__/state.pyc",
        portable_source.operator_state_dir / ".codex/session.sqlite3",
        portable_source.operator_state_dir / "logs/review.log",
        portable_source.operator_state_dir / "temp-audio/chunk.wav",
        portable_source.operator_state_dir / "archive/old.md",
        portable_source.operator_state_dir / "scratch.tmp",
    )
    for path in excluded:
        write_file(path, b"excluded")

    inventory = collect_fixture_inventory(portable_source, tmp_path)

    sources = {item.source for item in inventory.files}
    assert not sources.intersection(path.resolve() for path in excluded)
    assert (
        portable_source.operator_state_dir / "progress.md"
    ).resolve() in sources


def test_inventory_rejects_runtime_tool_hash_drift(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    portable_source.deno_path.write_bytes(b"changed")

    assert_portable_invalid(
        lambda: collect_fixture_inventory(portable_source, tmp_path)
    )


def test_inventory_rejects_requirements_drift(
    portable_source: PortableSourceFixture,
    tmp_path: Path,
) -> None:
    requirements = (
        portable_source.settings.data_dir
        / "voice-wheelhouse/requirements-runtime.txt"
    )
    requirements.write_text("pydantic==latest\n", encoding="utf-8")

    assert_portable_invalid(
        lambda: collect_fixture_inventory(portable_source, tmp_path)
    )
