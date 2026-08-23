from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest


SMOKE_FLAG = "MVFL_RUN_REAL_VOICE_SMOKE"
SMOKE_DATA_DIR = "MVFL_REAL_VOICE_SMOKE_DATA_DIR"
SMOKE_SKIP_REASON = "real presence voice smoke not requested"
SMOKE_CONFIG_FAILURE = "real presence voice smoke configuration invalid"
SMOKE_RUNTIME_FAILURE = "real presence voice runtime attestation failed"
SMOKE_SUCCESS = "Presence voice runtime attestation completed."
SMOKE_PRIVATE_SENTINEL = "private-voice-runtime-sentinel"
SMOKE_PRIVACY_FAILURE = "real presence voice smoke privacy control failed"


@dataclass(frozen=True, slots=True)
class PrivateSmokeConfig:
    data_dir: Path


def load_private_smoke_config() -> object:
    data_dir = os.environ.get(SMOKE_DATA_DIR)
    if type(data_dir) is not str:
        return object()
    return PrivateSmokeConfig(data_dir=Path(data_dir))


def validate_safe_config_shape(config: object) -> bool:
    return (
        type(config) is PrivateSmokeConfig
        and isinstance(config.data_dir, Path)
        and config.data_dir.is_absolute()
    )


def _runtime_version_probe(argv: tuple[str, ...]) -> str:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0 or type(completed.stdout) is not str:
        raise ValueError("runtime version probe failed")
    return completed.stdout


def _default_settings_factory(data_dir: Path) -> object:
    from market_voice_forecast_ledger.config import Settings

    return Settings.for_data_dir(data_dir)


def _default_attestation_runner(
    settings: object,
    *,
    version_probe: Callable[[tuple[str, ...]], str],
) -> object:
    from market_voice_forecast_ledger.voice.runtime import attest_runtime

    return attest_runtime(settings, version_probe=version_probe)


def run_real_smoke_without_private_assert_values(
    config: PrivateSmokeConfig,
    *,
    settings_factory: Callable[[Path], object] = _default_settings_factory,
    attestation_runner: Callable[..., object] = _default_attestation_runner,
    version_probe: Callable[[tuple[str, ...]], str] = _runtime_version_probe,
) -> str:
    try:
        settings = settings_factory(config.data_dir)
        attestation_runner(settings, version_probe=version_probe)
    except Exception:
        pytest.fail("real presence voice runtime attestation failed", pytrace=False)
    return SMOKE_SUCCESS


def test_real_presence_runtime() -> None:
    if os.environ.get(SMOKE_FLAG) != "1":
        pytest.skip("real presence voice smoke not requested")
    config = load_private_smoke_config()
    if not validate_safe_config_shape(config):
        pytest.fail("real presence voice smoke configuration invalid", pytrace=False)
    run_real_smoke_without_private_assert_values(config)


def test_smoke_config_has_one_absolute_private_data_root() -> None:
    valid = PrivateSmokeConfig(data_dir=Path.cwd())

    assert validate_safe_config_shape(valid) is True
    assert validate_safe_config_shape(object()) is False
    assert validate_safe_config_shape(PrivateSmokeConfig(data_dir=Path("relative"))) is False


def test_injected_smoke_attestation_positive_control() -> None:
    observed: list[object] = []
    config = PrivateSmokeConfig(data_dir=Path.cwd())

    def fake_settings_factory(data_dir: Path) -> object:
        observed.append(data_dir)
        return "safe-settings"

    def fake_attestation_runner(
        settings: object,
        *,
        version_probe: Callable[[tuple[str, ...]], str],
    ) -> object:
        observed.append(settings)
        observed.append(version_probe(("safe-tool", "--version")))
        return object()

    result = run_real_smoke_without_private_assert_values(
        config,
        settings_factory=fake_settings_factory,
        attestation_runner=fake_attestation_runner,
        version_probe=lambda _argv: "safe version",
    )

    assert result == SMOKE_SUCCESS
    assert observed == [Path.cwd(), "safe-settings", "safe version"]


def test_injected_malformed_attestation_uses_fixed_failure() -> None:
    config = PrivateSmokeConfig(data_dir=Path.cwd())

    def malformed_attestation_runner(
        _settings: object,
        *,
        version_probe: Callable[[tuple[str, ...]], str],
    ) -> object:
        version_probe(("safe-tool", "--version"))
        raise ValueError("malformed response")

    with pytest.raises(pytest.fail.Exception) as caught:
        run_real_smoke_without_private_assert_values(
            config,
            settings_factory=lambda _data_dir: object(),
            attestation_runner=malformed_attestation_runner,
            version_probe=lambda _argv: "safe version",
        )

    assert str(caught.value) == SMOKE_RUNTIME_FAILURE


def test_injected_private_attestation_failure_never_discloses_sentinel() -> None:
    config = PrivateSmokeConfig(data_dir=Path.cwd())

    def private_attestation_runner(
        _settings: object,
        *,
        version_probe: Callable[[tuple[str, ...]], str],
    ) -> object:
        version_probe(("safe-tool", "--version"))
        raise RuntimeError(SMOKE_PRIVATE_SENTINEL)

    with pytest.raises(pytest.fail.Exception) as caught:
        run_real_smoke_without_private_assert_values(
            config,
            settings_factory=lambda _data_dir: object(),
            attestation_runner=private_attestation_runner,
            version_probe=lambda _argv: "safe version",
        )

    rendered = f"{caught.value!s} {caught.value!r}"
    if SMOKE_PRIVATE_SENTINEL in rendered or str(caught.value) != SMOKE_RUNTIME_FAILURE:
        pytest.fail(SMOKE_PRIVACY_FAILURE, pytrace=False)


def test_real_smoke_collection_imports_no_private_runtime_module() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported_modules = tuple(
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    ) + tuple(
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert all(
        not module.startswith("market_voice_forecast_ledger")
        for module in imported_modules
    )


def test_real_smoke_opt_in_path_uses_only_fixed_failures_and_one_skip() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    target_names = {
        "load_private_smoke_config",
        "validate_safe_config_shape",
        "_runtime_version_probe",
        "_default_settings_factory",
        "_default_attestation_runner",
        "run_real_smoke_without_private_assert_values",
        "test_real_presence_runtime",
    }
    targets = tuple(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in target_names
    )
    assertions = tuple(
        node for target in targets for node in ast.walk(target) if isinstance(node, ast.Assert)
    )
    fail_calls = tuple(
        node
        for target in targets
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "fail"
    )
    skip_calls = tuple(
        node
        for target in targets
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "skip"
    )

    assert {target.name for target in targets} == target_names
    assert assertions == ()
    assert len(fail_calls) == 2
    assert all(
        call.args
        and isinstance(call.args[0], ast.Constant)
        and type(call.args[0].value) is str
        and any(
            keyword.arg == "pytrace"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
        for call in fail_calls
    )
    assert len(skip_calls) == 1
    assert (
        skip_calls[0].args
        and isinstance(skip_calls[0].args[0], ast.Constant)
        and skip_calls[0].args[0].value == SMOKE_SKIP_REASON
    )
