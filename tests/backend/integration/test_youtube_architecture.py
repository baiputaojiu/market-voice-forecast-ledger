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

    def visit(node: ast.AST) -> Iterable[ast.Import | ast.ImportFrom]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            return
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

    yield from visit(tree)


def _imports_outside(
    *,
    forbidden: Callable[[str], bool],
    allowed_paths: tuple[str, ...],
) -> tuple[tuple[str, int, str], ...]:
    matches = []
    for path in _python_files((PACKAGE_ROOT,)):
        relative = _relative(path)
        tree = _tree(path)
        for node in _runtime_import_nodes(tree):
            for imported in _imported_names(node, relative):
                if forbidden(imported) and relative not in allowed_paths:
                    matches.append((relative, node.lineno, imported))
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
        "urllib.error",
        "urllib.request",
    )
    return _imports_outside(
        forbidden=lambda name: name.startswith(network_roots),
        allowed_paths=(allowed_path,),
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
            name in {"subprocess", "win32com", "pythoncom"}
            or name == (
                "market_voice_forecast_ledger.windows.task_scheduler"
            )
            or name.endswith(".TaskSchedulerAdapter")
        ),
        allowed_paths=allowed_paths,
    )


def _database_imports_in_discoverers() -> tuple[tuple[str, int, str], ...]:
    matches = []
    forbidden_roots = (
        "market_voice_forecast_ledger.db",
        "market_voice_forecast_ledger.repositories",
    )
    for relative in ("youtube/discovery.py", "youtube/metadata.py"):
        path = PACKAGE_ROOT / Path(relative)
        for node in _runtime_import_nodes(_tree(path)):
            for imported in _imported_names(node, relative):
                if (
                    imported == "sqlite3"
                    or any(
                        imported == root or imported.startswith(f"{root}.")
                        for root in forbidden_roots
                    )
                ):
                    matches.append((relative, node.lineno, imported))
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
        )
    ) == ()


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


def test_discoverer_guard_detects_database_root_module_mutations(monkeypatch):
    tree = ast.parse(
        "from market_voice_forecast_ledger import db, repositories\n"
        "from .. import db, repositories\n"
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
