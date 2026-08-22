"""Attest only private, pinned voice-runtime artifacts before use."""

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCK_KEYS = frozenset(
    {
        "adapter_contract_version",
        "deno",
        "ffmpeg",
        "model",
        "provider",
        "python",
        "python_startup",
        "sherpa_onnx",
        "vad",
        "vad_contract_version",
        "yt_dlp",
    }
)
_SHERPA_VERSION = "1.13.4"
_SHERPA_WHEEL_SHA256 = "cb1834182c4047b8edb1dceeed8d5cf7d6e10295a4079e5e0fea674b4314db06"
_YT_DLP_SHA256 = "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a"
_DENO_SHA256 = "98f8c2a2d470e4ccb04c935c86ff8050817d877762aec5eaee9e409ccb3b9fd"
_PYTHON_VERSION = "3.14.6"
_YT_DLP_VERSION = "2026.08.19"
_DENO_VERSION = "2.9.5"
_FFMPEG_VERSION = "9.0.1"

VersionProbe = Callable[[tuple[str, ...]], str]


@dataclass(frozen=True, slots=True)
class RuntimeAllowlists:
    yt_dlp_sha256: str = _YT_DLP_SHA256
    deno_sha256: str = _DENO_SHA256
    sherpa_wheel_sha256: str = _SHERPA_WHEEL_SHA256


@dataclass(frozen=True, slots=True)
class RuntimeAttestation:
    python_path: Path
    python_sha256: str
    python_version: str
    python_import_root: Path
    python_import_files: tuple[tuple[str, str], ...]
    python_startup_manifest_path: Path
    python_startup_manifest_sha256: str
    python_pyvenv_path: Path
    python_pyvenv_sha256: str
    yt_dlp_path: Path
    yt_dlp_sha256: str
    yt_dlp_version: str
    deno_path: Path
    deno_sha256: str
    deno_version: str
    ffmpeg_path: Path
    ffmpeg_sha256: str
    ffmpeg_version: str
    model_path: Path
    model_sha256: str
    model_name: str
    model_version: str
    vad_path: Path
    vad_sha256: str
    vad_version: str
    provider: str
    adapter_contract_version: str
    vad_contract_version: str
    sherpa_onnx_version: str
    sherpa_wheel_sha256: str


def attest_runtime(
    settings: Settings,
    *,
    version_probe: VersionProbe,
    allowlists: RuntimeAllowlists = RuntimeAllowlists(),
) -> RuntimeAttestation:
    try:
        if not isinstance(settings, Settings) or not isinstance(allowlists, RuntimeAllowlists):
            raise ValueError("invalid runtime inputs")
        _validate_allowlists(allowlists)
        data_root = _private_root(settings.data_dir)
        runtime_root = _private_child_root(settings.voice_runtime_dir, data_root)
        model_root = _private_child_root(settings.voice_model_dir, data_root)
        lock_path = _private_file(settings.voice_runtime_dir / "runtime-lock.json", runtime_root)
        lock = _read_lock(lock_path)
        python = _artifact(lock["python"], runtime_root, {"path", "sha256", "version"})
        startup = _startup_attestation(lock["python_startup"], runtime_root)
        yt_dlp = _artifact(lock["yt_dlp"], runtime_root, {"path", "sha256", "version"})
        deno = _artifact(lock["deno"], runtime_root, {"path", "sha256", "version"})
        ffmpeg = _artifact(lock["ffmpeg"], runtime_root, {"path", "sha256", "version"})
        model = _artifact(lock["model"], model_root, {"name", "path", "sha256", "version"})
        vad = _artifact(lock["vad"], model_root, {"path", "sha256", "version"})
        provider = lock["provider"]
        adapter_version = lock["adapter_contract_version"]
        vad_contract_version = lock["vad_contract_version"]
        sherpa = lock["sherpa_onnx"]
        if (
            provider != "CPUExecutionProvider"
            or not _token(adapter_version)
            or not _token(vad_contract_version)
            or not isinstance(sherpa, dict)
            or set(sherpa) != {"version", "wheel_sha256"}
            or python["version"] != _PYTHON_VERSION
            or yt_dlp["version"] != _YT_DLP_VERSION
            or deno["version"] != _DENO_VERSION
            or ffmpeg["version"] != _FFMPEG_VERSION
            or sherpa["version"] != _SHERPA_VERSION
            or sherpa["wheel_sha256"] != allowlists.sherpa_wheel_sha256
            or yt_dlp["sha256"] != allowlists.yt_dlp_sha256
            or deno["sha256"] != allowlists.deno_sha256
        ):
            raise ValueError("runtime lock mismatch")
        _validate_probe(version_probe, python["path"], "--version", f"Python {python['version']}")
        _validate_probe(version_probe, yt_dlp["path"], "--version", yt_dlp["version"])
        _validate_probe(version_probe, deno["path"], "--version", f"deno {deno['version']}")
        _validate_probe(version_probe, ffmpeg["path"], "-version", f"ffmpeg version {ffmpeg['version']}")
        return RuntimeAttestation(
            python_path=python["path"],
            python_sha256=python["sha256"],
            python_version=python["version"],
            python_import_root=startup["import_root"],
            python_import_files=startup["files"],
            python_startup_manifest_path=startup["manifest_path"],
            python_startup_manifest_sha256=startup["manifest_sha256"],
            python_pyvenv_path=startup["pyvenv_path"],
            python_pyvenv_sha256=startup["pyvenv_sha256"],
            yt_dlp_path=yt_dlp["path"],
            yt_dlp_sha256=yt_dlp["sha256"],
            yt_dlp_version=yt_dlp["version"],
            deno_path=deno["path"],
            deno_sha256=deno["sha256"],
            deno_version=deno["version"],
            ffmpeg_path=ffmpeg["path"],
            ffmpeg_sha256=ffmpeg["sha256"],
            ffmpeg_version=ffmpeg["version"],
            model_path=model["path"],
            model_sha256=model["sha256"],
            model_name=model["name"],
            model_version=model["version"],
            vad_path=vad["path"],
            vad_sha256=vad["sha256"],
            vad_version=vad["version"],
            provider=provider,
            adapter_contract_version=adapter_version,
            vad_contract_version=vad_contract_version,
            sherpa_onnx_version=sherpa["version"],
            sherpa_wheel_sha256=sherpa["wheel_sha256"],
        )
    except Exception:
        raise _runtime_invalid() from None


def _validate_allowlists(allowlists: RuntimeAllowlists) -> None:
    if not all(_sha256(value) for value in (
        allowlists.yt_dlp_sha256,
        allowlists.deno_sha256,
        allowlists.sherpa_wheel_sha256,
    )):
        raise ValueError("invalid allowlists")


def verify_runtime_startup(
    attestation: RuntimeAttestation, data_root: Path
) -> None:
    try:
        if not isinstance(attestation, RuntimeAttestation):
            raise ValueError("invalid runtime attestation")
        private_data = _private_root(data_root)
        runtime_root = _private_child_root(
            attestation.python_path.parent, private_data
        )
        expected_import_root = runtime_root / "Lib" / "site-packages"
        import_root = _private_child_root(
            attestation.python_import_root, runtime_root
        )
        if import_root != expected_import_root:
            raise ValueError("unexpected Python import root")
        _reject_runtime_startup_hooks(runtime_root)
        manifest = _private_file(
            attestation.python_startup_manifest_path, runtime_root
        )
        pyvenv = _private_file(attestation.python_pyvenv_path, runtime_root)
        if (
            manifest != runtime_root / "startup-manifest.json"
            or pyvenv != runtime_root / "pyvenv.cfg"
            or _file_sha256(manifest)
            != attestation.python_startup_manifest_sha256
            or _file_sha256(pyvenv) != attestation.python_pyvenv_sha256
        ):
            raise ValueError("Python startup artifacts changed")
        files = _read_startup_manifest(manifest, import_root)
        if (
            files != attestation.python_import_files
            or _startup_inventory(import_root) != files
        ):
            raise ValueError("Python import inventory changed")
        _validate_pyvenv(pyvenv)
    except Exception:
        raise _runtime_invalid() from None


def _startup_attestation(
    value: object, runtime_root: Path
) -> dict[str, Any]:
    expected_keys = {
        "import_root",
        "manifest_path",
        "manifest_sha256",
        "pyvenv_path",
        "pyvenv_sha256",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise ValueError("invalid Python startup attestation")
    if not all(
        isinstance(value[key], str)
        for key in ("import_root", "manifest_path", "pyvenv_path")
    ) or not all(
        _sha256(value[key])
        for key in ("manifest_sha256", "pyvenv_sha256")
    ):
        raise ValueError("invalid Python startup fields")
    import_root = _private_child_root(Path(value["import_root"]), runtime_root)
    manifest = _private_file(Path(value["manifest_path"]), runtime_root)
    pyvenv = _private_file(Path(value["pyvenv_path"]), runtime_root)
    if (
        import_root != runtime_root / "Lib" / "site-packages"
        or manifest != runtime_root / "startup-manifest.json"
        or pyvenv != runtime_root / "pyvenv.cfg"
        or _file_sha256(manifest) != value["manifest_sha256"]
        or _file_sha256(pyvenv) != value["pyvenv_sha256"]
    ):
        raise ValueError("Python startup identity mismatch")
    _reject_runtime_startup_hooks(runtime_root)
    files = _read_startup_manifest(manifest, import_root)
    if _startup_inventory(import_root) != files:
        raise ValueError("Python import inventory mismatch")
    _validate_pyvenv(pyvenv)
    return {
        "import_root": import_root,
        "files": files,
        "manifest_path": manifest,
        "manifest_sha256": value["manifest_sha256"],
        "pyvenv_path": pyvenv,
        "pyvenv_sha256": value["pyvenv_sha256"],
    }


def _read_startup_manifest(
    path: Path, import_root: Path
) -> tuple[tuple[str, str], ...]:
    body = path.read_bytes()
    if not body or len(body) > 8_388_608:
        raise ValueError("invalid startup manifest size")
    value = json.loads(
        body.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object
    )
    if type(value) is not dict or set(value) != {"files"}:
        raise ValueError("invalid startup manifest")
    raw_files = value["files"]
    if type(raw_files) is not list or not raw_files or len(raw_files) > 100_000:
        raise ValueError("invalid startup file inventory")
    files: list[tuple[str, str]] = []
    for item in raw_files:
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise ValueError("invalid startup file")
        relative = item["path"]
        digest = item["sha256"]
        if (
            type(relative) is not str
            or not relative
            or "\\" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not _sha256(digest)
        ):
            raise ValueError("invalid startup file identity")
        _reject_startup_hook(relative)
        candidate = _private_file(import_root / Path(relative), import_root)
        if _file_sha256(candidate) != digest:
            raise ValueError("startup file changed")
        files.append((relative, digest))
    result = tuple(files)
    if result != tuple(sorted(set(result))):
        raise ValueError("startup inventory is not canonical")
    names = {relative for relative, _digest in result}
    if (
        "market_voice_forecast_ledger/voice/adapter_main.py" not in names
        or "sherpa_onnx/__init__.py" not in names
    ):
        raise ValueError("required startup material is absent")
    return result


def _startup_inventory(import_root: Path) -> tuple[tuple[str, str], ...]:
    files: list[tuple[str, str]] = []
    for current, directories, filenames in os.walk(
        import_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        if _is_reparse(current_path):
            raise ValueError("startup directory is a reparse point")
        for directory in directories:
            if _is_reparse(current_path / directory):
                raise ValueError("startup directory is a reparse point")
        for filename in filenames:
            candidate = _private_file(current_path / filename, import_root)
            relative = candidate.relative_to(import_root).as_posix()
            _reject_startup_hook(relative)
            files.append((relative, _file_sha256(candidate)))
    return tuple(sorted(files))


def _reject_startup_hook(relative: str) -> None:
    name = relative.rsplit("/", 1)[-1].casefold()
    if (
        name.endswith(".pth")
        or name.startswith("sitecustomize.")
        or name.startswith("usercustomize.")
    ):
        raise ValueError("Python startup hook is forbidden")


def _reject_runtime_startup_hooks(runtime_root: Path) -> None:
    for current, directories, filenames in os.walk(
        runtime_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        if _is_reparse(current_path):
            raise ValueError("runtime directory is a reparse point")
        for directory in directories:
            if _is_reparse(current_path / directory):
                raise ValueError("runtime directory is a reparse point")
        for filename in filenames:
            _reject_startup_hook(filename)


def _validate_pyvenv(path: Path) -> None:
    body = path.read_bytes()
    if not body or len(body) > 65_536:
        raise ValueError("invalid pyvenv config")
    text = body.decode("utf-8", errors="strict")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if "=" not in raw_line:
            raise ValueError("invalid pyvenv config")
        key, item = (part.strip() for part in raw_line.split("=", 1))
        folded = key.casefold()
        if not folded or folded in values or "\x00" in item:
            raise ValueError("invalid pyvenv config")
        values[folded] = item.casefold()
    if values.get("include-system-site-packages") != "false":
        raise ValueError("system site packages are not isolated")


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
        if len(body) > 65_536:
            raise ValueError("runtime lock is oversized")
        lock = json.loads(body.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("invalid runtime lock") from None
    if type(lock) is not dict or set(lock) != _LOCK_KEYS:
        raise ValueError("invalid runtime lock shape")
    return lock


def _artifact(value: object, root: Path, expected_keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ValueError("invalid artifact")
    path_value = value.get("path")
    digest = value.get("sha256")
    version = value.get("version")
    if not isinstance(path_value, str) or not _sha256(digest) or not _token(version):
        raise ValueError("invalid artifact fields")
    path = _private_file(Path(path_value), root)
    if _file_sha256(path) != digest:
        raise ValueError("artifact hash mismatch")
    result: dict[str, Any] = {"path": path, "sha256": digest, "version": version}
    if "name" in expected_keys:
        name = value.get("name")
        if not _token(name):
            raise ValueError("invalid model name")
        result["name"] = name
    return result


def _private_root(path: Path) -> Path:
    raw = path.absolute()
    if not raw.is_absolute():
        raise ValueError("invalid private root")
    _require_no_reparse(raw)
    root = raw.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid private root")
    return root


def _private_child_root(path: Path, parent: Path) -> Path:
    root = _private_root(path)
    try:
        root.relative_to(parent)
    except ValueError:
        raise ValueError("private root escaped data directory") from None
    return root


def _private_file(path: Path, root: Path) -> Path:
    raw = path.absolute()
    if not raw.is_absolute():
        raise ValueError("private artifact must be absolute")
    try:
        relative = raw.relative_to(root)
    except ValueError:
        raise ValueError("private artifact escaped root") from None
    current = root
    for component in relative.parts:
        current = current / component
        if _is_reparse(current):
            raise ValueError("private artifact reparse point")
    resolved = raw.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("private artifact escaped root") from None
    if not resolved.is_file():
        raise ValueError("private artifact is not a file")
    return resolved


def _require_no_reparse(path: Path) -> None:
    anchor = Path(path.anchor)
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        raise ValueError("invalid private path") from None
    current = anchor
    for component in relative.parts:
        current = current / component
        if _is_reparse(current):
            raise ValueError("private path reparse point")


def _is_reparse(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        path.is_symlink()
        or bool(isjunction(path))
        or bool(attributes & reparse_flag)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_probe(
    probe: VersionProbe, path: Path, argument: str, expected: str
) -> None:
    if not callable(probe):
        raise ValueError("invalid version probe")
    output = probe((str(path), argument))
    if type(output) is not str or output != expected:
        raise ValueError("unexpected version output")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate runtime lock key")
        result[key] = value
    return result


def _token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _runtime_invalid() -> DomainError:
    return DomainError("VOICE_RUNTIME_INVALID", "voice runtime is invalid")


__all__ = [
    "RuntimeAllowlists",
    "RuntimeAttestation",
    "VersionProbe",
    "attest_runtime",
    "verify_runtime_startup",
]
