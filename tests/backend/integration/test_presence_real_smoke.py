from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Mapping
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
    for line in completed.stdout.splitlines():
        if line:
            return line
    raise ValueError("runtime version probe failed")


def test_runtime_version_probe_returns_first_nonempty_stdout_line() -> None:
    writer = "import sys; sys.stdout.write(sys.argv[1])"

    output = _runtime_version_probe(
        (
            sys.executable,
            "-c",
            writer,
            "\nPython 3.14.6\nextra output\n",
        )
    )

    assert output == "Python 3.14.6"


@pytest.mark.parametrize(
    "script",
    (
        "",
        "import sys; sys.stdout.write('Python 3.14.6\\n'); sys.exit(7)",
    ),
)
def test_runtime_version_probe_rejects_empty_stdout_or_nonzero_exit(
    script: str,
) -> None:
    with pytest.raises(ValueError, match="runtime version probe failed"):
        _runtime_version_probe((sys.executable, "-c", script))


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


def run_presence_smoke_entry(
    *,
    environment: Mapping[str, str] = os.environ,
    config_loader: Callable[[], object] = load_private_smoke_config,
    config_validator: Callable[[object], bool] = validate_safe_config_shape,
    smoke_runner: Callable[[PrivateSmokeConfig], str] = (
        run_real_smoke_without_private_assert_values
    ),
) -> None:
    if environment.get(SMOKE_FLAG) != "1":
        pytest.skip("real presence voice smoke not requested")
    config = config_loader()
    if not config_validator(config):
        pytest.fail("real presence voice smoke configuration invalid", pytrace=False)
    smoke_runner(config)


def test_real_presence_runtime() -> None:
    run_presence_smoke_entry()


@pytest.mark.parametrize(
    "environment",
    ({}, {"MVFL_RUN_REAL_VOICE_SMOKE": "0"}),
)
def test_real_smoke_entry_requires_the_exact_opt_in_flag_before_loading_config(
    environment: dict[str, str],
) -> None:
    calls: list[str] = []

    def forbidden_loader() -> object:
        calls.append("load")
        return object()

    with pytest.raises(pytest.skip.Exception) as caught:
        run_presence_smoke_entry(
            environment=environment,
            config_loader=forbidden_loader,
            config_validator=lambda _config: True,
            smoke_runner=lambda _config: calls.append("run"),
        )

    assert str(caught.value) == SMOKE_SKIP_REASON
    assert calls == []


def test_real_smoke_entry_runs_each_opt_in_stage_once_for_exact_flag() -> None:
    calls: list[object] = []
    config = PrivateSmokeConfig(data_dir=Path.cwd())

    def config_loader() -> object:
        calls.append("load")
        return config

    def config_validator(candidate: object) -> bool:
        calls.append(candidate)
        return candidate is config

    def smoke_runner(candidate: PrivateSmokeConfig) -> str:
        calls.append(candidate)
        return "unused fixed result"

    try:
        run_presence_smoke_entry(
            environment={"MVFL_RUN_REAL_VOICE_SMOKE": "1"},
            config_loader=config_loader,
            config_validator=config_validator,
            smoke_runner=smoke_runner,
        )
    except pytest.skip.Exception:
        pytest.fail("real presence voice smoke entry unexpectedly skipped", pytrace=False)

    assert calls == ["load", config, config]


def test_real_smoke_entry_rejects_invalid_config_with_fixed_nontrace_failure() -> None:
    calls: list[str] = []

    def invalid_config_loader() -> object:
        calls.append("load")
        return object()

    def invalid_config_validator(_config: object) -> bool:
        calls.append("validate")
        return False

    with pytest.raises(pytest.fail.Exception) as caught:
        run_presence_smoke_entry(
            environment={"MVFL_RUN_REAL_VOICE_SMOKE": "1"},
            config_loader=invalid_config_loader,
            config_validator=invalid_config_validator,
            smoke_runner=lambda _config: calls.append("run"),
        )

    assert str(caught.value) == SMOKE_CONFIG_FAILURE
    assert caught.value.pytrace is False
    assert calls == ["load", "validate"]


def test_module_scope_import_guard_recurses_without_entering_helpers() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    assert _module_scope_production_imports(tree) == ()


def test_static_guard_mutations_detect_module_import_and_new_helper_assertion() -> None:
    module_import_mutation = ast.parse(
        "if True:\n    import market_voice_forecast_ledger.voice.runtime\n"
    )
    helper_assertion_mutation = ast.parse(
        "def _new_opt_in_helper() -> None:\n    assert False\n"
    )

    assert _module_scope_production_imports(module_import_mutation) == (
        "market_voice_forecast_ledger.voice.runtime",
    )
    assert _opt_in_path_assertions(helper_assertion_mutation)


def test_static_guard_detects_rewritten_assertion_in_exact_real_smoke_root() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    mutated = source.replace(
        "def test_real_presence_runtime() -> None:\n    run_presence_smoke_entry()\n",
        (
            "def test_real_presence_runtime() -> None:\n"
            "    run_presence_smoke_entry()\n"
            "    assert os.environ['private-voice-env']\n"
        ),
        1,
    )

    assert _opt_in_path_assertions(ast.parse(mutated))


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


def _module_scope_production_imports(tree: ast.Module) -> tuple[str, ...]:
    imported_modules: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return tuple(
        module
        for module in imported_modules
        if module.startswith("market_voice_forecast_ledger")
    )


def _opt_in_path_nodes(tree: ast.Module) -> tuple[ast.FunctionDef, ...]:
    return tuple(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and (
            not node.name.startswith("test_")
            or node.name == "test_real_presence_runtime"
        )
    )


def _opt_in_path_assertions(tree: ast.Module) -> tuple[ast.Assert, ...]:
    return tuple(
        node
        for target in _opt_in_path_nodes(tree)
        for node in ast.walk(target)
        if isinstance(node, ast.Assert)
    )


def test_real_smoke_collection_imports_no_private_runtime_module() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    assert _module_scope_production_imports(tree) == ()


def test_real_smoke_opt_in_path_uses_only_fixed_failures_and_one_skip() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    targets = _opt_in_path_nodes(tree)
    assertions = _opt_in_path_assertions(tree)
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

    assert {
        "run_presence_smoke_entry",
        "test_real_presence_runtime",
    } <= {target.name for target in targets}
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
