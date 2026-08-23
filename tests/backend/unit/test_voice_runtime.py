import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError
from market_voice_forecast_ledger.voice import runtime
from market_voice_forecast_ledger.voice.runtime import (
    RuntimeAllowlists,
    attest_runtime,
    verify_runtime_startup,
)


def _write(path: Path, contents: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return hashlib.sha256(contents).hexdigest()


def _install_startup_inventory(
    settings: Settings, lock: dict[str, object]
) -> tuple[Path, tuple[tuple[str, str], ...]]:
    import_root = settings.voice_runtime_dir / "Lib" / "site-packages"
    files = {
        "market_voice_forecast_ledger/__init__.py": b"# installed project\n",
        "market_voice_forecast_ledger/voice/adapter_main.py": (
            b"# installed adapter\n"
        ),
        "sherpa_onnx/__init__.py": b"# installed pinned sherpa\n",
    }
    inventory = tuple(
        (relative, _write(import_root / Path(relative), contents))
        for relative, contents in sorted(files.items())
    )
    manifest = settings.voice_runtime_dir / "startup-manifest.json"
    manifest_sha256 = _write(
        manifest,
        json.dumps(
            {
                "files": [
                    {"path": relative, "sha256": digest}
                    for relative, digest in inventory
                ]
            },
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    pyvenv = settings.voice_runtime_dir / "pyvenv.cfg"
    pyvenv_sha256 = _write(
        pyvenv,
        b"home = C:/private-python\n"
        b"include-system-site-packages = false\n"
        b"version = 3.14.6\n",
    )
    lock["python_startup"] = {
        "import_root": str(import_root),
        "manifest_path": str(manifest),
        "manifest_sha256": manifest_sha256,
        "pyvenv_path": str(pyvenv),
        "pyvenv_sha256": pyvenv_sha256,
    }
    return import_root.resolve(), inventory


class _FakeProbe:
    def __init__(self, outputs: dict[tuple[str, ...], str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> str:
        self.calls.append(argv)
        return self.outputs[argv]


def _runtime_fixture(
    tmp_path: Path,
    *,
    provider: str = "CPUExecutionProvider",
    python_relative: Path = Path("python.exe"),
) -> tuple[Settings, _FakeProbe, RuntimeAllowlists]:
    settings = Settings.for_data_dir(tmp_path / "private-data")
    python = settings.voice_runtime_dir / python_relative
    yt_dlp = settings.voice_runtime_dir / "yt-dlp.exe"
    deno = settings.voice_runtime_dir / "deno.exe"
    ffmpeg = settings.voice_runtime_dir / "ffmpeg.exe"
    model = settings.voice_model_dir / "model.onnx"
    vad = settings.voice_model_dir / "vad.onnx"
    hashes = {
        "python": _write(python, b"private-python"),
        "yt_dlp": _write(yt_dlp, b"private-yt-dlp"),
        "deno": _write(deno, b"private-deno"),
        "ffmpeg": _write(ffmpeg, b"private-ffmpeg"),
        "model": _write(model, b"private-model"),
        "vad": _write(vad, b"private-vad"),
    }
    lock = {
        "adapter_contract_version": "voice-adapter-v1",
        "deno": {"path": str(deno), "sha256": hashes["deno"], "version": "2.9.5"},
        "ffmpeg": {"path": str(ffmpeg), "sha256": hashes["ffmpeg"], "version": "9.0.1"},
        "model": {
            "name": "model.onnx",
            "path": str(model),
            "sha256": hashes["model"],
            "version": "model-v1",
        },
        "provider": provider,
        "python": {"path": str(python), "sha256": hashes["python"], "version": "3.14.6"},
        "sherpa_onnx": {
            "version": "1.13.4",
            "wheel_sha256": "cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06",
        },
        "vad": {"path": str(vad), "sha256": hashes["vad"], "version": "vad-v1"},
        "vad_contract_version": "vad-v1",
        "yt_dlp": {"path": str(yt_dlp), "sha256": hashes["yt_dlp"], "version": "2026.08.19"},
    }
    _install_startup_inventory(settings, lock)
    settings.voice_runtime_dir.mkdir(parents=True, exist_ok=True)
    (settings.voice_runtime_dir / "runtime-lock.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    outputs = {
        (str(python.resolve()), "--version"): "Python 3.14.6",
        (str(yt_dlp.resolve()), "--version"): "2026.08.19",
        (str(deno.resolve()), "--version"): "deno 2.9.5",
        (str(ffmpeg.resolve()), "-version"): "ffmpeg version 9.0.1",
    }
    return settings, _FakeProbe(outputs), RuntimeAllowlists(
        deno_sha256=hashes["deno"],
        sherpa_wheel_sha256="cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06",
        yt_dlp_sha256=hashes["yt_dlp"],
    )


def test_default_runtime_allowlist_pins_official_deno_executable_sha256() -> None:
    assert RuntimeAllowlists().deno_sha256 == (
        "98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaeeb9e409ccb3b9fd"
    )


@pytest.mark.parametrize(
    "deno_sha256",
    (
        "98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaee9e409ccb3b9fd",
        "0" * 64,
    ),
)
def test_runtime_rejects_near_or_wrong_deno_executable_sha256(
    tmp_path: Path, deno_sha256: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    candidate = RuntimeAllowlists(
        deno_sha256=deno_sha256,
        sherpa_wheel_sha256=allowlists.sherpa_wheel_sha256,
        yt_dlp_sha256=allowlists.yt_dlp_sha256,
    )

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=probe, allowlists=candidate)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"
    assert probe.calls == []


def test_runtime_attests_exact_private_artifacts_and_fixed_probe_argv(tmp_path: Path) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)

    attestation = attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert attestation.provider == "CPUExecutionProvider"
    assert attestation.python_path.is_absolute()
    assert attestation.model_path.is_absolute()
    assert attestation.adapter_contract_version == "voice-adapter-v1"
    assert probe.calls == [
        (str(attestation.python_path), "--version"),
        (str(attestation.yt_dlp_path), "--version"),
        (str(attestation.deno_path), "--version"),
        (str(attestation.ffmpeg_path), "-version"),
    ]


@pytest.mark.parametrize(
    ("executable", "argument", "output"),
    (
        (
            "deno.exe",
            "--version",
            "deno 2.9.5 (stable, release, x86_64-pc-windows-msvc)",
        ),
        (
            "ffmpeg.exe",
            "-version",
            "ffmpeg version 9.0.1-essentials_build-www.gyan.dev Copyright 2026",
        ),
    ),
)
def test_runtime_accepts_pinned_official_version_banner_suffixes(
    tmp_path: Path, executable: str, argument: str, output: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    path = (settings.voice_runtime_dir / executable).resolve()
    probe.outputs[(str(path), argument)] = output

    attestation = attest_runtime(
        settings, version_probe=probe, allowlists=allowlists
    )

    assert (attestation.deno_version, attestation.ffmpeg_version) == (
        "2.9.5",
        "9.0.1",
    )


@pytest.mark.parametrize(
    ("executable", "argument", "output"),
    (
        (
            "deno.exe",
            "--version",
            "deno 2.9.50 (stable, release, x86_64-pc-windows-msvc)",
        ),
        (
            "ffmpeg.exe",
            "-version",
            "ffmpeg version 9.0.10-essentials_build-www.gyan.dev",
        ),
        ("deno.exe", "--version", "2.9.5 (stable, release)"),
        ("ffmpeg.exe", "-version", "9.0.1-essentials_build-www.gyan.dev"),
        ("deno.exe", "--version", "deno 2.9.5(stable, release)"),
        (
            "ffmpeg.exe",
            "-version",
            "ffmpeg version 9.0.1_essentials_build-www.gyan.dev",
        ),
    ),
)
def test_runtime_rejects_near_versions_missing_prefixes_and_bad_boundaries(
    tmp_path: Path, executable: str, argument: str, output: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    path = (settings.voice_runtime_dir / executable).resolve()
    probe.outputs[(str(path), argument)] = output

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"


def test_runtime_attests_exact_project_and_sherpa_startup_inventory(
    tmp_path: Path,
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    lock_path = settings.voice_runtime_dir / "runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    import_root, inventory = _install_startup_inventory(settings, lock)
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    attestation = attest_runtime(
        settings, version_probe=probe, allowlists=allowlists
    )

    assert attestation.python_import_root == import_root
    assert attestation.python_import_files == inventory


def test_voice_private_paths_are_derived_only_from_settings_data_dir(tmp_path: Path) -> None:
    settings = Settings.for_data_dir(tmp_path / "private-data")

    assert settings.voice_runtime_dir == settings.data_dir / "voice-runtime"
    assert settings.voice_model_dir == settings.data_dir / "voice-models"
    assert settings.voice_work_dir == settings.data_dir / "voice-work"


@pytest.mark.parametrize("mutation", ("cuda", "hash", "version", "path_escape", "symlink"))
def test_runtime_fails_closed_for_private_runtime_mutations(
    tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    lock_path = settings.voice_runtime_dir / "runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if mutation == "cuda":
        lock["provider"] = "CUDAExecutionProvider"
    elif mutation == "hash":
        lock["model"]["sha256"] = "0" * 64
    elif mutation == "version":
        lock["yt_dlp"]["version"] = "2026.01.01"
    elif mutation == "path_escape":
        outside = tmp_path / "outside-model.onnx"
        _write(outside, b"outside")
        lock["model"]["path"] = str(outside)
        lock["model"]["sha256"] = hashlib.sha256(b"outside").hexdigest()
    elif mutation == "symlink":
        original = runtime._is_reparse
        monkeypatch.setattr(
            runtime,
            "_is_reparse",
            lambda path: path.name == "model.onnx" or original(path),
        )
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"
    assert str(settings.data_dir) not in caught.value.message
    assert "outside-model" not in caught.value.message


def test_runtime_rejects_unknown_lock_shape_without_running_probes(tmp_path: Path) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    lock_path = settings.voice_runtime_dir / "runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["private_path"] = "secret"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert probe.calls == []


@pytest.mark.parametrize(
    ("lock_key", "version", "probe_prefix"),
    (
        ("python", "3.14.7", "Python "),
        ("yt_dlp", "2026.08.20", ""),
        ("deno", "2.9.6", "deno "),
        ("ffmpeg", "9.0.2", "ffmpeg version "),
    ),
)
def test_runtime_rejects_coordinated_lock_and_probe_version_drift(
    tmp_path: Path, lock_key: str, version: str, probe_prefix: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    lock_path = settings.voice_runtime_dir / "runtime-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock[lock_key]["version"] = version
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    path = Path(lock[lock_key]["path"]).resolve()
    argument = "-version" if lock_key == "ffmpeg" else "--version"
    probe.outputs[(str(path), argument)] = f"{probe_prefix}{version}"

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"


@pytest.mark.parametrize(
    "reparse_name", ("private-data", "voice-runtime", "voice-models", "runtime-lock.json")
)
def test_runtime_rejects_private_root_or_lock_reparse_before_lock_read(
    tmp_path: Path, reparse_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    reads: list[Path] = []
    original_read = runtime._read_lock

    def _read_spy(path: Path) -> dict[str, object]:
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(runtime, "_read_lock", _read_spy)
    original_reparse = runtime._is_reparse
    monkeypatch.setattr(
        runtime,
        "_is_reparse",
        lambda path: path.name == reparse_name or original_reparse(path),
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert reads == []


def test_runtime_maps_injected_domain_error_without_private_text(tmp_path: Path) -> None:
    settings, _, allowlists = _runtime_fixture(tmp_path)

    def _private_failure(argv: tuple[str, ...]) -> str:
        raise DomainError("PRIVATE_CODE", f"private sentinel {argv[0]}")

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=_private_failure, allowlists=allowlists)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"
    assert "private sentinel" not in caught.value.message


@pytest.mark.parametrize(
    "hook_name", ("unsafe.pth", "sitecustomize.py", "usercustomize.py")
)
def test_runtime_rejects_private_python_startup_hooks(
    tmp_path: Path, hook_name: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    site_packages = settings.voice_runtime_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / hook_name).write_text(
        "raise RuntimeError('private-startup-hook')", encoding="utf-8"
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)


@pytest.mark.parametrize(
    "hook_name", ("python._pth", "python314._pth", "PYTHON314._PTH")
)
def test_runtime_rejects_windows_python_path_override_before_probes(
    tmp_path: Path, hook_name: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    (settings.voice_runtime_dir / hook_name).write_text(
        "C:/private-alternate-import-root\nimport site\n", encoding="utf-8"
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert probe.calls == []


@pytest.mark.parametrize("hook_name", ("python._pth", "PYTHON314._PTH"))
def test_verify_runtime_startup_rejects_late_windows_python_path_override(
    tmp_path: Path, hook_name: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    attestation = attest_runtime(
        settings, version_probe=probe, allowlists=allowlists
    )
    (settings.voice_runtime_dir / hook_name).write_text(
        "C:/private-alternate-import-root\nimport site\n", encoding="utf-8"
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        verify_runtime_startup(attestation, settings.data_dir)


@pytest.mark.parametrize(
    "python_relative",
    (
        pytest.param(Path("python.exe"), id="root"),
        pytest.param(Path("Scripts/python.exe"), id="windows-venv"),
    ),
)
def test_verify_runtime_startup_accepts_supported_python_layouts(
    tmp_path: Path, python_relative: Path
) -> None:
    settings, probe, allowlists = _runtime_fixture(
        tmp_path, python_relative=python_relative
    )
    attestation = attest_runtime(
        settings, version_probe=probe, allowlists=allowlists
    )

    verify_runtime_startup(attestation, settings.data_dir)


def test_verify_runtime_startup_rejects_mismatched_startup_artifact_parents(
    tmp_path: Path,
) -> None:
    settings, probe, allowlists = _runtime_fixture(
        tmp_path, python_relative=Path("Scripts/python.exe")
    )
    attestation = attest_runtime(
        settings, version_probe=probe, allowlists=allowlists
    )
    alternate_manifest = (
        settings.voice_runtime_dir / "alternate" / "startup-manifest.json"
    )
    alternate_sha256 = _write(
        alternate_manifest,
        attestation.python_startup_manifest_path.read_bytes(),
    )
    mismatched = replace(
        attestation,
        python_startup_manifest_path=alternate_manifest.resolve(),
        python_startup_manifest_sha256=alternate_sha256,
    )

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        verify_runtime_startup(mismatched, settings.data_dir)

    assert caught.value.code == "VOICE_RUNTIME_INVALID"


def test_runtime_rejects_system_site_packages_in_pyvenv_config(
    tmp_path: Path,
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    (settings.voice_runtime_dir / "pyvenv.cfg").write_text(
        "home = C:/private-python\n"
        "include-system-site-packages = true\n"
        "version = 3.14.6\n",
        encoding="utf-8",
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)


def test_runtime_rejects_customize_hook_outside_site_packages(
    tmp_path: Path,
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    (settings.voice_runtime_dir / "Lib" / "sitecustomize.py").write_text(
        "raise RuntimeError('private-startup-hook')", encoding="utf-8"
    )

    with pytest.raises(DomainError, match="voice runtime is invalid"):
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)


@pytest.mark.parametrize("material", ("adapter", "sherpa", "extra"))
def test_runtime_rejects_python_import_inventory_drift(
    tmp_path: Path, material: str
) -> None:
    settings, probe, allowlists = _runtime_fixture(tmp_path)
    import_root = settings.voice_runtime_dir / "Lib" / "site-packages"
    if material == "adapter":
        adapter = (
            import_root
            / "market_voice_forecast_ledger"
            / "voice"
            / "adapter_main.py"
        )
        adapter.write_bytes(b"mutated-adapter")
    elif material == "sherpa":
        (import_root / "sherpa_onnx" / "__init__.py").write_bytes(
            b"mutated-sherpa"
        )
    else:
        (import_root / "private-sentinel.py").write_bytes(b"private-sentinel")

    with pytest.raises(DomainError, match="voice runtime is invalid") as caught:
        attest_runtime(settings, version_probe=probe, allowlists=allowlists)

    assert "private-sentinel" not in str(caught.value)
