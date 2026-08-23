from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

from market_voice_forecast_ledger.db.migrate import apply_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "market_voice_forecast_ledger"
VOICE_ADAPTER_FILES = (PACKAGE_ROOT / "voice" / "adapter_main.py",)
PRESENCE_ENTRYPOINT_FILES = (
    PACKAGE_ROOT / "services" / "voice_reference.py",
    PACKAGE_ROOT / "services" / "voice_verification.py",
    PACKAGE_ROOT / "workers" / "presence_verification.py",
    PACKAGE_ROOT / "voice" / "adapter_main.py",
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


def _resolve_relative_import(name: str, relative: str) -> str:
    if not name.startswith("."):
        return name
    level = len(name) - len(name.lstrip("."))
    package_parts = (PACKAGE_ROOT.name, *Path(relative).parent.parts)
    keep = len(package_parts) - (level - 1)
    suffix = tuple(part for part in name[level:].split(".") if part)
    return ".".join((*package_parts[: max(keep, 0)], *suffix))


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


@dataclass(frozen=True, slots=True)
class _KnownBinding:
    kind: str
    value: str | None = None


@dataclass(slots=True)
class _LexicalScope:
    kind: str
    parent: _LexicalScope | None
    events: dict[str, list[ast.AST | _KnownBinding]] = field(
        default_factory=dict
    )

    def bind(self, name: str, value: ast.AST | _KnownBinding) -> None:
        self.events.setdefault(name, []).append(value)


@dataclass(frozen=True, slots=True)
class _ImportRecord:
    lineno: int
    kind: str
    module: str
    symbol: str | None


_UNKNOWN_BINDING = _KnownBinding("unknown")
_TYPE_CHECKING_BINDING = _KnownBinding("type_checking")
_TYPING_MODULE_BINDING = _KnownBinding("typing_module")
_IMPORTLIB_MODULE_BINDING = _KnownBinding("importlib_module")
_IMPORT_MODULE_BINDING = _KnownBinding("import_module")
_BUILTINS_MODULE_BINDING = _KnownBinding("builtins_module")
_BUILTIN_IMPORT_BINDING = _KnownBinding("builtin_import")


def _binding_target_names(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    )


class _ScopeBuilder(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.module = _LexicalScope("module", None)
        self.current = self.module
        self.node_scopes: dict[int, _LexicalScope] = {}
        self.scopes = [self.module]
        self.visit(tree)

    def generic_visit(self, node: ast.AST) -> None:
        self.node_scopes[id(node)] = self.current
        super().generic_visit(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.node_scopes[id(node)] = self.current
        self.current.bind(node.name, _UNKNOWN_BINDING)
        for item in (
            *node.decorator_list,
            *node.args.defaults,
            *(
                default
                for default in node.args.kw_defaults
                if default is not None
            ),
        ):
            self.visit(item)
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *((node.args.vararg,) if node.args.vararg is not None else ()),
            *((node.args.kwarg,) if node.args.kwarg is not None else ()),
        ):
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        child = _LexicalScope("function", self.current)
        self.scopes.append(child)
        previous, self.current = self.current, child
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *((node.args.vararg,) if node.args.vararg is not None else ()),
            *((node.args.kwarg,) if node.args.kwarg is not None else ()),
        ):
            self.node_scopes[id(argument)] = child
            child.bind(argument.arg, _UNKNOWN_BINDING)
        for statement in node.body:
            self.visit(statement)
        self.current = previous

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.node_scopes[id(node)] = self.current
        self.current.bind(node.name, _UNKNOWN_BINDING)
        for item in (*node.decorator_list, *node.bases):
            self.visit(item)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        child = _LexicalScope("class", self.current)
        self.scopes.append(child)
        previous, self.current = self.current, child
        for statement in node.body:
            self.visit(statement)
        self.current = previous

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.node_scopes[id(node)] = self.current
        for default in (
            *node.args.defaults,
            *(item for item in node.args.kw_defaults if item is not None),
        ):
            self.visit(default)
        child = _LexicalScope("lambda", self.current)
        self.scopes.append(child)
        previous, self.current = self.current, child
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *((node.args.vararg,) if node.args.vararg is not None else ()),
            *((node.args.kwarg,) if node.args.kwarg is not None else ()),
        ):
            self.node_scopes[id(argument)] = child
            child.bind(argument.arg, _UNKNOWN_BINDING)
        self.visit(node.body)
        self.current = previous

    def _visit_comprehension_scope(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.generators[0].iter)
        child = _LexicalScope("comprehension", self.current)
        self.scopes.append(child)
        previous, self.current = self.current, child
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            self.visit(generator.target)
            for name in _binding_target_names(generator.target):
                child.bind(name, _UNKNOWN_BINDING)
            for condition in generator.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.current = previous

    visit_ListComp = _visit_comprehension_scope
    visit_SetComp = _visit_comprehension_scope
    visit_DictComp = _visit_comprehension_scope
    visit_GeneratorExp = _visit_comprehension_scope

    def visit_Import(self, node: ast.Import) -> None:
        self.node_scopes[id(node)] = self.current
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            known = {
                "typing": _TYPING_MODULE_BINDING,
                "importlib": _IMPORTLIB_MODULE_BINDING,
                "builtins": _BUILTINS_MODULE_BINDING,
            }.get(alias.name, _UNKNOWN_BINDING)
            self.current.bind(bound, known)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.node_scopes[id(node)] = self.current
        for alias in node.names:
            bound = alias.asname or alias.name
            known = _UNKNOWN_BINDING
            if node.level == 0 and node.module == "typing":
                if alias.name == "TYPE_CHECKING":
                    known = _TYPE_CHECKING_BINDING
            elif node.level == 0 and node.module == "importlib":
                if alias.name == "import_module":
                    known = _IMPORT_MODULE_BINDING
            elif node.level == 0 and node.module == "builtins":
                if alias.name == "__import__":
                    known = _BUILTIN_IMPORT_BINDING
            self.current.bind(bound, known)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
            for name in _binding_target_names(target):
                self.current.bind(name, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.node_scopes[id(node)] = self.current
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        for name in _binding_target_names(node.target):
            self.current.bind(name, node.value or _UNKNOWN_BINDING)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        self.visit(node.target)
        for name in _binding_target_names(node.target):
            self.current.bind(name, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        self.visit(node.target)
        for name in _binding_target_names(node.target):
            self.current.bind(name, _UNKNOWN_BINDING)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.iter)
        self.visit(node.target)
        for name in _binding_target_names(node.target):
            self.current.bind(name, _UNKNOWN_BINDING)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        self.node_scopes[id(node)] = self.current
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                for name in _binding_target_names(item.optional_vars):
                    self.current.bind(name, _UNKNOWN_BINDING)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.node_scopes[id(node)] = self.current
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self.current.bind(node.name, _UNKNOWN_BINDING)
        for statement in node.body:
            self.visit(statement)

    def visit_Global(self, node: ast.Global) -> None:
        self.node_scopes[id(node)] = self.current
        for name in node.names:
            self.current.bind(name, _UNKNOWN_BINDING)
            self.module.bind(name, _UNKNOWN_BINDING)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.node_scopes[id(node)] = self.current
        for name in node.names:
            self.current.bind(name, _UNKNOWN_BINDING)
            parent = self.current.parent
            while parent is not None and parent.kind == "class":
                parent = parent.parent
            if parent is not None:
                parent.bind(name, _UNKNOWN_BINDING)


def _outer_scope(scope: _LexicalScope) -> _LexicalScope | None:
    parent = scope.parent
    if scope.kind in {"function", "lambda", "comprehension"}:
        while parent is not None and parent.kind == "class":
            parent = parent.parent
    return parent


def _resolve_binding(
    name: str,
    scope: _LexicalScope,
    *,
    seen: frozenset[tuple[int, str]] = frozenset(),
) -> _KnownBinding:
    key = (id(scope), name)
    if key in seen:
        return _UNKNOWN_BINDING
    events = scope.events.get(name, ())
    if len(events) > 1:
        return _UNKNOWN_BINDING
    if len(events) == 1:
        event = events[0]
        if isinstance(event, _KnownBinding):
            return event
        return _evaluate_binding(event, scope, seen=seen | {key})
    parent = _outer_scope(scope)
    if parent is not None:
        return _resolve_binding(name, parent, seen=seen | {key})
    if name == "__import__":
        return _BUILTIN_IMPORT_BINDING
    return _UNKNOWN_BINDING


def _evaluate_binding(
    node: ast.AST,
    scope: _LexicalScope,
    *,
    seen: frozenset[tuple[int, str]],
) -> _KnownBinding:
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return _KnownBinding("string", node.value)
    if isinstance(node, ast.Name):
        return _resolve_binding(node.id, scope, seen=seen)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = _resolve_binding(node.value.id, scope, seen=seen)
        if owner == _TYPING_MODULE_BINDING and node.attr == "TYPE_CHECKING":
            return _TYPE_CHECKING_BINDING
        if owner == _IMPORTLIB_MODULE_BINDING and node.attr == "import_module":
            return _IMPORT_MODULE_BINDING
        if owner == _BUILTINS_MODULE_BINDING and node.attr == "__import__":
            return _BUILTIN_IMPORT_BINDING
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _evaluate_binding(node.left, scope, seen=seen)
        right = _evaluate_binding(node.right, scope, seen=seen)
        if left.kind == right.kind == "string":
            return _KnownBinding("string", f"{left.value}{right.value}")
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            target = value.value if isinstance(value, ast.FormattedValue) else value
            rendered = _evaluate_binding(target, scope, seen=seen)
            if rendered.kind != "string":
                return _UNKNOWN_BINDING
            parts.append(rendered.value or "")
        return _KnownBinding("string", "".join(parts))
    return _UNKNOWN_BINDING


def _runtime_lexical_nodes(
    tree: ast.Module,
    builder: _ScopeBuilder,
) -> tuple[ast.AST, ...]:
    nodes = []

    def visit(node: ast.AST) -> None:
        nodes.append(node)
        if isinstance(node, ast.If):
            scope = builder.node_scopes[id(node)]
            guard = _evaluate_binding(node.test, scope, seen=frozenset())
            if guard == _TYPE_CHECKING_BINDING:
                for child in node.orelse:
                    visit(child)
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return tuple(nodes)


def _resolve_dynamic_import(
    name: _KnownBinding,
    package: _KnownBinding | None,
) -> str:
    if name.kind != "string" or name.value is None:
        return "<dynamic-import>"
    if package is not None and package.kind != "string":
        return "<dynamic-import>"
    if not name.value.startswith("."):
        return name.value
    if package is None or package.value is None:
        return "<dynamic-import>"
    level = len(name.value) - len(name.value.lstrip("."))
    parts = package.value.split(".")
    if level > len(parts):
        return "<dynamic-import>"
    suffix = tuple(
        part for part in name.value[level:].split(".") if part
    )
    return ".".join((*parts[: len(parts) - level + 1], *suffix))


def _lexical_import_records(
    tree: ast.Module,
    relative: str,
) -> tuple[_ImportRecord, ...]:
    builder = _ScopeBuilder(tree)
    nodes = _runtime_lexical_nodes(tree, builder)
    records = []
    for node in nodes:
        if isinstance(node, ast.Import):
            records.extend(
                _ImportRecord(node.lineno, "import", alias.name, None)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            module = (
                _resolve_relative_import(prefix, relative)
                if node.level
                else prefix
            )
            records.extend(
                _ImportRecord(node.lineno, "from", module, alias.name)
                for alias in node.names
            )
        elif isinstance(node, ast.Call):
            scope = builder.node_scopes[id(node)]
            callable_binding = _evaluate_binding(
                node.func, scope, seen=frozenset()
            )
            if callable_binding not in {
                _IMPORT_MODULE_BINDING,
                _BUILTIN_IMPORT_BINDING,
            }:
                continue
            name_node = node.args[0] if node.args else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in {"name", "module"}
                ),
                None,
            )
            name = (
                _UNKNOWN_BINDING
                if name_node is None
                else _evaluate_binding(name_node, scope, seen=frozenset())
            )
            package_node = (
                node.args[1]
                if callable_binding == _IMPORT_MODULE_BINDING
                and len(node.args) > 1
                else next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "package"
                    ),
                    None,
                )
            )
            package = (
                None
                if package_node is None
                else _evaluate_binding(package_node, scope, seen=frozenset())
            )
            module = _resolve_dynamic_import(name, package)
            kind = (
                "dynamic"
                if callable_binding == _IMPORT_MODULE_BINDING
                else "builtin"
            )
            records.append(_ImportRecord(node.lineno, kind, module, None))
    return tuple(records)


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
        ('from', f'{PACKAGE_ROOT.name}.domain.common', 'canonical_json'),
        ('from', f'{PACKAGE_ROOT.name}.domain.common', 'sha256_text'),
        ('from', f'{PACKAGE_ROOT.name}.domain.errors', 'DomainError'),
        ('from', f'{PACKAGE_ROOT.name}.domain.voice_verification', 'VoiceProposal'),
        *(
            ('from', f'{PACKAGE_ROOT.name}.voice.protocol', symbol)
            for symbol in (
                'MAX_ADAPTER_RESPONSE_BYTES', 'MAX_ADAPTER_SEGMENTS',
                'AdapterRequest', 'AdapterResponse', 'ReferenceAudioInput',
                'ReferenceDryRunRequest', 'ReferenceDryRunResponse',
                'ReferenceEnrollmentRequest', 'ReferenceEnrollmentResponse',
                'ReferenceRequest', 'ReferenceResponse', 'ReferenceScoreRequest',
                'ReferenceScoreResponse', 'decode_reference_input_feature',
                'decode_reference_request', 'decode_reference_response',
                'decode_reference_feature', 'decode_response',
                'encode_reference_response', 'encode_request',
                'reference_feature_semantics', 'wipe_reference_feature',
            )
        ),
        ('dynamic', 'sherpa_onnx', None),
    }
)


def _display_import(record: _ImportRecord) -> str:
    if record.symbol is None:
        return record.module
    return f"{record.module}.{record.symbol}"


def runtime_import_violations(
    files: Iterable[Path],
) -> tuple[tuple[str, int, str], ...]:
    violations = []
    for path in files:
        relative = _relative(path)
        for record in _lexical_import_records(_tree(path), relative):
            identity = (record.kind, record.module, record.symbol)
            if identity not in ADAPTER_ALLOWED_IMPORTS:
                violations.append(
                    (relative, record.lineno, _display_import(record))
                )
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


def _writes_presence_decision(sql: str) -> bool:
    decision_table = (
        r'(?:presence_decisions|"presence_decisions"|'
        r"`presence_decisions`|\[presence_decisions\])"
    )
    candidate_table = (
        r'(?:subject_video_candidates|"subject_video_candidates"|'
        r"`subject_video_candidates`|\[subject_video_candidates\])"
    )
    pointer_column = (
        r'(?:current_presence_decision_id|"current_presence_decision_id"|'
        r"`current_presence_decision_id`|\[current_presence_decision_id\])"
    )
    inserts = re.search(
        rf"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+{decision_table}",
        sql,
        re.IGNORECASE,
    )
    pointer = re.search(
        rf"\bUPDATE\s+{candidate_table}",
        sql,
        re.IGNORECASE,
    ) and re.search(pointer_column, sql, re.IGNORECASE)
    return inserts is not None or bool(pointer)


@dataclass(frozen=True, slots=True)
class _ExecutableScope:
    name: str
    node: ast.AST
    positional_parameters: tuple[str, ...]


def _executable_scopes(tree: ast.Module) -> tuple[_ExecutableScope, ...]:
    scopes = [_ExecutableScope("<module>", tree, ())]

    def visit(node: ast.AST, owners: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                name = ".".join((*owners, child.name))
                scopes.append(_ExecutableScope(f"{name}.<body>", child, ()))
                visit(child, (*owners, child.name))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join((*owners, child.name))
                parameters = tuple(
                    argument.arg
                    for argument in (
                        *child.args.posonlyargs,
                        *child.args.args,
                    )
                )
                scopes.append(_ExecutableScope(name, child, parameters))
                visit(child, (*owners, child.name))
            elif isinstance(child, ast.Lambda):
                prefix = ".".join(owners)
                name = (
                    f"{prefix}.<lambda>@{child.lineno}"
                    if prefix
                    else f"<lambda>@{child.lineno}"
                )
                parameters = tuple(
                    argument.arg
                    for argument in (
                        *child.args.posonlyargs,
                        *child.args.args,
                    )
                )
                scopes.append(_ExecutableScope(name, child, parameters))
                visit(child, (*owners, f"<lambda>@{child.lineno}"))
            else:
                visit(child, owners)

    visit(tree, ())
    unique = {id(scope.node): scope for scope in scopes}
    return tuple(unique.values())


def _sql_expression_calls(
    nodes: tuple[ast.AST, ...],
) -> tuple[tuple[ast.Call, ast.AST], ...]:
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
    calls = []
    for node in nodes:
        if not isinstance(node, ast.Call) or not node.args:
            continue
        is_executor = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ) or (
            isinstance(node.func, ast.Name) and node.func.id in executor_aliases
        )
        if is_executor:
            calls.append((node, node.args[0]))
    return tuple(calls)


_PRESENCE_STATE_BY_ATTRIBUTE = {
    "UNVERIFIED": "unverified",
    "CONFIRMED": "presence_confirmed",
    "REJECTED": "presence_rejected",
}
_PRESENCE_STATES = frozenset(_PRESENCE_STATE_BY_ATTRIBUTE.values())


def _expression_presence_states(
    expression: ast.AST,
    bindings: dict[str, frozenset[str]],
    constants: dict[str, str],
) -> frozenset[str]:
    states = set()
    for node in ast.walk(expression):
        rendered = _static_string(node, constants)
        if rendered in _PRESENCE_STATES:
            states.add(rendered)
        if isinstance(node, ast.Name):
            states.update(bindings.get(node.id, ()))
        elif isinstance(node, ast.Attribute):
            state = _PRESENCE_STATE_BY_ATTRIBUTE.get(node.attr)
            if state is not None:
                states.add(state)
    return frozenset(states)


def _presence_state_bindings(
    nodes: tuple[ast.AST, ...],
    inherited: dict[str, frozenset[str]] | None = None,
) -> dict[str, frozenset[str]]:
    assignments = tuple(
        node
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
    )
    local_names = {
        name
        for node in assignments
        for target in (
            node.targets if isinstance(node, ast.Assign) else (node.target,)
        )
        for name in _binding_target_names(target)
    }
    bindings = {
        name: states
        for name, states in (inherited or {}).items()
        if name not in local_names
    }
    constants = _string_constants(nodes)
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            states = _expression_presence_states(value, bindings, constants)
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            ):
                for name in _binding_target_names(target):
                    combined = bindings.get(name, frozenset()) | states
                    if combined != bindings.get(name):
                        bindings[name] = combined
                        changed = True
    return bindings


def _presence_states(
    nodes: tuple[ast.AST, ...],
    inherited: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    bindings = _presence_state_bindings(nodes, inherited)
    constants = _string_constants(nodes)
    states = set()
    for node in nodes:
        states.update(_expression_presence_states(node, bindings, constants))
    return frozenset(states)


def _presence_writer_details(
    files: Iterable[Path],
) -> dict[str, frozenset[str]]:
    details: dict[str, frozenset[str]] = {}
    for path in files:
        tree = _tree(path)
        scopes = _executable_scopes(tree)
        module_constants = _string_constants(_scope_nodes(tree))
        module_state_bindings = _presence_state_bindings(_scope_nodes(tree))
        nodes_by_name = {
            scope.name: _scope_nodes(scope.node) for scope in scopes
        }
        constants_by_name = {}
        for scope in scopes:
            constants = dict(module_constants)
            constants.update(_string_constants(nodes_by_name[scope.name]))
            constants_by_name[scope.name] = constants
        functions = {
            scope.name.rsplit(".", 1)[-1]: scope
            for scope in scopes
            if scope.name not in {"<module>"}
            and ".<body>" not in scope.name
            and "<lambda>" not in scope.name
        }
        sink_parameters: dict[str, set[int]] = {
            scope.name: set() for scope in scopes
        }
        writer_names = set()
        for scope in scopes:
            constants = constants_by_name[scope.name]
            for _call, expression in _sql_expression_calls(
                nodes_by_name[scope.name]
            ):
                rendered = _static_string(expression, constants)
                if rendered is not None and _writes_presence_decision(rendered):
                    writer_names.add(scope.name)
                elif isinstance(expression, ast.Name):
                    try:
                        index = scope.positional_parameters.index(expression.id)
                    except ValueError:
                        continue
                    sink_parameters[scope.name].add(index)
        changed = True
        while changed:
            changed = False
            for scope in scopes:
                nodes = nodes_by_name[scope.name]
                constants = constants_by_name[scope.name]
                for node in nodes:
                    if (
                        not isinstance(node, ast.Call)
                        or not isinstance(node.func, ast.Name)
                        or node.func.id not in functions
                    ):
                        continue
                    callee = functions[node.func.id]
                    for index in sink_parameters[callee.name]:
                        if index >= len(node.args):
                            continue
                        expression = node.args[index]
                        rendered = _static_string(expression, constants)
                        if (
                            rendered is not None
                            and _writes_presence_decision(rendered)
                        ):
                            instantiated = {scope.name, callee.name}
                            if not instantiated <= writer_names:
                                writer_names.update(instantiated)
                                changed = True
                        elif isinstance(expression, ast.Name):
                            try:
                                caller_index = scope.positional_parameters.index(
                                    expression.id
                                )
                            except ValueError:
                                continue
                            if caller_index not in sink_parameters[scope.name]:
                                sink_parameters[scope.name].add(caller_index)
                                changed = True
                            if (
                                scope.name in writer_names
                                and callee.name not in writer_names
                            ):
                                writer_names.add(callee.name)
                                changed = True
        relative = _relative(path)
        for scope in scopes:
            if scope.name in writer_names:
                details[f"{relative}:{scope.name}"] = _presence_states(
                    nodes_by_name[scope.name], module_state_bindings
                )
    return details


def presence_decision_writer_calls(
    files: Iterable[Path] = PRODUCTION_FILES,
) -> tuple[str, ...]:
    return tuple(sorted(_presence_writer_details(files)))


EXPECTED_PRESENCE_WRITER_STATES = {
    "repositories/discovery.py:"
    "DiscoveryRepository._get_or_create_candidate": frozenset({"unverified"}),
    "repositories/discovery.py:"
    "DiscoveryRepository.create_initial_candidate": frozenset({"unverified"}),
    "repositories/voice_verification.py:"
    "VoiceVerificationRepository.add_review_and_decision": frozenset(
        {"presence_confirmed", "presence_rejected"}
    ),
}


def presence_decision_state_violations(
    files: Iterable[Path] = PRODUCTION_FILES,
) -> tuple[str, ...]:
    details = _presence_writer_details(files)
    return tuple(
        sorted(
            writer
            for writer, states in details.items()
            if EXPECTED_PRESENCE_WRITER_STATES.get(writer) != states
        )
    )


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


@cache
def _migrated_analysis_table_names() -> frozenset[str]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations(conn)
        return frozenset(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'analysis_%'"
            )
        )
    finally:
        conn.close()


def _writes_any_table(sql: str, tables: frozenset[str]) -> bool:
    quoted = "|".join(
        rf'(?:{re.escape(table)}|"{re.escape(table)}"|'
        rf"`{re.escape(table)}`|\[{re.escape(table)}\])"
        for table in sorted(tables, key=len, reverse=True)
    )
    return re.search(
        rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
        rf"\s+(?:{quoted})",
        sql,
        re.IGNORECASE,
    ) is not None


def _scope_writes_tables(
    scope: _ExecutableScope,
    module_constants: dict[str, str],
    tables: frozenset[str],
) -> bool:
    nodes = _scope_nodes(scope.node)
    constants = dict(module_constants)
    constants.update(_string_constants(nodes))
    return any(
        rendered is not None and _writes_any_table(rendered, tables)
        for _call, expression in _sql_expression_calls(nodes)
        for rendered in (_static_string(expression, constants),)
    )


@cache
def _derived_downstream_writer_methods() -> frozenset[str]:
    tables = frozenset(
        {
            *_migrated_analysis_table_names(),
            "speaker_assignments",
            "transcript_segments",
        }
    )
    methods = set()
    for path in PRODUCTION_FILES:
        # The production method inventory must remain anchored to production
        # even when a mutation test supplies a synthetic entrypoint parser.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_constants = _string_constants(_scope_nodes(tree))
        for scope in _executable_scopes(tree):
            if (
                scope.name != "<module>"
                and ".<body>" not in scope.name
                and "<lambda>" not in scope.name
                and _scope_writes_tables(scope, module_constants, tables)
            ):
                methods.add(scope.name.rsplit(".", 1)[-1])
    return frozenset(methods)


def _module_file(
    module: str,
    package_root: Path,
) -> Path | None:
    prefix = package_root.name
    if module == prefix:
        candidate = package_root / "__init__.py"
        return candidate if candidate.is_file() else None
    if not module.startswith(f"{prefix}."):
        return None
    relative = module[len(prefix) + 1 :].split(".")
    candidate = package_root.joinpath(*relative).with_suffix(".py")
    if candidate.is_file():
        return candidate
    candidate = package_root.joinpath(*relative, "__init__.py")
    return candidate if candidate.is_file() else None


def _absolute_import_module(
    node: ast.ImportFrom,
    path: Path,
    package_root: Path,
) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(package_root).with_suffix("").as_posix()
    prefix = "." * node.level + (node.module or "")
    return _resolve_relative_import(prefix, relative)


def _project_import_targets(
    path: Path,
    tree: ast.Module,
    package_root: Path,
) -> dict[str, tuple[Path, str | None]]:
    targets = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _module_file(alias.name, package_root)
                if target is not None:
                    targets[alias.asname or alias.name.split(".", 1)[0]] = (
                        target,
                        None,
                    )
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_import_module(node, path, package_root)
            for alias in node.names:
                nested = _module_file(f"{module}.{alias.name}", package_root)
                target = nested or _module_file(module, package_root)
                if target is not None:
                    targets[alias.asname or alias.name] = (
                        target,
                        None if nested is not None else alias.name,
                    )
    return targets


def _project_import_closure(
    entrypoints: Iterable[Path],
    package_root: Path,
) -> dict[Path, ast.Module]:
    queued = [path.resolve() for path in entrypoints]
    trees: dict[Path, ast.Module] = {}
    while queued:
        path = queued.pop()
        if path in trees:
            continue
        tree = _tree(path)
        trees[path] = tree
        for target, _symbol in _project_import_targets(
            path, tree, package_root
        ).values():
            resolved = target.resolve()
            if resolved not in trees:
                queued.append(resolved)
    return trees


def presence_expansion_writer_calls(
    entrypoints: Iterable[Path] = PRESENCE_ENTRYPOINT_FILES,
    *,
    package_root: Path = PACKAGE_ROOT,
) -> tuple[str, ...]:
    root = package_root.resolve()
    trees = _project_import_closure(entrypoints, root)
    tables = frozenset(
        {
            *_migrated_analysis_table_names(),
            "speaker_assignments",
            "transcript_segments",
        }
    )
    writer_methods = _derived_downstream_writer_methods()
    scopes_by_path = {
        path: _executable_scopes(tree) for path, tree in trees.items()
    }
    scope_lookup = {
        (path, scope.name.rsplit(".", 1)[-1]): scope
        for path, scopes in scopes_by_path.items()
        for scope in scopes
    }
    imports_by_path = {
        path: _project_import_targets(path, tree, root)
        for path, tree in trees.items()
    }
    module_constants = {
        path: _string_constants(_scope_nodes(tree))
        for path, tree in trees.items()
    }
    entrypoint_paths = {path.resolve() for path in entrypoints}
    queue: list[tuple[Path, _ExecutableScope]] = []
    for path, scopes in scopes_by_path.items():
        queue.extend(
            (path, scope)
            for scope in scopes
            if scope.name == "<module>" or path in entrypoint_paths
        )
    visited = set()
    writers = set()
    while queue:
        path, scope = queue.pop()
        identity = (path, scope.name)
        if identity in visited:
            continue
        visited.add(identity)
        nodes = _scope_nodes(scope.node)
        direct_sql = _scope_writes_tables(
            scope, module_constants[path], tables
        )
        method_call = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in writer_methods
            for node in nodes
        )
        if direct_sql or method_call:
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = _relative(path)
            writers.add(f"{relative}:{scope.name}")
        local = {
            item.name.rsplit(".", 1)[-1]: item
            for item in scopes_by_path[path]
        }
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                target = imports_by_path[path].get(node.func.value.id)
                if target is None or target[1] is not None:
                    continue
                target_path = target[0].resolve()
                target_scope = scope_lookup.get(
                    (target_path, node.func.attr)
                )
                if target_scope is not None:
                    queue.append((target_path, target_scope))
                continue
            if not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name in local:
                queue.append((path, local[name]))
                continue
            target = imports_by_path[path].get(name)
            if target is None:
                continue
            target_path, symbol = target
            if symbol is None:
                continue
            target_scope = scope_lookup.get((target_path.resolve(), symbol))
            if target_scope is not None:
                queue.append((target_path.resolve(), target_scope))
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


def test_all_raw_presence_decision_writers_are_canonical() -> None:
    assert presence_decision_writer_calls() == (
        "repositories/discovery.py:"
        "DiscoveryRepository._get_or_create_candidate",
        "repositories/discovery.py:"
        "DiscoveryRepository.create_initial_candidate",
        "repositories/voice_verification.py:"
        "VoiceVerificationRepository.add_review_and_decision",
    )


def test_discovery_and_review_writers_own_only_their_approved_states() -> None:
    assert presence_decision_state_violations() == ()


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
            "<dynamic-import>",
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
    "rebind",
    (
        "def CHECK():\n    return runtime_flag\n",
        "class CHECK:\n    pass\n",
        "import runtime_flags as CHECK\n",
    ),
)
def test_adapter_guard_does_not_trust_definition_or_import_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    rebind: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        f"{rebind}"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    import_line = 5 if rebind.startswith(("def", "class")) else 4
    expected = (
        (("voice/adapter_mutation.py", 2, "runtime_flags"),)
        if rebind.startswith("import")
        else ()
    )
    assert runtime_import_violations((fake,)) == (
        *expected,
        ("voice/adapter_mutation.py", import_line, "sqlite3"),
    )


def test_adapter_guard_resolves_type_checking_aliases_in_lexical_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "if CHECK:\n"
        "    import sqlite3\n"
        "def runtime():\n"
        "    CHECK = runtime_flag\n"
        "    if CHECK:\n"
        "        import requests\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", 7, "requests"),
    )


@pytest.mark.parametrize(
    ("mutation", "line", "imported"),
    (
        (
            "import importlib\n"
            "def first():\n"
            "    module = 'json'\n"
            "def second():\n"
            "    importlib.import_module(module)\n",
            5,
            "<dynamic-import>",
        ),
        (
            "import importlib\n"
            "def load(package):\n"
            "    importlib.import_module('.protocol', package=package)\n",
            3,
            "<dynamic-import>",
        ),
        (
            "import importlib\n"
            "importlib.import_module(\n"
            "    '.protocol', package='market_voice_forecast_ledger.voice'\n"
            ")\n",
            2,
            "market_voice_forecast_ledger.voice.protocol",
        ),
        (
            "from market_voice_forecast_ledger.voice.protocol.evil "
            "import AdapterRequest\n",
            1,
            "market_voice_forecast_ledger.voice.protocol.evil.AdapterRequest",
        ),
        (
            "from base64 import b64encode\n",
            1,
            "base64.b64encode",
        ),
    ),
)
def test_adapter_guard_rejects_dynamic_package_and_noncanonical_import_forms(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    line: int,
    imported: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", line, imported),
    )


@pytest.mark.parametrize(
    ("mutation", "imported"),
    (
        (
            "def runtime(value: __import__('sqlite3')):\n"
            "    return value\n",
            "sqlite3",
        ),
        ("(lambda: __import__('subprocess'))()\n", "subprocess"),
        (
            "loaded = [__import__('requests') for item in values]\n",
            "requests",
        ),
    ),
)
def test_adapter_guard_inspects_annotation_lambda_and_comprehension_scopes(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    imported: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", 1, imported),
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


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "def initial_writer(conn):\n"
            "    state = 'unverified'\n"
            "    conn.execute('INSERT INTO presence_decisions '"
            "                 '(candidate_id, state) VALUES (?, ?)', (1, state))\n",
            ("workers/raw_mutation.py:initial_writer",),
        ),
        (
            "state = 'presence_confirmed'\n"
            "conn.execute('INSERT OR REPLACE INTO \"presence_decisions\" '"
            "             '(candidate_id, state) VALUES (1, ?)', (state,))\n",
            ("workers/raw_mutation.py:<module>",),
        ),
        (
            "writer = lambda conn: conn.executemany(\n"
            "    'INSERT INTO [presence_decisions](candidate_id, state) '"
            "    'VALUES (?, ?)', [(1, 'presence_rejected')])\n",
            ("workers/raw_mutation.py:<lambda>@1",),
        ),
        (
            "def execute_sql(conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def model_writer(conn):\n"
            "    table = 'presence_' + 'decisions'\n"
            "    statement = f'INSERT INTO `{table}` '"
            "                '(candidate_id, state) VALUES (?, ?)'\n"
            "    execute_sql(conn, statement, (1, 'presence_confirmed'))\n",
            (
                "workers/raw_mutation.py:execute_sql",
                "workers/raw_mutation.py:model_writer",
            ),
        ),
        (
            "def script_writer(conn):\n"
            "    prefix = 'INSERT OR ' + 'REPLACE INTO '\n"
            "    conn.executescript(prefix + 'presence_decisions '"
            "                       '(candidate_id, state) '"
            "                       \"VALUES (1, 'presence_confirmed')\")\n",
            ("workers/raw_mutation.py:script_writer",),
        ),
        (
            "def pointer_writer(conn):\n"
            "    conn.execute('UPDATE \"subject_video_candidates\" '"
            "                 'SET \"current_presence_decision_id\"=1')\n",
            ("workers/raw_mutation.py:pointer_writer",),
        ),
    ),
)
def test_raw_presence_writer_guard_handles_scope_sql_and_helper_bypasses(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    fake = Path("raw-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/raw_mutation.py"
    )

    assert presence_decision_writer_calls((fake,)) == writers


@pytest.mark.parametrize(
    ("relative", "owner", "state"),
    (
        (
            "repositories/discovery.py",
            "DiscoveryRepository.create_initial_candidate",
            "presence_confirmed",
        ),
        (
            "repositories/voice_verification.py",
            "VoiceVerificationRepository.add_review_and_decision",
            "unverified",
        ),
    ),
)
def test_presence_writer_state_guard_rejects_cross_authority_mutations(
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    owner: str,
    state: str,
) -> None:
    class_name, method_name = owner.split(".")
    tree = ast.parse(
        f"class {class_name}:\n"
        f"    def {method_name}(self, conn):\n"
        f"        state = {state!r}\n"
        "        conn.execute('INSERT INTO presence_decisions '"
        "                     '(candidate_id, state) VALUES (?, ?)', (1, state))\n"
    )
    fake = Path("state-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(globals(), "_relative", lambda _path: relative)

    assert presence_decision_state_violations((fake,)) == (
        f"{relative}:{owner}",
    )


def test_presence_writer_state_guard_follows_folded_module_state_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "TERMINAL = {'presence_' + 'confirmed'}\n"
        "class DiscoveryRepository:\n"
        "    def create_initial_candidate(self, conn):\n"
        "        fallback = 'unverified'\n"
        "        state = next(iter(TERMINAL))\n"
        "        conn.execute('INSERT INTO presence_decisions '"
        "                     '(candidate_id, state) VALUES (?, ?)', (1, state))\n"
    )
    fake = Path("state-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "repositories/discovery.py"
    )

    assert presence_decision_state_violations((fake,)) == (
        "repositories/discovery.py:"
        "DiscoveryRepository.create_initial_candidate",
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
        (
            "def create_subject(repository, command):\n"
            "    repository.create_subject(command)\n"
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


@pytest.mark.parametrize(
    "entrypoint_source",
    (
        (
            "from ..helpers.analysis_escape import run_analysis\n"
            "def run(repository):\n"
            "    run_analysis(repository)\n"
        ),
        (
            "from ..helpers import analysis_escape as helper\n"
            "def run(repository):\n"
            "    helper.run_analysis(repository)\n"
        ),
    ),
)
def test_presence_expansion_guard_follows_transitive_project_imports(
    tmp_path: Path,
    entrypoint_source: str,
) -> None:
    package_root = tmp_path / "market_voice_forecast_ledger"
    entrypoint = package_root / "workers" / "presence_mutation.py"
    helper = package_root / "helpers" / "analysis_escape.py"
    entrypoint.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    entrypoint.write_text(entrypoint_source, encoding="utf-8")
    helper.write_text(
        "def run_analysis(repository):\n"
        "    repository.create_subject('synthetic')\n",
        encoding="utf-8",
    )

    assert presence_expansion_writer_calls(
        (entrypoint,), package_root=package_root
    ) == ("helpers/analysis_escape.py:run_analysis",)
