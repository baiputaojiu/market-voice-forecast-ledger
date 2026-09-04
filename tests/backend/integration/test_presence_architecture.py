from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.migrate import apply_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "market_voice_forecast_ledger"
VOICE_ADAPTER_FILES = (PACKAGE_ROOT / "voice" / "adapter_main.py",)
PRODUCTION_FILES = tuple(sorted(PACKAGE_ROOT.rglob("*.py")))
PRESENCE_SHARED_BOUNDARY_PATHS = frozenset(
    {
        "cli.py",
        "config.py",
        "db/connection.py",
        "repositories/discovery.py",
        "repositories/retention.py",
        "services/audit.py",
        "services/job_state.py",
        "services/retention.py",
    }
)
PRESENCE_NAMED_BOUNDARY_DIRECTORIES = frozenset(
    {"domain", "repositories", "services", "workers"}
)
PRESENCE_SQL_OWNERS = frozenset(
    {
        "repositories/discovery.py",
        "repositories/voice_verification.py",
    }
)
CANONICAL_DOWNSTREAM_SQL_OWNERS = frozenset(
    {
        ("repositories/retention.py", "analysis_input_snapshots"),
        ("repositories/retention.py", "transcript_segments"),
    }
)


def _is_protected_presence_path(relative: str) -> bool:
    parts = relative.split("/")
    if relative in PRESENCE_SHARED_BOUNDARY_PATHS:
        return True
    if len(parts) != 2 or not parts[1].endswith(".py"):
        return False
    if parts[0] == "voice":
        return True
    stem = parts[1][:-3]
    return parts[0] in PRESENCE_NAMED_BOUNDARY_DIRECTORIES and stem.startswith(
        ("presence_", "voice_")
    )


def _protected_presence_files(package_root: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(package_root.rglob("*.py"))
        if _is_protected_presence_path(_relative(path, package_root))
    )
REVIEW_WRITER_METHOD = "add_review_and_decision"
APPROVED_REVIEW_WRITER_CALLERS = (
    "services/voice_verification.py:PresenceVerificationService.review",
)
EXPECTED_VOICE_TABLES = frozenset(
    {
        "voice_vad_repairs",
        "voice_reference_calibrations",
        "voice_reference_clips",
        "voice_reference_features",
        "voice_reference_profiles",
        "voice_verification_manifests",
        "voice_verification_reviews",
        "voice_verification_runs",
        "voice_verification_segments",
    }
)


ADAPTER_ALLOWED_IMPORTS = frozenset(
    {
        *(('import', module, None) for module in (
            '_socket', 'base64', 'hashlib', 'importlib', 'json', 'math',
            'socket', 'struct', 'sys', 'time', 'typing', 'wave',
        )),
        ('from', 'array', 'array'),
        ('from', 'collections.abc', 'Callable'),
        ('from', 'importlib', 'import_module'),
        ('from', 'pathlib', 'Path'),
        ('from', 'typing', 'Any'),
        ('from', 'typing', 'Protocol'),
        ('from', 'typing', 'TYPE_CHECKING'),
        (
            'from',
            f'{PACKAGE_ROOT.name}.domain.common',
            'canonical_json',
        ),
        ('from', f'{PACKAGE_ROOT.name}.domain.common', 'sha256_text'),
        ('from', f'{PACKAGE_ROOT.name}.domain.errors', 'DomainError'),
        (
            'from',
            f'{PACKAGE_ROOT.name}.domain.voice_verification',
            'VoiceProposal',
        ),
        (
            'from',
            f'{PACKAGE_ROOT.name}.domain.voice_verification',
            'PRESENCE_VAD_CONTRACT_VERSION',
        ),
        *(
            ('from', f'{PACKAGE_ROOT.name}.voice.protocol', symbol)
            for symbol in (
                'MAX_ADAPTER_RESPONSE_BYTES',
                'MAX_ADAPTER_SEGMENTS',
                'AdapterRequest',
                'AdapterResponse',
                'ReferenceAudioInput',
                'ReferenceDryRunRequest',
                'ReferenceDryRunResponse',
                'ReferenceEnrollmentRequest',
                'ReferenceEnrollmentResponse',
                'ReferenceRequest',
                'ReferenceResponse',
                'ReferenceScoreRequest',
                'ReferenceScoreResponse',
                'decode_reference_input_feature',
                'decode_reference_request',
                'decode_reference_response',
                'decode_reference_feature',
                'decode_response',
                'encode_reference_response',
                'encode_request',
                'reference_feature_semantics',
                'wipe_reference_feature',
            )
        ),
        ('dynamic', 'sherpa_onnx', None),
    }
)


_SQL_IDENTIFIER = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_]\w*)'
_SQL_WRITE = re.compile(
    rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE(?:\s+INTO)?|"
    rf"UPDATE|DELETE\s+FROM)\s+"
    rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?"
    rf"(?P<table>{_SQL_IDENTIFIER})",
    re.IGNORECASE,
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: Path, package_root: Path = PACKAGE_ROOT) -> str:
    return path.relative_to(package_root).as_posix()


def _resolve_import_from(
    node: ast.ImportFrom,
    path: Path,
    package_root: Path,
) -> str:
    if node.level == 0:
        return node.module or ""
    package = (package_root.name, *path.relative_to(package_root).parent.parts)
    keep = len(package) - (node.level - 1)
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*package[: max(keep, 0)], *suffix))


def _direct_import_records(
    path: Path,
    package_root: Path,
) -> Iterator[tuple[int, str, str, str | None]]:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, "import", alias.name, None
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(node, path, package_root)
            for alias in node.names:
                yield node.lineno, "from", module, alias.name
        elif isinstance(node, ast.Call):
            is_import_module = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ) or (
                isinstance(node.func, ast.Name)
                and node.func.id in {"import_module", "__import__"}
            )
            if (
                is_import_module
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and type(node.args[0].value) is str
            ):
                yield node.lineno, "dynamic", node.args[0].value, None


def runtime_import_violations(
    files: Iterable[Path],
    *,
    package_root: Path = PACKAGE_ROOT,
) -> tuple[tuple[str, int, str], ...]:
    violations = []
    for path in files:
        relative = _relative(path, package_root)
        for lineno, kind, module, symbol in _direct_import_records(
            path, package_root
        ):
            if (kind, module, symbol) not in ADAPTER_ALLOWED_IMPORTS:
                display = module if symbol is None else f"{module}.{symbol}"
                violations.append((relative, lineno, display))
    return tuple(sorted(violations))


def _unquote_identifier(identifier: str) -> str:
    if identifier[:1] in {'"', '`', '['}:
        return identifier[1:-1]
    return identifier


def _direct_sql_writes(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or type(node.value) is not str:
            continue
        for match in _SQL_WRITE.finditer(node.value):
            yield node.lineno, _unquote_identifier(
                match.group("table")
            ).lower()


def _is_protected_sql_write(relative: str, table: str) -> bool:
    if table in {"presence_decisions", "subject_video_candidates"}:
        return relative not in PRESENCE_SQL_OWNERS
    is_downstream = (
        table in {"speaker_assignments", "transcript_segments"}
        or table.startswith("analysis_")
    )
    return (
        _is_protected_presence_path(relative)
        and is_downstream
        and (relative, table) not in CANONICAL_DOWNSTREAM_SQL_OWNERS
    )


def direct_sql_write_violations(
    files: Iterable[Path] = PRODUCTION_FILES,
    *,
    package_root: Path = PACKAGE_ROOT,
) -> tuple[tuple[str, int, str], ...]:
    violations = []
    for path in files:
        relative = _relative(path, package_root)
        violations.extend(
            (relative, lineno, table)
            for lineno, table in _direct_sql_writes(_tree(path))
            if _is_protected_sql_write(relative, table)
        )
    return tuple(sorted(violations))


def _nodes_with_owner(
    node: ast.AST,
    owners: tuple[str, ...] = (),
) -> Iterator[tuple[ast.AST, str]]:
    nested = owners
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        nested = (*owners, node.name)
    owner = ".".join(nested) if nested else "<module>"
    yield node, owner
    for child in ast.iter_child_nodes(node):
        yield from _nodes_with_owner(child, nested)


def direct_review_writer_calls(
    files: Iterable[Path] = PRODUCTION_FILES,
    *,
    package_root: Path = PACKAGE_ROOT,
) -> tuple[str, ...]:
    callers = set()
    for path in files:
        relative = _relative(path, package_root)
        for node, owner in _nodes_with_owner(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == REVIEW_WRITER_METHOD
            ):
                callers.add(f"{relative}:{owner}")
    return tuple(sorted(callers))


def _contains_writer_reference(node: ast.AST) -> bool:
    return any(
        (
            isinstance(item, ast.Attribute)
            and item.attr == REVIEW_WRITER_METHOD
        )
        or (
            isinstance(item, ast.Constant)
            and item.value == REVIEW_WRITER_METHOD
        )
        for item in ast.walk(node)
    )


def _assigned_values(node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        value = node.value
        return () if value is None else (value,)
    return ()


def _stores_writer_reference(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return False
    if isinstance(node, ast.Attribute) and node.attr == REVIEW_WRITER_METHOD:
        return True
    return any(_stores_writer_reference(child) for child in ast.iter_child_nodes(node))


def _call_mechanism(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        name = node.func.id
        if name in {"eval", "exec"}:
            return name
        if name in {"getattr", "setattr"} and _contains_writer_reference(node):
            return name
        if name == "partial" and _contains_writer_reference(node):
            return "functools.partial"
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "functools"
        and node.func.attr == "partial"
        and _contains_writer_reference(node)
    ):
        return "functools.partial"
    return None


def writer_convention_violations(
    files: Iterable[Path] | None = None,
    *,
    package_root: Path = PACKAGE_ROOT,
) -> tuple[tuple[str, int, str], ...]:
    violations = []
    targets = _protected_presence_files(package_root) if files is None else files
    for path in targets:
        relative = _relative(path, package_root)
        for node in ast.walk(_tree(path)):
            if any(
                _stores_writer_reference(value)
                for value in _assigned_values(node)
            ):
                violations.append((relative, node.lineno, "writer alias"))
            if isinstance(node, ast.Call):
                mechanism = _call_mechanism(node)
                if mechanism is not None:
                    violations.append((relative, node.lineno, mechanism))
    return tuple(sorted(violations))


def _voice_table_names() -> frozenset[str]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    try:
        apply_migrations(conn)
        return frozenset(
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'voice_%'"
            )
        )
    finally:
        conn.close()


def test_final_migrated_schema_has_exact_voice_tables() -> None:
    assert _voice_table_names() == EXPECTED_VOICE_TABLES


def test_adapter_cannot_import_database_network_credentials_or_analysis() -> None:
    assert runtime_import_violations(VOICE_ADAPTER_FILES) == ()


def test_protected_sql_writers_have_canonical_owners() -> None:
    assert direct_sql_write_violations() == ()


def test_only_review_service_calls_canonical_presence_writer_directly() -> None:
    assert direct_review_writer_calls() == APPROVED_REVIEW_WRITER_CALLERS


def test_protected_modules_do_not_alias_or_dynamically_dispatch_writer() -> None:
    assert writer_convention_violations() == ()


@pytest.mark.parametrize(
    ("relative", "table"),
    (
        ("voice/process.py", "transcript_segments"),
        ("services/presence_mutation.py", "analysis_runs"),
    ),
)
def test_finite_guard_protects_complete_presence_production_surface(
    tmp_path: Path,
    relative: str,
    table: str,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / relative
    mutation.parent.mkdir(parents=True)
    mutation.write_text(
        f"def mutate(conn):\n"
        f"    conn.execute(\"INSERT INTO {table}(id) VALUES (1)\")\n",
        encoding="utf-8",
    )

    assert direct_sql_write_violations(
        (mutation,), package_root=package_root
    ) == ((relative, 2, table),)


def test_finite_guard_detects_direct_protected_sql_mutation(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / "services" / "presence_mutation.py"
    mutation.parent.mkdir(parents=True)
    mutation.write_text(
        "def mutate(conn):\n"
        "    conn.execute(\"INSERT INTO presence_decisions(id) VALUES (1)\")\n",
        encoding="utf-8",
    )

    assert direct_sql_write_violations(
        (mutation,), package_root=package_root
    ) == (("services/presence_mutation.py", 2, "presence_decisions"),)


def test_finite_guard_detects_downstream_sql_in_presence_module(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / "workers" / "presence_verification.py"
    mutation.parent.mkdir(parents=True)
    mutation.write_text(
        "def mutate(conn):\n"
        "    conn.execute(\"INSERT INTO analysis_runs(id) VALUES (1)\")\n",
        encoding="utf-8",
    )

    assert direct_sql_write_violations(
        (mutation,), package_root=package_root
    ) == (("workers/presence_verification.py", 2, "analysis_runs"),)


def test_finite_guard_reports_unapproved_direct_review_writer_call(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / "workers" / "presence_verification.py"
    mutation.parent.mkdir(parents=True)
    mutation.write_text(
        "def mutate(repository, command):\n"
        "    repository.add_review_and_decision(command)\n",
        encoding="utf-8",
    )

    assert direct_review_writer_calls(
        (mutation,), package_root=package_root
    ) == ("workers/presence_verification.py:mutate",)


def test_finite_guard_detects_writer_alias_and_dynamic_dispatch(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / "services" / "voice_verification.py"
    mutation.parent.mkdir(parents=True)
    mutation.write_text(
        "import functools\n"
        "def mutate(repository, command):\n"
        "    writer = repository.add_review_and_decision\n"
        "    getattr(repository, 'add_review_and_decision')(command)\n"
        "    setattr(repository, 'writer', repository.add_review_and_decision)\n"
        "    functools.partial(repository.add_review_and_decision, command)\n"
        "    eval('repository.add_review_and_decision(command)')\n"
        "    exec('repository.add_review_and_decision(command)')\n",
        encoding="utf-8",
    )

    assert writer_convention_violations(
        (mutation,), package_root=package_root
    ) == (
        ("services/voice_verification.py", 3, "writer alias"),
        ("services/voice_verification.py", 4, "getattr"),
        ("services/voice_verification.py", 5, "setattr"),
        ("services/voice_verification.py", 6, "functools.partial"),
        ("services/voice_verification.py", 7, "eval"),
        ("services/voice_verification.py", 8, "exec"),
    )


@pytest.mark.parametrize(
    ("source", "display"),
    (
        ("def mutate():\n    import sqlite3 as storage\n", "sqlite3"),
        (
            "import importlib\n"
            "def mutate():\n"
            "    importlib.import_module('subprocess')\n",
            "subprocess",
        ),
    ),
)
def test_adapter_guard_detects_direct_disallowed_imports(
    tmp_path: Path,
    source: str,
    display: str,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    mutation = package_root / "voice" / "adapter_main.py"
    mutation.parent.mkdir(parents=True)
    mutation.write_text(source, encoding="utf-8")

    violations = runtime_import_violations(
        (mutation,), package_root=package_root
    )
    assert tuple(item[2] for item in violations) == (display,)


def test_architecture_lint_remains_finite() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    class_names = {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
    }
    forbidden_interpreter_markers = {
        "BindingEvent",
        "ExecutableScope",
        "LexicalScope",
        "ParameterSelector",
        "ResolvedCallable",
        "ReviewFormal",
        "ReviewResolver",
        "ReviewValue",
        "ScopeBuilder",
    }

    assert not any(
        marker in name
        for name in class_names
        for marker in forbidden_interpreter_markers
    )
    assert len(source.splitlines()) <= 600
