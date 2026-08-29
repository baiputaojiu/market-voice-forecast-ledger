from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from market_voice_forecast_ledger.db.migrate import apply_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "market_voice_forecast_ledger"
TEST_ROOT = PROJECT_ROOT / "tests" / "backend"
CUTOVER_SQL = (
    PACKAGE_ROOT / "db" / "migrations" / "0018_youtube_discovery_cutover.sql"
)
LEGACY_SCHEMA_NAMES = frozenset(
    {
        "analysis_run_segments_policy_run",
        "bound_video_eligibility_identity_immutable",
        "bound_video_eligibility_no_delete",
        "subject_channel_policies",
        "subject_video_eligibility",
        "video_pipeline_job_bindings_eligibility",
    }
)
LEGACY_SCHEMA_VOCABULARY = re.compile(
    r"(?:"
    r"organization_assigned_statement|organization_assignment|"
    r"channel_organization|fixed_channel|all_channels|"
    r"subject_channel_polic(?:y|ies)|subject_video_eligibility|"
    r"eligibility_status|['\"]organization['\"]"
    r")",
    re.IGNORECASE,
)
LEGACY_SYMBOL_PATTERN = re.compile(
    r"^(?:"
    r"ALL_CHANNELS|CHANNEL_ORGANIZATION|FIXED_CHANNEL|ORGANIZATION|"
    r"AllChannels|ChannelOrganization|EligibilityStatus|FixedChannel|"
    r"OrganizationAssignment(?:Service)?|PolicyKind|"
    r"SubjectChannelPolic(?:y|ies)|SubjectVideoEligibility"
    r")$"
)
SUBJECT_LITERALS = frozenset(
    {
        "木野内栄治",
        "大川智宏",
        "江守哲",
        "千竈 鉄平",
        "UCXvjRTXoDa8tKwdkTaukGug",
        "UCVXka7buS_WptsAzSE0LcKg",
        "UCOfzLmXpI3qmZfV7_Cs1sYA",
    }
)


def _python_files(roots: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for root in roots
            for path in root.rglob("*.py")
            if path.resolve() != Path(__file__).resolve()
        )
    )


def _relative(path: Path) -> str:
    try:
        return path.relative_to(PACKAGE_ROOT).as_posix()
    except ValueError:
        return path.relative_to(PROJECT_ROOT).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _identifier_names(tree: ast.AST) -> Iterable[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.lineno, node.name


def _runtime_matches(pattern: re.Pattern[str]) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (relative, lineno, name)
        for path in _python_files((PACKAGE_ROOT, TEST_ROOT))
        for relative in (_relative(path),)
        for lineno, name in _identifier_names(_tree(path))
        if pattern.fullmatch(name) is not None
    )


def _orchestrator_subject_conditionals() -> tuple[tuple[int, str], ...]:
    path = PACKAGE_ROOT / "services" / "youtube_sync.py"
    matches = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.If, ast.IfExp)):
            condition = node.test
        elif isinstance(node, ast.Match):
            condition = node.subject
        else:
            continue
        literals = {
            item.value
            for item in ast.walk(condition)
            if isinstance(item, ast.Constant) and type(item.value) is str
        }
        compares_subject_identity = any(
            isinstance(compare, ast.Compare)
            and any(
                (
                    isinstance(value, ast.Name) and value.id == "subject_id"
                )
                or (
                    isinstance(value, ast.Attribute)
                    and value.attr == "subject_id"
                )
                for value in (compare.left, *compare.comparators)
            )
            and any(
                isinstance(
                    operator,
                    (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot),
                )
                for operator in compare.ops
            )
            for compare in ast.walk(condition)
        )
        matches_subject_identity = isinstance(node, ast.Match) and any(
            (isinstance(value, ast.Name) and value.id == "subject_id")
            or (isinstance(value, ast.Attribute) and value.attr == "subject_id")
            for value in ast.walk(condition)
        )
        if (
            literals & SUBJECT_LITERALS
            or compares_subject_identity
            or matches_subject_identity
        ):
            matches.append((node.lineno, ast.unparse(condition)))
    return tuple(matches)


def _imported_names(
    node: ast.Import | ast.ImportFrom,
    relative: str,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if node.level:
        package_parts = (
            PACKAGE_ROOT.name,
            *Path(relative).parent.parts,
        )
        keep = len(package_parts) - (node.level - 1)
        prefix_parts = package_parts[: max(keep, 0)]
        module_parts = tuple((node.module or "").split("."))
        resolved_parts = (*prefix_parts, *(part for part in module_parts if part))
        module = ".".join(resolved_parts)
    else:
        module = node.module or ""
    return tuple(
        f"{module}.{alias.name}" if module else alias.name
        for alias in node.names
    )


def _type_checking_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    direct_names = set()
    module_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(
                alias.asname or "typing"
                for alias in node.names
                if alias.name == "typing"
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "typing"
        ):
            direct_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "TYPE_CHECKING"
            )
    return frozenset(direct_names), frozenset(module_names)


def _is_type_checking_guard(
    node: ast.AST,
    direct_names: frozenset[str],
    module_names: frozenset[str],
) -> bool:
    return (
        isinstance(node, ast.Name) and node.id in direct_names
    ) or (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id in module_names
    )


def _runtime_import_nodes(tree: ast.AST) -> Iterable[ast.Import | ast.ImportFrom]:
    direct_names, module_names = _type_checking_bindings(tree)

    def visit(node: ast.AST) -> Iterable[ast.AST]:
        yield node
        if isinstance(node, ast.If) and _is_type_checking_guard(
            node.test,
            direct_names,
            module_names,
        ):
            for child in node.orelse:
                yield from visit(child)
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    yield from (
        node
        for node in visit(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )


def _runtime_import_names(
    tree: ast.AST, relative: str
) -> Iterable[tuple[int, str, str]]:
    import_nodes = tuple(_runtime_import_nodes(tree))
    importlib_modules = {
        alias.asname or alias.name
        for node in import_nodes
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "importlib"
    }
    import_functions = {
        alias.asname or alias.name
        for node in import_nodes
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "importlib"
        for alias in node.names
        if alias.name == "import_module"
    }
    for node in import_nodes:
        for imported in _imported_names(node, relative):
            yield node.lineno, imported, "direct"
    direct_names, module_names = _type_checking_bindings(tree)

    def visit(node: ast.AST) -> Iterable[ast.AST]:
        yield node
        if isinstance(node, ast.If) and _is_type_checking_guard(
            node.test, direct_names, module_names
        ):
            for child in node.orelse:
                yield from visit(child)
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    for node in visit(tree):
        if (
            not isinstance(node, ast.Call)
            or not node.args
            or not isinstance(node.args[0], ast.Constant)
            or type(node.args[0].value) is not str
        ):
            continue
        is_importlib = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_modules
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in import_functions
        )
        is_builtin = isinstance(node.func, ast.Name) and node.func.id == "__import__"
        if is_importlib or is_builtin:
            yield node.lineno, _resolve_dynamic_import(
                node.args[0].value, relative
            ), "importlib" if is_importlib else "builtin"


def _resolve_dynamic_import(name: str, relative: str) -> str:
    if not name.startswith("."):
        return name
    level = len(name) - len(name.lstrip("."))
    package_parts = (PACKAGE_ROOT.name, *Path(relative).parent.parts)
    keep = len(package_parts) - (level - 1)
    suffix = tuple(part for part in name[level:].split(".") if part)
    return ".".join((*package_parts[: max(keep, 0)], *suffix))


def _matches_import_root(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in roots)


def _imports_outside(
    *,
    forbidden: Callable[[str], bool],
    allowed_paths: tuple[str, ...],
    allowed_imports: tuple[tuple[str, str, str], ...] = (),
) -> tuple[tuple[str, int, str], ...]:
    matches = []
    for path in _python_files((PACKAGE_ROOT,)):
        relative = _relative(path)
        tree = _tree(path)
        for lineno, imported, kind in _runtime_import_names(tree, relative):
            if (
                forbidden(imported)
                and relative not in allowed_paths
                and (relative, imported, kind) not in allowed_imports
            ):
                matches.append((relative, lineno, imported))
    return tuple(matches)


def _network_imports_outside(
    allowed_path: str,
) -> tuple[tuple[str, int, str], ...]:
    network_roots = (
        "aiohttp",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "_socket",
        "urllib.error",
        "urllib.request",
    )
    return _imports_outside(
        forbidden=lambda name: _matches_import_root(name, network_roots),
        allowed_paths=(allowed_path,),
        allowed_imports=(
            ("voice/adapter_main.py", "socket", "direct"),
            ("voice/adapter_main.py", "_socket", "direct"),
        ),
    )


def _native_credential_imports_outside(
    allowed_paths: tuple[str, ...],
) -> tuple[tuple[str, int, str], ...]:
    return _imports_outside(
        forbidden=lambda name: (
            name == "ctypes"
            or name.startswith("win32cred")
            or name.startswith(
                "market_voice_forecast_ledger.credentials.windows"
            )
        ),
        allowed_paths=allowed_paths,
    )


def _scheduler_imports_outside(
    allowed_paths: tuple[str, ...],
) -> tuple[tuple[str, int, str], ...]:
    return _imports_outside(
        forbidden=lambda name: (
            _matches_import_root(
                name, ("subprocess", "win32com", "pythoncom")
            )
            or name == (
                "market_voice_forecast_ledger.windows.task_scheduler"
            )
            or name.endswith(".TaskSchedulerAdapter")
        ),
        allowed_paths=allowed_paths,
        allowed_imports=(
            ("voice/media.py", "subprocess", "direct"),
            ("voice/process.py", "subprocess", "direct"),
            ("pc_transfer/checkpoint.py", "subprocess", "direct"),
            ("pc_transfer/portable.py", "subprocess", "direct"),
            ("pc_transfer/runtime_rebuild.py", "subprocess", "direct"),
        ),
    )


def _sherpa_imports_outside() -> tuple[tuple[str, int, str], ...]:
    return _imports_outside(
        forbidden=lambda name: _matches_import_root(name, ("sherpa_onnx",)),
        allowed_paths=(),
        allowed_imports=(
            ("voice/adapter_main.py", "sherpa_onnx", "importlib"),
        ),
    )


def _database_imports_in_discoverers() -> tuple[tuple[str, int, str], ...]:
    matches = []
    forbidden_roots = (
        "market_voice_forecast_ledger.db",
        "market_voice_forecast_ledger.repositories",
    )
    for relative in ("youtube/discovery.py", "youtube/metadata.py"):
        path = PACKAGE_ROOT / Path(relative)
        for lineno, imported, _dynamic in _runtime_import_names(
            _tree(path), relative
        ):
            if _matches_import_root(
                imported, ("sqlite3", *forbidden_roots)
            ):
                matches.append((relative, lineno, imported))
    return tuple(matches)


def _subject_specific_collector_classes() -> tuple[tuple[str, int, str], ...]:
    matches = []
    for path in _python_files((PACKAGE_ROOT,)):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.endswith("Collector") and node.name != "YouTubeCollector":
                matches.append((_relative(path), node.lineno, node.name))
    return tuple(matches)


def _current_schema_names() -> frozenset[str]:
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
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            )
        )
    finally:
        conn.close()


def _legacy_current_schema_definitions() -> tuple[tuple[str, str], ...]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    try:
        apply_migrations(conn)
        return tuple(
            (row["name"], row["sql"])
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL ORDER BY type, name"
            )
            if LEGACY_SCHEMA_VOCABULARY.search(row["sql"]) is not None
        )
    finally:
        conn.close()


def _legacy_cutover_non_drop_references() -> tuple[str, ...]:
    sql = CUTOVER_SQL.read_text(encoding="utf-8")
    violations = []
    for statement in sql.split(";"):
        names = tuple(name for name in LEGACY_SCHEMA_NAMES if name in statement)
        if names and not statement.strip().upper().startswith("DROP "):
            violations.extend(names)
    return tuple(sorted(set(violations)))


def test_final_migrated_schema_has_no_legacy_objects():
    current_schema_names = _current_schema_names()
    assert not LEGACY_SCHEMA_NAMES & current_schema_names


def test_final_migrated_schema_definitions_have_no_legacy_vocabulary():
    assert _legacy_current_schema_definitions() == ()


def test_cutover_sql_names_legacy_objects_only_in_drop_statements():
    assert _legacy_cutover_non_drop_references() == ()


def test_current_python_runtime_and_tests_have_no_legacy_symbols():
    assert not _runtime_matches(LEGACY_SYMBOL_PATTERN)


def test_youtube_sync_orchestrator_has_no_subject_specific_conditionals():
    assert _orchestrator_subject_conditionals() == ()


def test_network_io_imports_are_confined_to_youtube_client():
    assert _network_imports_outside("youtube/client.py") == ()


def test_discoverers_do_not_import_database_or_repositories():
    assert _database_imports_in_discoverers() == ()


def test_native_credential_imports_stay_in_approved_composition_roots():
    assert _native_credential_imports_outside(
        ("credentials/windows.py", "cli.py", "workers/scheduled_sync.py")
    ) == ()


def test_scheduler_native_imports_stay_in_approved_composition_roots():
    assert _scheduler_imports_outside(
        (
            "windows/task_scheduler.py",
            "cli.py",
            "api/dependencies.py",
            "workers/scheduled_sync.py",
            "pc_transfer/cli.py",
        )
    ) == ()


def test_sherpa_import_is_only_literal_dynamic_in_adapter_entrypoint():
    assert _sherpa_imports_outside() == ()


def test_no_per_subject_collector_classes_exist():
    assert _subject_specific_collector_classes() == ()


def test_subject_branch_guard_detects_indirect_membership_mutation(monkeypatch):
    tree = ast.parse(
        "def mutate(profile, special_subject_ids):\n"
        "    if profile.subject_id in special_subject_ids:\n"
        "        return 'special'\n"
    )
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert _orchestrator_subject_conditionals() == (
        (2, "profile.subject_id in special_subject_ids"),
    )


def test_scheduler_guard_detects_local_and_module_root_import_mutations(
    monkeypatch,
):
    tree = ast.parse(
        "def mutate():\n"
        "    import subprocess\n"
        "from market_voice_forecast_ledger.windows import task_scheduler\n"
        "import market_voice_forecast_ledger.windows.task_scheduler\n"
        "from ..windows import task_scheduler\n"
    )
    fake_path = Path("mutation.py")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: (fake_path,))
    monkeypatch.setitem(globals(), "_relative", lambda _path: "services/mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert set(_scheduler_imports_outside(())) == {
        ("services/mutation.py", 2, "subprocess"),
        (
            "services/mutation.py",
            3,
            "market_voice_forecast_ledger.windows.task_scheduler",
        ),
        (
            "services/mutation.py",
            4,
            "market_voice_forecast_ledger.windows.task_scheduler",
        ),
        (
            "services/mutation.py",
            5,
            "market_voice_forecast_ledger.windows.task_scheduler",
        ),
    }


def test_scheduler_guard_detects_imported_symbols_and_literal_dynamic_imports(
    monkeypatch,
):
    tree = ast.parse(
        "from subprocess import Popen as Run\n"
        "import importlib as loader\n"
        "from importlib import import_module as load\n"
        "def mutate():\n"
        "    loader.import_module('subprocess')\n"
        "    load('win32com.client')\n"
    )
    fake_path = Path("mutation.py")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: (fake_path,))
    monkeypatch.setitem(globals(), "_relative", lambda _path: "services/mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert set(_scheduler_imports_outside(())) == {
        ("services/mutation.py", 1, "subprocess.Popen"),
        ("services/mutation.py", 5, "subprocess"),
        ("services/mutation.py", 6, "win32com.client"),
    }


def test_network_guard_allows_only_adapter_socket_import_mutation(monkeypatch):
    tree = ast.parse("import socket\nimport requests\n")
    fake_path = Path("mutation.py")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: (fake_path,))
    monkeypatch.setitem(globals(), "_relative", lambda _path: "voice/adapter_main.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert _network_imports_outside("youtube/client.py") == (
        ("voice/adapter_main.py", 2, "requests"),
    )


def test_network_guard_rejects_symbol_dynamic_and_adjacent_low_level_mutations(
    monkeypatch,
):
    paths = (Path("adapter.py"), Path("other.py"))
    relatives = {
        "adapter.py": "voice/adapter_main.py",
        "other.py": "voice/other.py",
    }
    trees = {
        "adapter.py": ast.parse(
            "from socket import socket as Socket\n"
            "import _socket as low_level\n"
            "import importlib as loader\n"
            "loader.import_module('requests.sessions')\n"
            "loader.import_module('sherpa_onnx')\n"
        ),
        "other.py": ast.parse("def load():\n    import _socket as low\n"),
    }
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: paths)
    monkeypatch.setitem(
        globals(), "_relative", lambda path: relatives[path.name]
    )
    monkeypatch.setitem(globals(), "_tree", lambda path: trees[path.name])

    assert set(_network_imports_outside("youtube/client.py")) == {
        ("voice/adapter_main.py", 1, "socket.socket"),
        ("voice/adapter_main.py", 4, "requests.sessions"),
        ("voice/other.py", 2, "_socket"),
    }


def test_scheduler_guard_allows_only_exact_voice_subprocess_imports(monkeypatch):
    paths = (Path("media.py"), Path("process.py"), Path("other.py"))
    relatives = {
        "media.py": "voice/media.py",
        "process.py": "voice/process.py",
        "other.py": "voice/other.py",
    }
    tree = ast.parse("import subprocess\nimport win32com\n")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: paths)
    monkeypatch.setitem(
        globals(), "_relative", lambda path: relatives[path.name]
    )
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert set(_scheduler_imports_outside(())) == {
        ("voice/media.py", 2, "win32com"),
        ("voice/process.py", 2, "win32com"),
        ("voice/other.py", 1, "subprocess"),
        ("voice/other.py", 2, "win32com"),
    }


def test_scheduler_guard_allows_only_exact_transfer_process_adapters(
    monkeypatch,
):
    paths = tuple(
        Path(name)
        for name in ("checkpoint", "portable", "runtime", "bundle")
    )
    relatives = {
        "checkpoint": "pc_transfer/checkpoint.py",
        "portable": "pc_transfer/portable.py",
        "runtime": "pc_transfer/runtime_rebuild.py",
        "bundle": "pc_transfer/bundle.py",
    }
    tree = ast.parse("import subprocess\n")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: paths)
    monkeypatch.setitem(
        globals(), "_relative", lambda path: relatives[path.name]
    )
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert _scheduler_imports_outside(()) == (
        ("pc_transfer/bundle.py", 1, "subprocess"),
    )


def test_scheduler_guard_rejects_imported_subprocess_symbol_in_voice_root(
    monkeypatch,
):
    fake_path = Path("media.py")
    tree = ast.parse("from subprocess import Popen as Run\n")
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: (fake_path,))
    monkeypatch.setitem(globals(), "_relative", lambda _path: "voice/media.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert _scheduler_imports_outside(()) == (
        ("voice/media.py", 1, "subprocess.Popen"),
    )


def test_sherpa_guard_allows_only_adapter_literal_dynamic_import(monkeypatch):
    paths = (Path("adapter.py"), Path("other.py"))
    relatives = {
        "adapter.py": "voice/adapter_main.py",
        "other.py": "voice/other.py",
    }
    trees = {
        "adapter.py": ast.parse(
            "import importlib\n"
            "import sherpa_onnx\n"
            "importlib.import_module('sherpa_onnx')\n"
            "__import__('sherpa_onnx')\n"
        ),
        "other.py": ast.parse(
            "from importlib import import_module as load\n"
            "load('sherpa_onnx')\n"
        ),
    }
    monkeypatch.setitem(globals(), "_python_files", lambda _roots: paths)
    monkeypatch.setitem(
        globals(), "_relative", lambda path: relatives[path.name]
    )
    monkeypatch.setitem(globals(), "_tree", lambda path: trees[path.name])

    assert set(_sherpa_imports_outside()) == {
        ("voice/adapter_main.py", 2, "sherpa_onnx"),
        ("voice/adapter_main.py", 4, "sherpa_onnx"),
        ("voice/other.py", 2, "sherpa_onnx"),
    }


def test_discoverer_guard_detects_database_root_module_mutations(monkeypatch):
    tree = ast.parse(
        "from market_voice_forecast_ledger import db, repositories\n"
        "from .. import db, repositories\n"
        "from sqlite3 import connect as open_database\n"
        "from importlib import import_module as load\n"
        "def mutate():\n"
        "    return load('sqlite3')\n"
    )
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert set(_database_imports_in_discoverers()) == {
        (
            "youtube/discovery.py",
            1,
            "market_voice_forecast_ledger.db",
        ),
        (
            "youtube/discovery.py",
            1,
            "market_voice_forecast_ledger.repositories",
        ),
        (
            "youtube/discovery.py",
            2,
            "market_voice_forecast_ledger.db",
        ),
        (
            "youtube/discovery.py",
            2,
            "market_voice_forecast_ledger.repositories",
        ),
        (
            "youtube/metadata.py",
            1,
            "market_voice_forecast_ledger.db",
        ),
        (
            "youtube/metadata.py",
            1,
            "market_voice_forecast_ledger.repositories",
        ),
        (
            "youtube/metadata.py",
            2,
            "market_voice_forecast_ledger.db",
        ),
        (
            "youtube/metadata.py",
            2,
            "market_voice_forecast_ledger.repositories",
        ),
        ("youtube/discovery.py", 3, "sqlite3.connect"),
        ("youtube/discovery.py", 6, "sqlite3"),
        ("youtube/metadata.py", 3, "sqlite3.connect"),
        ("youtube/metadata.py", 6, "sqlite3"),
    }


def test_discoverer_guard_ignores_protocol_and_type_only_imports(monkeypatch):
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECKING\n"
        "import typing as type_hints\n"
        "from market_voice_forecast_ledger.youtube.protocols import Clock\n"
        "if CHECKING:\n"
        "    import sqlite3\n"
        "if type_hints.TYPE_CHECKING:\n"
        "    from market_voice_forecast_ledger import db\n"
        "if runtime_flags.TYPE_CHECKING:\n"
        "    import sqlite3\n"
    )
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)

    assert set(_database_imports_in_discoverers()) == {
        ("youtube/discovery.py", 9, "sqlite3"),
        ("youtube/metadata.py", 9, "sqlite3"),
    }
