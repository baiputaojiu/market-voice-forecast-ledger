from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.migrate import apply_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "market_voice_forecast_ledger"
VOICE_ADAPTER_FILES = (PACKAGE_ROOT / "voice" / "adapter_main.py",)
PRESENCE_RUNTIME_FILES = tuple(
    sorted(
        (
            PACKAGE_ROOT / "domain" / "voice_verification.py",
            PACKAGE_ROOT / "repositories" / "voice_verification.py",
            PACKAGE_ROOT / "services" / "voice_reference.py",
            PACKAGE_ROOT / "services" / "voice_verification.py",
            PACKAGE_ROOT / "workers" / "presence_verification.py",
            *(PACKAGE_ROOT / "voice").glob("*.py"),
        )
    )
)
PRODUCTION_FILES = tuple(sorted(PACKAGE_ROOT.rglob("*.py")))
EXPECTED_VOICE_TABLES = frozenset(
    {
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


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE_ROOT).as_posix()


def _type_checking_bindings(
    tree: ast.Module,
) -> tuple[frozenset[str], frozenset[str]]:
    assigned = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    assigned.update(
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    )
    direct_names = set()
    module_names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "typing"
                and (alias.asname or alias.name) not in assigned
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
                and (alias.asname or alias.name) not in assigned
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


def _runtime_nodes(tree: ast.Module) -> Iterable[ast.AST]:
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

    yield from visit(tree)


def _resolve_relative_import(name: str, relative: str) -> str:
    if not name.startswith("."):
        return name
    level = len(name) - len(name.lstrip("."))
    package_parts = (PACKAGE_ROOT.name, *Path(relative).parent.parts)
    keep = len(package_parts) - (level - 1)
    suffix = tuple(part for part in name[level:].split(".") if part)
    return ".".join((*package_parts[: max(keep, 0)], *suffix))


def _imported_names(
    node: ast.Import | ast.ImportFrom,
    relative: str,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    prefix = "." * node.level + (node.module or "")
    module = _resolve_relative_import(prefix, relative) if node.level else prefix
    return tuple(
        f"{module}.{alias.name}" if module else alias.name
        for alias in node.names
    )


def _static_string(
    node: ast.AST,
    constants: dict[str, str] | None = None,
) -> str | None:
    values = constants or {}
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, values)
        right = _static_string(node.right, values)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            target = value.value if isinstance(value, ast.FormattedValue) else value
            rendered = _static_string(target, values)
            if rendered is None:
                return None
            parts.append(rendered)
        return "".join(parts)
    return None


def _dynamic_import_bindings(
    nodes: tuple[ast.AST, ...],
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    importlib_modules = set()
    import_functions = set()
    builtin_functions = {"__import__"}
    for node in nodes:
        if isinstance(node, ast.Import):
            importlib_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "importlib"
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == "importlib":
                import_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
            elif node.module == "builtins":
                builtin_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "__import__"
                )
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            if value is None:
                continue
            names = {
                target.id for target in targets if isinstance(target, ast.Name)
            }
            is_import_module = (
                isinstance(value, ast.Attribute)
                and value.attr == "import_module"
                and isinstance(value.value, ast.Name)
                and value.value.id in importlib_modules
            ) or (
                isinstance(value, ast.Name) and value.id in import_functions
            )
            is_builtin = (
                isinstance(value, ast.Name) and value.id in builtin_functions
            )
            destination = import_functions if is_import_module else builtin_functions
            if (is_import_module or is_builtin) and not names <= destination:
                destination.update(names)
                changed = True
    return (
        frozenset(importlib_modules),
        frozenset(import_functions),
        frozenset(builtin_functions),
    )


def _runtime_import_names(
    tree: ast.Module,
    relative: str,
) -> Iterable[tuple[int, str, str]]:
    nodes = tuple(_runtime_nodes(tree))
    constants = _string_constants(nodes)
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for imported in _imported_names(node, relative):
                yield node.lineno, imported, "direct"
    importlib_modules, import_functions, builtin_functions = (
        _dynamic_import_bindings(nodes)
    )
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        argument = node.args[0] if node.args else next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg in {"name", "module"}
            ),
            None,
        )
        if argument is None:
            continue
        name = _static_string(argument, constants)
        if name is None:
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
        is_builtin = (
            isinstance(node.func, ast.Name)
            and node.func.id in builtin_functions
        )
        if is_importlib or is_builtin:
            yield (
                node.lineno,
                _resolve_relative_import(name, relative),
                "importlib" if is_importlib else "builtin",
            )


def _is_forbidden_adapter_import(name: str) -> bool:
    project_prefix = f"{PACKAGE_ROOT.name}."
    allowed_project = (
        f"{PACKAGE_ROOT.name}.domain.common",
        f"{PACKAGE_ROOT.name}.domain.errors",
        f"{PACKAGE_ROOT.name}.domain.voice_verification",
        f"{PACKAGE_ROOT.name}.voice.protocol",
    )
    if name == PACKAGE_ROOT.name or name.startswith(project_prefix):
        return not any(
            name == allowed or name.startswith(f"{allowed}.")
            for allowed in allowed_project
        )
    allowed_safe_modules = (
        "array",
        "base64",
        "collections.abc",
        "hashlib",
        "importlib",
        "json",
        "math",
        "pathlib",
        "struct",
        "sys",
        "time",
        "typing",
        "wave",
    )
    return not any(
        name == allowed or name.startswith(f"{allowed}.")
        for allowed in allowed_safe_modules
    )


def runtime_import_violations(
    files: Iterable[Path],
) -> tuple[tuple[str, int, str], ...]:
    violations = []
    for path in files:
        relative = _relative(path)
        for lineno, imported, kind in _runtime_import_names(
            _tree(path), relative
        ):
            allowed_socket = (
                relative == "voice/adapter_main.py"
                and imported in {"socket", "_socket"}
                and kind == "direct"
            )
            allowed_sherpa = (
                relative == "voice/adapter_main.py"
                and imported == "sherpa_onnx"
                and kind == "importlib"
            )
            if (
                not allowed_socket
                and not allowed_sherpa
                and _is_forbidden_adapter_import(imported)
            ):
                violations.append((relative, lineno, imported))
    return tuple(violations)


def _function_scopes(tree: ast.Module) -> tuple[tuple[str, ast.AST], ...]:
    scopes: list[tuple[str, ast.AST]] = []

    def visit(body: list[ast.stmt], owners: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*owners, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join((*owners, node.name))
                scopes.append((name, node))
                visit(node.body, (*owners, node.name))

    visit(tree.body, ())
    return tuple(scopes)


def _scope_nodes(scope: ast.AST) -> tuple[ast.AST, ...]:
    nodes = [scope]

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ):
                continue
            nodes.append(child)
            visit(child)

    visit(scope)
    return tuple(nodes)


def _string_constants(nodes: Iterable[ast.AST]) -> dict[str, str]:
    assignments: dict[str, ast.AST] = {}
    repeated = set()
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            if not isinstance(target, ast.Name) or node.value is None:
                continue
            if target.id in assignments:
                repeated.add(target.id)
            else:
                assignments[target.id] = node.value
    for name in repeated:
        assignments.pop(name, None)
    constants: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, value in assignments.items():
            if name in constants:
                continue
            rendered = _static_string(value, constants)
            if rendered is None:
                continue
            constants[name] = rendered
            changed = True
    return constants


def _contains_terminal_presence(
    nodes: Iterable[ast.AST],
    semantic_names: frozenset[str],
) -> bool:
    terminal_values = {"presence_confirmed", "presence_rejected"}
    for node in nodes:
        if isinstance(node, ast.Constant) and node.value in terminal_values:
            return True
        if isinstance(node, ast.Attribute) and node.attr in {
            "CONFIRMED",
            "REJECTED",
        }:
            return True
        if isinstance(node, ast.Name) and node.id in semantic_names:
            return True
    return False


def _semantic_names(tree: ast.Module) -> frozenset[str]:
    names = set()
    changed = True
    assignments = tuple(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
    while changed:
        changed = False
        for node in assignments:
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            if value is None or not _contains_terminal_presence(
                ast.walk(value), frozenset(names)
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return frozenset(names)


def _sql_texts(
    nodes: tuple[ast.AST, ...],
    constants: dict[str, str],
) -> tuple[str, ...]:
    executor_aliases = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            value = node.value
            is_executor = (
                isinstance(value, ast.Attribute)
                and value.attr in {"execute", "executemany", "executescript"}
            ) or (
                isinstance(value, ast.Name) and value.id in executor_aliases
            )
            aliases = {
                target.id for target in targets if isinstance(target, ast.Name)
            }
            if is_executor and not aliases <= executor_aliases:
                executor_aliases.update(aliases)
                changed = True
    texts = []
    for node in nodes:
        if not isinstance(node, ast.Call) or not node.args:
            continue
        is_executor = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ) or (
            isinstance(node.func, ast.Name) and node.func.id in executor_aliases
        )
        if not is_executor:
            continue
        rendered = _static_string(node.args[0], constants)
        if rendered is not None:
            texts.append(rendered)
    return tuple(texts)


def _writes_presence_decision(sql: str) -> bool:
    inserts = re.search(
        r"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+presence_decisions\b",
        sql,
        re.IGNORECASE,
    )
    pointer = re.search(
        r"\bUPDATE\s+subject_video_candidates\b",
        sql,
        re.IGNORECASE,
    ) and re.search(r"\bcurrent_presence_decision_id\b", sql, re.IGNORECASE)
    return inserts is not None or bool(pointer)


def presence_decision_writer_calls(
    files: Iterable[Path] = PRODUCTION_FILES,
) -> tuple[str, ...]:
    writers = []
    for path in files:
        tree = _tree(path)
        module_constants = _string_constants(tree.body)
        semantic_names = _semantic_names(tree)
        for name, scope in _function_scopes(tree):
            nodes = _scope_nodes(scope)
            constants = dict(module_constants)
            constants.update(_string_constants(nodes))
            if (
                any(
                    _writes_presence_decision(sql)
                    for sql in _sql_texts(nodes, constants)
                )
                and _contains_terminal_presence(nodes, semantic_names)
            ):
                writers.append(f"{_relative(path)}:{name}")
    return tuple(sorted(writers))


def review_writer_callers(
    files: Iterable[Path] = PRODUCTION_FILES,
) -> tuple[str, ...]:
    callers = []
    for path in files:
        tree = _tree(path)
        imported_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "add_review_and_decision"
        }
        for name, scope in _function_scopes(tree):
            nodes = _scope_nodes(scope)
            aliases = set(imported_aliases)
            for node in nodes:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                value = node.value
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "add_review_and_decision"
                ):
                    aliases.update(
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    )
            calls_writer = any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_review_and_decision"
                    )
                    or (
                        isinstance(node.func, ast.Name)
                        and node.func.id in aliases
                    )
                )
                for node in nodes
            )
            if calls_writer:
                callers.append(f"{_relative(path)}:{name}")
    return tuple(sorted(callers))


def _writes_expansion(sql: str) -> bool:
    target = (
        r"(?:transcription_chunks|transcript_segments|speaker_assignments|"
        r"analysis_[a-z0-9_]+)"
    )
    return re.search(
        rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+{target}\b",
        sql,
        re.IGNORECASE,
    ) is not None


def presence_expansion_writer_calls(
    files: Iterable[Path] = PRESENCE_RUNTIME_FILES,
) -> tuple[str, ...]:
    writer_methods = {
        "add_chunk",
        "add_segment",
        "append_run_event",
        "get_or_create_scope",
        "insert_job_attempt",
        "insert_run",
        "insert_run_segments",
        "insert_snapshot",
        "save_assignment",
        "validate_and_store",
    }
    writers = []
    for path in files:
        tree = _tree(path)
        module_constants = _string_constants(tree.body)
        for name, scope in _function_scopes(tree):
            nodes = _scope_nodes(scope)
            constants = dict(module_constants)
            constants.update(_string_constants(nodes))
            sql_writer = any(
                _writes_expansion(sql) for sql in _sql_texts(nodes, constants)
            )
            method_writer = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in writer_methods
                for node in nodes
            )
            if sql_writer or method_writer:
                writers.append(f"{_relative(path)}:{name}")
    return tuple(sorted(writers))


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


def test_only_review_repository_can_write_confirmed_or_rejected_presence() -> None:
    assert presence_decision_writer_calls() == (
        "repositories/voice_verification.py:"
        "VoiceVerificationRepository.add_review_and_decision",
    )


def test_only_review_service_can_call_canonical_presence_writer() -> None:
    assert review_writer_callers() == (
        "services/voice_verification.py:PresenceVerificationService.review",
    )


def test_presence_runtime_cannot_write_transcripts_speakers_or_analysis() -> None:
    assert presence_expansion_writer_calls() == ()


def test_adapter_guard_detects_runtime_scope_alias_relative_and_dynamic_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "import typing as hints\n"
        "if CHECK:\n"
        "    from ..repositories import voice_verification\n"
        "if hints.TYPE_CHECKING:\n"
        "    import sqlite3\n"
        "if flags.TYPE_CHECKING:\n"
        "    import sqlite3 as hidden_db\n"
        "def mutate():\n"
        "    from .. import db as store\n"
        "    from market_voice_forecast_ledger.credentials import windows as creds\n"
        "    import importlib as loader\n"
        "    from importlib import import_module as load\n"
        "    loader.import_module('requests.sessions')\n"
        "    load('.repositories.analysis')\n"
        "    __import__('subprocess')\n"
        "    network_module = 'smtplib'\n"
        "    dynamic = loader.import_module\n"
        "    dynamic(network_module)\n"
        "    from sqlalchemy import create_engine as db_engine\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert set(runtime_import_violations((fake,))) == {
        ("voice/adapter_mutation.py", 8, "sqlite3"),
        (
            "voice/adapter_mutation.py",
            10,
            "market_voice_forecast_ledger.db",
        ),
        (
            "voice/adapter_mutation.py",
            11,
            "market_voice_forecast_ledger.credentials.windows",
        ),
        ("voice/adapter_mutation.py", 14, "requests.sessions"),
        (
            "voice/adapter_mutation.py",
            15,
            "market_voice_forecast_ledger.voice.repositories.analysis",
        ),
        ("voice/adapter_mutation.py", 16, "subprocess"),
        ("voice/adapter_mutation.py", 19, "smtplib"),
        ("voice/adapter_mutation.py", 20, "sqlalchemy.create_engine"),
    }


def test_adapter_guard_does_not_trust_reassigned_type_checking_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "CHECK = runtime_flag\n"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", 4, "sqlite3"),
    )


def test_adapter_guard_does_not_trust_vararg_shadowed_type_checking_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "def runtime(*CHECK):\n"
        "    if CHECK:\n"
        "        import sqlite3\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", 4, "sqlite3"),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        (
            "CONFIRMED = {'presence_confirmed', 'presence_rejected'}\n"
            "class ModelWriter:\n"
            "    def save(self, conn):\n"
            "        state = next(iter(CONFIRMED))\n"
            "        write = conn.execute\n"
            "        write('INSERT INTO presence_'\n"
            "              'decisions(candidate_id, state) VALUES (?, ?)',\n"
            "              (1, state))\n"
        ),
        (
            "class PresenceState:\n"
            "    CONFIRMED = object()\n"
            "class ModelWriter:\n"
            "    def save(self, conn):\n"
            "        state = PresenceState.CONFIRMED\n"
            "        conn.execute('INSERT INTO presence_decisions '"
            "                     '(candidate_id, state) VALUES (?, ?)', (1, state))\n"
        ),
    ),
)
def test_presence_writer_guard_detects_indirect_literal_set_and_enum_mutations(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("model_mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/model_mutation.py"
    )

    assert presence_decision_writer_calls((fake,)) == (
        "workers/model_mutation.py:ModelWriter.save",
    )


def test_presence_writer_guard_attributes_nested_runtime_scope_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "def outer():\n"
        "    def model_writer(conn):\n"
        "        state = 'presence_confirmed'\n"
        "        conn.execute('INSERT INTO presence_decisions '"
        "                     '(candidate_id, state) VALUES (?, ?)', (1, state))\n"
        "    return model_writer\n"
    )
    fake = Path("nested_mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/nested_mutation.py"
    )

    assert presence_decision_writer_calls((fake,)) == (
        "workers/nested_mutation.py:outer.model_writer",
    )


def test_review_writer_caller_guard_detects_aliased_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "class ModelWriter:\n"
        "    def save(self, repository, command):\n"
        "        write = repository.add_review_and_decision\n"
        "        return write(command)\n"
    )
    fake = Path("model_mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/model_mutation.py"
    )

    assert review_writer_callers((fake,)) == (
        "workers/model_mutation.py:ModelWriter.save",
    )


@pytest.mark.parametrize(
    "mutation",
    (
        (
            "def add_transcript(conn):\n"
            "    table = 'transcript_segments'\n"
            "    conn.execute(f'INSERT INTO {table}(id) VALUES (1)')\n"
        ),
        (
            "def assign(speakers, assignment):\n"
            "    speakers.save_assignment(assignment)\n"
        ),
        (
            "def analyze(conn):\n"
            "    conn.executemany('INSERT INTO analysis_runs(id) VALUES (?)', [])\n"
        ),
        (
            "def map_analysis(conn):\n"
            "    write = conn.execute\n"
            "    table = 'analysis_asset_mappings'\n"
            "    write(f'INSERT INTO {table}(id) VALUES (1)')\n"
        ),
    ),
)
def test_presence_expansion_guard_detects_new_runtime_writers(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("presence_mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/presence_mutation.py"
    )

    function_name = next(
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    )
    assert presence_expansion_writer_calls((fake,)) == (
        f"workers/presence_mutation.py:{function_name}",
    )
