from __future__ import annotations

import ast
import re
import sqlite3
from collections.abc import Callable, Iterable
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
    global_names: frozenset[str] = frozenset()
    nonlocal_names: frozenset[str] = frozenset()
    has_star_import: bool = False

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
        if isinstance(item, ast.Name)
        and isinstance(item.ctx, (ast.Store, ast.Del))
    )


def _scope_declarations(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[frozenset[str], frozenset[str]]:
    global_names = set()
    nonlocal_names = set()

    def visit(candidate: ast.AST) -> None:
        for child in ast.iter_child_nodes(candidate):
            if isinstance(
                child,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            ):
                continue
            if isinstance(child, ast.Global):
                global_names.update(child.names)
            elif isinstance(child, ast.Nonlocal):
                nonlocal_names.update(child.names)
            visit(child)

    visit(node)
    return frozenset(global_names), frozenset(nonlocal_names)


class _ScopeBuilder(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.module = _LexicalScope("module", None)
        self.current = self.module
        self.node_scopes: dict[int, _LexicalScope] = {}
        self.scopes = [self.module]
        self.recorded_targets: set[int] = set()
        self.visit(tree)

    def _binding_scope(
        self,
        name: str,
        scope: _LexicalScope | None = None,
    ) -> _LexicalScope:
        target = scope or self.current
        if name in target.global_names:
            return self.module
        if name in target.nonlocal_names:
            parent = _outer_scope(target)
            while parent is not None:
                if name in parent.events or parent.kind in {
                    "function",
                    "lambda",
                    "type_parameters",
                }:
                    return parent
                parent = _outer_scope(parent)
        return target

    def _bind_name(
        self,
        name: str,
        value: ast.AST | _KnownBinding,
        scope: _LexicalScope | None = None,
    ) -> None:
        self._binding_scope(name, scope).bind(name, value)

    def _bind_target(
        self,
        target: ast.AST,
        value: ast.AST | _KnownBinding,
        scope: _LexicalScope | None = None,
    ) -> None:
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                self.recorded_targets.add(id(node))
                self._bind_name(node.id, value, scope)
        previous = self.current
        if scope is not None:
            self.current = scope
        self.visit(target)
        self.current = previous

    def _type_parameter_scope(self, node: ast.AST) -> _LexicalScope:
        child = _LexicalScope("type_parameters", self.current)
        self.scopes.append(child)
        previous, self.current = self.current, child
        for parameter in getattr(node, "type_params", ()):
            name = getattr(parameter, "name", None)
            if isinstance(name, str):
                child.bind(name, _UNKNOWN_BINDING)
            self.visit(parameter)
        self.current = previous
        return child

    def generic_visit(self, node: ast.AST) -> None:
        self.node_scopes[id(node)] = self.current
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self.node_scopes[id(node)] = self.current
        if (
            isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in self.recorded_targets
        ):
            self._bind_name(node.id, _UNKNOWN_BINDING)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.node_scopes[id(node)] = self.current
        self._bind_name(node.name, _UNKNOWN_BINDING)
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
        outer = self.current
        if node.type_params:
            self.current = self._type_parameter_scope(node)
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
        global_names, nonlocal_names = _scope_declarations(node)
        child = _LexicalScope(
            "function",
            self.current,
            global_names=global_names,
            nonlocal_names=nonlocal_names,
        )
        self.scopes.append(child)
        self.current = child
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
        self.current = outer

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.node_scopes[id(node)] = self.current
        self._bind_name(node.name, _UNKNOWN_BINDING)
        for item in node.decorator_list:
            self.visit(item)
        outer = self.current
        if node.type_params:
            self.current = self._type_parameter_scope(node)
        for item in node.bases:
            self.visit(item)
        for keyword in node.keywords:
            self.visit(keyword.value)
        global_names, nonlocal_names = _scope_declarations(node)
        child = _LexicalScope(
            "class",
            self.current,
            global_names=global_names,
            nonlocal_names=nonlocal_names,
        )
        self.scopes.append(child)
        self.current = child
        for statement in node.body:
            self.visit(statement)
        self.current = outer

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

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.node_scopes[id(node)] = self.current
        outer = self.current
        self._bind_target(node.name, _UNKNOWN_BINDING)
        if node.type_params:
            self.current = self._type_parameter_scope(node)
        self.visit(node.value)
        self.current = outer

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
            self._bind_target(generator.target, _UNKNOWN_BINDING)
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

    def visit_Match(self, node: ast.Match) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.subject)
        for case in node.cases:
            self.visit(case.pattern)
            captures = {
                item.name
                for item in ast.walk(case.pattern)
                if isinstance(item, (ast.MatchAs, ast.MatchStar))
                and item.name is not None
            }
            captures.update(
                item.rest
                for item in ast.walk(case.pattern)
                if isinstance(item, ast.MatchMapping)
                and item.rest is not None
            )
            for name in captures:
                self._bind_name(name, _UNKNOWN_BINDING)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        self.node_scopes[id(node)] = self.current
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            known = {
                "typing": _TYPING_MODULE_BINDING,
                "importlib": _IMPORTLIB_MODULE_BINDING,
                "builtins": _BUILTINS_MODULE_BINDING,
            }.get(alias.name, _UNKNOWN_BINDING)
            self._bind_name(bound, known)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.node_scopes[id(node)] = self.current
        for alias in node.names:
            if alias.name == "*":
                self.current.has_star_import = True
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
            self._bind_name(bound, known)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.node_scopes[id(node)] = self.current
        if node.value is not None:
            self.visit(node.value)
        self._bind_target(node.target, node.value or _UNKNOWN_BINDING)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        binding_scope = self.current
        while binding_scope.kind == "comprehension":
            if binding_scope.parent is None:
                break
            binding_scope = binding_scope.parent
        self._bind_target(node.target, node.value, binding_scope)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.value)
        self._bind_target(node.target, _UNKNOWN_BINDING)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.node_scopes[id(node)] = self.current
        for target in node.targets:
            self._bind_target(target, _UNKNOWN_BINDING)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.node_scopes[id(node)] = self.current
        self.visit(node.iter)
        self._bind_target(node.target, _UNKNOWN_BINDING)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        self.node_scopes[id(node)] = self.current
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars, _UNKNOWN_BINDING)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.node_scopes[id(node)] = self.current
        if node.type is not None:
            self.visit(node.type)
        if node.name is not None:
            self._bind_name(node.name, _UNKNOWN_BINDING)
        for statement in node.body:
            self.visit(statement)

    def visit_Global(self, node: ast.Global) -> None:
        self.node_scopes[id(node)] = self.current

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.node_scopes[id(node)] = self.current


def _outer_scope(scope: _LexicalScope) -> _LexicalScope | None:
    parent = scope.parent
    if scope.kind in {
        "function",
        "lambda",
        "comprehension",
        "type_parameters",
    }:
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
    if name in scope.global_names and scope.kind != "module":
        module = scope
        while module.parent is not None:
            module = module.parent
        return _resolve_binding(name, module, seen=seen | {key})
    if name in scope.nonlocal_names:
        parent = _outer_scope(scope)
        return (
            _UNKNOWN_BINDING
            if parent is None
            else _resolve_binding(name, parent, seen=seen | {key})
        )
    if scope.has_star_import:
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
    qualifier = (
        r"(?:(?:[A-Za-z_]\w*|\"[^\"]+\"|`[^`]+`|\[[^\]]+\])"
        r"\s*\.\s*)?"
    )
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
        rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE(?:\s+INTO)?)"
        rf"\s+{qualifier}{decision_table}",
        sql,
        re.IGNORECASE,
    )
    pointer = re.search(
        rf"\bUPDATE\s+{qualifier}{candidate_table}",
        sql,
        re.IGNORECASE,
    ) and re.search(pointer_column, sql, re.IGNORECASE)
    return inserts is not None or bool(pointer)


@dataclass(frozen=True, slots=True)
class _ExecutableScope:
    name: str
    node: ast.AST
    positional_parameters: tuple[str, ...]
    positional_count: int
    defaults: tuple[tuple[str, ast.AST], ...]
    method_kind: str
    class_owner: str | None

    def default_for(self, parameter: str) -> ast.AST | None:
        return dict(self.defaults).get(parameter)


@dataclass(frozen=True, slots=True)
class _ResolvedCallable:
    name: str
    bound_offset: int


def _executable_scopes(tree: ast.Module) -> tuple[_ExecutableScope, ...]:
    scopes = [_ExecutableScope("<module>", tree, (), 0, (), "module", None)]

    def function_scope(
        child: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        name: str,
        class_owner: str | None,
    ) -> _ExecutableScope:
        positional = (*child.args.posonlyargs, *child.args.args)
        keyword_only = child.args.kwonlyargs
        parameters = tuple(
            argument.arg for argument in (*positional, *keyword_only)
        )
        positional_default_args = (
            positional[-len(child.args.defaults) :]
            if child.args.defaults
            else ()
        )
        defaults = {
            argument.arg: value
            for argument, value in zip(
                positional_default_args,
                child.args.defaults,
                strict=True,
            )
        }
        defaults.update(
            {
                argument.arg: value
                for argument, value in zip(
                    keyword_only,
                    child.args.kw_defaults,
                    strict=True,
                )
                if value is not None
            }
        )
        decorators = {
            decorator.id
            for decorator in getattr(child, "decorator_list", ())
            if isinstance(decorator, ast.Name)
        }
        method_kind = "function"
        if class_owner is not None:
            if "staticmethod" in decorators:
                method_kind = "static"
            elif "classmethod" in decorators:
                method_kind = "class"
            else:
                method_kind = "instance"
        return _ExecutableScope(
            name,
            child,
            parameters,
            len(positional),
            tuple(defaults.items()),
            method_kind,
            class_owner,
        )

    def visit(
        node: ast.AST,
        owners: tuple[str, ...],
        class_owner: str | None = None,
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                name = ".".join((*owners, child.name))
                scopes.append(
                    _ExecutableScope(
                        f"{name}.<body>", child, (), 0, (), "class", name
                    )
                )
                visit(child, (*owners, child.name), name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = ".".join((*owners, child.name))
                scopes.append(function_scope(child, name, class_owner))
                visit(child, (*owners, child.name))
            elif isinstance(child, ast.Lambda):
                prefix = ".".join(owners)
                name = (
                    f"{prefix}.<lambda>@{child.lineno}"
                    if prefix
                    else f"<lambda>@{child.lineno}"
                )
                scopes.append(function_scope(child, name, None))
                visit(child, (*owners, f"<lambda>@{child.lineno}"))
            else:
                visit(child, owners, class_owner)

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
    assignments = _scope_assignment_values(nodes)

    def assigned_values(expression: ast.AST) -> tuple[ast.AST, ...]:
        return _assigned_expression_values(assignments, expression)

    calls = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        is_executor = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "executescript"}
        ) or (
            isinstance(node.func, ast.Name) and node.func.id in executor_aliases
        )
        if is_executor:
            calls.extend(
                (node, option[0])
                for option in _expanded_positional_options(
                    node.args, assigned_values
                )
                if option
            )
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


def _scope_assignment_values(
    nodes: tuple[ast.AST, ...],
) -> dict[str, tuple[ast.AST, ...]]:
    values: dict[str, list[ast.AST]] = {}
    root = nodes[0] if nodes else None
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        positional = (*root.args.posonlyargs, *root.args.args)
        default_parameters = (
            positional[-len(root.args.defaults) :]
            if root.args.defaults
            else ()
        )
        for parameter, default in zip(
            default_parameters, root.args.defaults, strict=True
        ):
            values.setdefault(parameter.arg, []).append(default)
        for parameter, default in zip(
            root.args.kwonlyargs, root.args.kw_defaults, strict=True
        ):
            if default is not None:
                values.setdefault(parameter.arg, []).append(default)
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            continue
        value = node.value
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            path = _expression_alias_key(target)
            if path is not None:
                values.setdefault(path, []).append(value)
                continue
            for name in _binding_target_names(target):
                values.setdefault(name, []).append(value)
    return {name: tuple(items) for name, items in values.items()}


def _scope_binding_values(
    scope: _ExecutableScope,
) -> dict[str, tuple[ast.AST, ...]]:
    return _scope_assignment_values(_scope_nodes(scope.node))


def _expression_path(expression: ast.AST) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        owner = _expression_path(expression.value)
        return None if owner is None else f"{owner}.{expression.attr}"
    if isinstance(expression, ast.Subscript):
        owner = _expression_path(expression.value)
        key = expression.slice
        if (
            owner is None
            or not isinstance(key, ast.Constant)
            or type(key.value) not in {str, int}
        ):
            return None
        return f"{owner}[{key.value!r}]"
    return None


_DYNAMIC_SUBSCRIPT = "[<dynamic>]"


def _expression_alias_key(expression: ast.AST) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        owner = _expression_alias_key(expression.value)
        return None if owner is None else f"{owner}.{expression.attr}"
    if isinstance(expression, ast.Subscript):
        owner = _expression_alias_key(expression.value)
        if owner is None:
            return None
        key = expression.slice
        if isinstance(key, ast.Constant) and type(key.value) in {str, int}:
            return f"{owner}[{key.value!r}]"
        return f"{owner}{_DYNAMIC_SUBSCRIPT}"
    return None


def _alias_keys_overlap(left: str, right: str) -> bool:
    def pattern(key: str) -> re.Pattern[str]:
        rendered = re.escape(key).replace(
            re.escape(_DYNAMIC_SUBSCRIPT), r"\[[^\]]+\]"
        )
        return re.compile(rf"^{rendered}$")

    return pattern(left).fullmatch(right) is not None or pattern(
        right
    ).fullmatch(left) is not None


def _assigned_expression_values(
    assignments: dict[str, tuple[ast.AST, ...]],
    expression: ast.AST,
) -> tuple[ast.AST, ...]:
    key = _expression_alias_key(expression)
    if key is None:
        return ()
    return tuple(
        value
        for assigned_key, values in assignments.items()
        if _alias_keys_overlap(key, assigned_key)
        for value in values
    )


def _visible_scope_names(
    scope_name: str,
    known_scopes: frozenset[str],
) -> tuple[str, ...]:
    visible = [scope_name]
    parts = scope_name.split(".")
    for end in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:end])
        for candidate in (prefix, f"{prefix}.<body>"):
            if candidate in known_scopes and candidate not in visible:
                visible.append(candidate)
    if "<module>" not in visible:
        visible.append("<module>")
    return tuple(visible)


def _sequence_expression_options(
    expression: ast.AST,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[tuple[ast.AST, ...], ...]:
    if isinstance(expression, (ast.List, ast.Tuple)):
        options: tuple[tuple[ast.AST, ...], ...] = ((),)
        for item in expression.elts:
            item_options = (
                _sequence_expression_options(
                    item.value, assigned_values, seen
                )
                if isinstance(item, ast.Starred)
                else ((item,),)
            )
            if not item_options:
                return ()
            options = tuple(
                (*prefix, *suffix)
                for prefix in options
                for suffix in item_options
            )
        return options
    key = _expression_alias_key(expression)
    if key is None or key in seen:
        return ()
    return tuple(
        option
        for value in assigned_values(expression)
        for option in _sequence_expression_options(
            value, assigned_values, seen | {key}
        )
    )


def _mapping_expression_values(
    expression: ast.AST,
    parameter: str,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[ast.AST, ...]:
    if isinstance(expression, ast.Dict):
        found = []
        for key_node, value in zip(
            expression.keys, expression.values, strict=True
        ):
            if key_node is None:
                found.extend(
                    _mapping_expression_values(
                        value, parameter, assigned_values, seen
                    )
                )
            elif (
                isinstance(key_node, ast.Constant)
                and type(key_node.value) is str
                and key_node.value == parameter
            ):
                found.append(value)
        return tuple(found)
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "dict"
    ):
        return tuple(
            keyword.value
            for keyword in expression.keywords
            if keyword.arg == parameter
        )
    key = _expression_alias_key(expression)
    if key is None or key in seen:
        return ()
    return tuple(
        value
        for assigned in assigned_values(expression)
        for value in _mapping_expression_values(
            assigned, parameter, assigned_values, seen | {key}
        )
    )


def _expanded_positional_options(
    arguments: Iterable[ast.AST],
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
) -> tuple[tuple[ast.AST, ...], ...]:
    options: tuple[tuple[ast.AST, ...], ...] = ((),)
    for argument in arguments:
        argument_options = (
            _sequence_expression_options(argument.value, assigned_values)
            if isinstance(argument, ast.Starred)
            else ((argument,),)
        )
        if not argument_options:
            return ()
        options = tuple(
            (*prefix, *suffix)
            for prefix in options
            for suffix in argument_options
        )
    return options


def _subscript_container_values(
    expression: ast.AST,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
) -> tuple[ast.AST, ...]:
    if not isinstance(expression, ast.Subscript):
        return ()
    key_node = expression.slice
    key = (
        key_node.value
        if isinstance(key_node, ast.Constant)
        and type(key_node.value) in {str, int}
        else None
    )

    def containers(
        candidate: ast.AST,
        seen: frozenset[str] = frozenset(),
    ) -> tuple[ast.AST, ...]:
        if isinstance(candidate, (ast.Dict, ast.List, ast.Tuple)):
            return (candidate,)
        path_key = _expression_alias_key(candidate)
        if path_key is None or path_key in seen:
            return ()
        return tuple(
            container
            for value in assigned_values(candidate)
            for container in containers(value, seen | {path_key})
        )

    values = []
    for container in containers(expression.value):
        if isinstance(container, (ast.List, ast.Tuple)):
            if type(key) is int and -len(container.elts) <= key < len(
                container.elts
            ):
                values.append(container.elts[key])
            elif key is None:
                values.extend(container.elts)
        elif isinstance(container, ast.Dict):
            for item_key, value in zip(
                container.keys, container.values, strict=True
            ):
                if key is None:
                    values.append(value)
                elif (
                    isinstance(item_key, ast.Constant)
                    and item_key.value == key
                    and type(item_key.value) is type(key)
                ):
                    values.append(value)
    return tuple(dict.fromkeys(values))


def _call_argument_values(
    call: ast.Call,
    callee: _ExecutableScope,
    parameter_index: int,
    bound_offset: int,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
) -> tuple[ast.AST, ...]:
    if parameter_index < bound_offset:
        return ()
    positional_index = parameter_index - bound_offset
    if parameter_index < callee.positional_count:
        options = _expanded_positional_options(call.args, assigned_values)
        positional = tuple(
            option[positional_index]
            for option in options
            if 0 <= positional_index < len(option)
        )
        if positional:
            return tuple(dict.fromkeys(positional))
    parameter_name = callee.positional_parameters[parameter_index]
    keywords = tuple(
        keyword.value
        for keyword in call.keywords
        if keyword.arg == parameter_name
    )
    unpacked = tuple(
        value
        for keyword in call.keywords
        if keyword.arg is None
        for value in _mapping_expression_values(
            keyword.value, parameter_name, assigned_values
        )
    )
    supplied = tuple(dict.fromkeys((*keywords, *unpacked)))
    if supplied:
        return supplied
    default = callee.default_for(parameter_name)
    return () if default is None else (default,)


def _presence_writer_details(
    files: Iterable[Path],
    *,
    writes_sql: Callable[[str], bool] = _writes_presence_decision,
) -> dict[str, frozenset[str]]:
    details: dict[str, frozenset[str]] = {}
    for path in files:
        tree = _tree(path)
        scopes = _executable_scopes(tree)
        scopes_by_name = {scope.name: scope for scope in scopes}
        known_scopes = frozenset(scopes_by_name)
        module_nodes = _scope_nodes(tree)
        module_constants = _string_constants(module_nodes)
        module_state_bindings = _presence_state_bindings(module_nodes)
        nodes_by_name = {
            scope.name: _scope_nodes(scope.node) for scope in scopes
        }
        constants_by_name = {}
        assignments_by_name = {}
        for scope in scopes:
            constants = dict(module_constants)
            constants.update(_string_constants(nodes_by_name[scope.name]))
            constants_by_name[scope.name] = constants
            assignments_by_name[scope.name] = _scope_binding_values(scope)
        sql_call_ids_by_name = {
            scope.name: {
                id(call)
                for call, _expression in _sql_expression_calls(
                    nodes_by_name[scope.name]
                )
            }
            for scope in scopes
        }

        callables_by_short: dict[str, set[str]] = {}
        lambda_by_node = {}
        for scope in scopes:
            if scope.name == "<module>" or ".<body>" in scope.name:
                continue
            callables_by_short.setdefault(
                scope.name.rsplit(".", 1)[-1], set()
            ).add(scope.name)
            if isinstance(scope.node, ast.Lambda):
                lambda_by_node[id(scope.node)] = scope.name

        def is_class_reference(
            expression: ast.AST,
            class_owner: str | None,
            caller_name: str,
            seen: frozenset[tuple[str, str]] = frozenset(),
        ) -> bool:
            if class_owner is None:
                return False
            path = _expression_alias_key(expression)
            class_names = {class_owner, class_owner.rsplit(".", 1)[-1]}
            if path in class_names:
                return True
            if path is None:
                return False
            key = (caller_name, path)
            if key in seen:
                return False
            return any(
                is_class_reference(
                    value,
                    class_owner,
                    caller_name,
                    seen | {key},
                )
                for visible in _visible_scope_names(
                    caller_name, known_scopes
                )
                for value in _assigned_expression_values(
                    assignments_by_name[visible], expression
                )
            )

        def callable_targets(
            expression: ast.AST,
            caller_name: str,
            seen: frozenset[tuple[str, str]] = frozenset(),
        ) -> frozenset[_ResolvedCallable]:
            if isinstance(expression, ast.Lambda):
                target = lambda_by_node.get(id(expression))
                return (
                    frozenset()
                    if target is None
                    else frozenset({_ResolvedCallable(target, 0)})
                )
            path = _expression_alias_key(expression)
            if path is not None:
                key = (caller_name, path)
                if key in seen:
                    return frozenset()
                resolved = set()
                for visible in _visible_scope_names(caller_name, known_scopes):
                    for value in _assigned_expression_values(
                        assignments_by_name[visible], expression
                    ):
                        resolved.update(
                            callable_targets(
                                value, caller_name, seen | {key}
                            )
                        )
                if isinstance(expression, ast.Name):
                    resolved.update(
                        _ResolvedCallable(target, 0)
                        for target in callables_by_short.get(
                            expression.id, ()
                        )
                    )
                if resolved:
                    return frozenset(resolved)
            if isinstance(expression, ast.Attribute):
                if (
                    isinstance(expression.value, ast.Name)
                    and expression.value.id not in {"self", "cls"}
                    and expression.attr
                    in {"execute", "executemany", "executescript"}
                ):
                    return frozenset()
                resolved = set()
                for target in callables_by_short.get(expression.attr, ()):
                    callee = scopes_by_name[target]
                    offset = 0
                    if callee.method_kind == "class":
                        offset = 1
                    elif callee.method_kind == "instance":
                        offset = (
                            0
                            if is_class_reference(
                                expression.value,
                                callee.class_owner,
                                caller_name,
                            )
                            else 1
                        )
                    resolved.add(_ResolvedCallable(target, offset))
                return frozenset(resolved)
            return frozenset()

        def parameter_indexes(
            expression: ast.AST,
            scope: _ExecutableScope,
            seen: frozenset[str] = frozenset(),
        ) -> frozenset[int]:
            if not isinstance(expression, ast.Name):
                return frozenset()
            if expression.id in scope.positional_parameters:
                return frozenset(
                    {scope.positional_parameters.index(expression.id)}
                )
            if expression.id in seen:
                return frozenset()
            indexes = set()
            for value in _assigned_expression_values(
                assignments_by_name[scope.name], expression
            ):
                indexes.update(
                    parameter_indexes(
                        value, scope, seen | {expression.id}
                    )
                )
            return frozenset(indexes)

        def argument_states(
            expression: ast.AST,
            scope_name: str,
            seen: frozenset[str] = frozenset(),
        ) -> frozenset[str]:
            rendered = _static_string(
                expression, constants_by_name[scope_name]
            )
            if rendered in _PRESENCE_STATES:
                return frozenset({rendered})
            if isinstance(expression, ast.Attribute):
                state = _PRESENCE_STATE_BY_ATTRIBUTE.get(expression.attr)
                return frozenset() if state is None else frozenset({state})
            path = _expression_alias_key(expression)
            if path is not None and path not in seen:
                states = set()
                for visible in _visible_scope_names(
                    scope_name, known_scopes
                ):
                    for value in _assigned_expression_values(
                        assignments_by_name[visible], expression
                    ):
                        states.update(
                            argument_states(
                                value, scope_name, seen | {path}
                            )
                        )
                if states:
                    return frozenset(states)
            children: tuple[ast.AST, ...] = ()
            if isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
                children = tuple(expression.elts)
            elif isinstance(expression, ast.Dict):
                children = tuple(
                    item
                    for item in (*expression.keys, *expression.values)
                    if item is not None
                )
            return frozenset(
                state
                for child in children
                for state in argument_states(child, scope_name, seen)
            )

        def visible_assigned_values(
            expression: ast.AST,
            scope_name: str,
        ) -> tuple[ast.AST, ...]:
            return tuple(
                value
                for visible in _visible_scope_names(scope_name, known_scopes)
                for value in _assigned_expression_values(
                    assignments_by_name[visible], expression
                )
            )

        sink_parameters: dict[str, set[int]] = {
            scope.name: set() for scope in scopes
        }
        writer_names = set()
        for scope in scopes:
            for _call, expression in _sql_expression_calls(
                nodes_by_name[scope.name]
            ):
                rendered = _static_string(
                    expression, constants_by_name[scope.name]
                )
                if rendered is not None and writes_sql(rendered):
                    writer_names.add(scope.name)
                sink_parameters[scope.name].update(
                    parameter_indexes(expression, scope)
                )

        edges: list[tuple[str, _ResolvedCallable, ast.Call]] = []
        for scope in scopes:
            for node in nodes_by_name[scope.name]:
                if not isinstance(node, ast.Call):
                    continue
                resolved_targets = callable_targets(node.func, scope.name)
                for target in resolved_targets:
                    edges.append((scope.name, target, node))
                arguments = (
                    *node.args,
                    *(keyword.value for keyword in node.keywords),
                )
                rendered_arguments = tuple(
                    _static_string(
                        argument, constants_by_name[scope.name]
                    )
                    for argument in arguments
                )
                if any(
                    rendered is not None
                    and writes_sql(rendered)
                    for rendered in rendered_arguments
                ):
                    writer_names.add(scope.name)
                elif (
                    not resolved_targets
                    and id(node) not in sql_call_ids_by_name[scope.name]
                    and any(
                        argument_states(argument, scope.name)
                        for argument in arguments
                    )
                ):
                    writer_names.add(scope.name)

        changed = True
        while changed:
            changed = False
            for caller_name, resolved, call in edges:
                callee_name = resolved.name
                caller = scopes_by_name[caller_name]
                callee = scopes_by_name[callee_name]
                for index in tuple(sink_parameters[callee_name]):
                    expressions = _call_argument_values(
                        call,
                        callee,
                        index,
                        resolved.bound_offset,
                        lambda expression: visible_assigned_values(
                            expression, caller_name
                        ),
                    )
                    for expression in expressions:
                        propagated = parameter_indexes(expression, caller)
                        if not propagated <= sink_parameters[caller_name]:
                            sink_parameters[caller_name].update(propagated)
                            changed = True

        for scope in scopes:
            for index in sink_parameters[scope.name]:
                parameter = scope.positional_parameters[index]
                default = scope.default_for(parameter)
                rendered = (
                    None
                    if default is None
                    else _static_string(
                        default, constants_by_name[scope.name]
                    )
                )
                if rendered is not None and writes_sql(rendered):
                    writer_names.add(scope.name)

        active_edges: set[tuple[str, str]] = set()
        for caller_name, resolved, call in edges:
            callee_name = resolved.name
            callee = scopes_by_name[callee_name]
            for index in sink_parameters[callee_name]:
                expressions = _call_argument_values(
                    call,
                    callee,
                    index,
                    resolved.bound_offset,
                    lambda expression: visible_assigned_values(
                        expression, caller_name
                    ),
                )
                if any(
                    (rendered := _static_string(
                        expression, constants_by_name[caller_name]
                    ))
                    is not None
                    and writes_sql(rendered)
                    for expression in expressions
                ):
                    active_edges.add((caller_name, callee_name))
                    writer_names.update({caller_name, callee_name})

        changed = True
        while changed:
            changed = False
            for caller_name, resolved, call in edges:
                callee_name = resolved.name
                if caller_name not in writer_names:
                    continue
                caller = scopes_by_name[caller_name]
                callee = scopes_by_name[callee_name]
                for index in sink_parameters[callee_name]:
                    expressions = _call_argument_values(
                        call,
                        callee,
                        index,
                        resolved.bound_offset,
                        lambda expression: visible_assigned_values(
                            expression, caller_name
                        ),
                    )
                    if not any(
                        parameter_indexes(expression, caller)
                        & sink_parameters[caller_name]
                        for expression in expressions
                    ):
                        continue
                    edge = (caller_name, callee_name)
                    if edge not in active_edges:
                        active_edges.add(edge)
                        changed = True
                    if callee_name not in writer_names:
                        writer_names.add(callee_name)
                        changed = True

        local_states = {
            scope.name: _presence_states(
                nodes_by_name[scope.name], module_state_bindings
            )
            for scope in scopes
        }
        adjacency = {name: set() for name in writer_names}
        for caller_name, callee_name in active_edges:
            adjacency.setdefault(caller_name, set()).add(callee_name)
            adjacency.setdefault(callee_name, set()).add(caller_name)
        try:
            relative = _relative(path)
        except ValueError:
            relative = path.resolve().as_posix()
        for writer_name in writer_names:
            component = {writer_name}
            queued = [writer_name]
            while queued:
                current = queued.pop()
                for adjacent in adjacency.get(current, ()):
                    if adjacent not in component:
                        component.add(adjacent)
                        queued.append(adjacent)
            states = frozenset(
                state
                for name in component
                for state in local_states[name]
            )
            details[f"{relative}:{writer_name}"] = states
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
        scopes = _executable_scopes(tree)
        known_scopes = frozenset(scope.name for scope in scopes)
        assignments = {
            scope.name: _scope_binding_values(scope) for scope in scopes
        }
        class_scopes = tuple(
            scope
            for scope in scopes
            if isinstance(scope.node, ast.ClassDef)
            and scope.class_owner is not None
        )
        classes_by_name = {
            scope.class_owner or "": scope for scope in class_scopes
        }

        def class_derives_from(
            derived: str,
            target: str,
            seen: frozenset[str] = frozenset(),
        ) -> bool:
            if derived == target or derived.rsplit(".", 1)[-1] == target:
                return True
            if derived in seen:
                return False
            class_scope = classes_by_name.get(derived)
            if class_scope is None or not isinstance(
                class_scope.node, ast.ClassDef
            ):
                return False
            for base in class_scope.node.bases:
                base_name = _expression_alias_key(base)
                if base_name is None:
                    continue
                candidates = tuple(
                    name
                    for name in classes_by_name
                    if name == base_name
                    or name.rsplit(".", 1)[-1] == base_name
                )
                if any(
                    class_derives_from(
                        candidate, target, seen | {derived}
                    )
                    for candidate in candidates
                ):
                    return True
            return False

        imported_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "add_review_and_decision"
        }

        for scope in scopes:
            nodes = _scope_nodes(scope.node)

            def visible_values(expression: ast.AST) -> tuple[ast.AST, ...]:
                return tuple(
                    value
                    for visible in _visible_scope_names(
                        scope.name, known_scopes
                    )
                    for value in _assigned_expression_values(
                        assignments[visible], expression
                    )
                )

            def refers_to_class(
                expression: ast.AST,
                class_name: str,
                seen: frozenset[str] = frozenset(),
            ) -> bool:
                path_key = _expression_alias_key(expression)
                class_names = {class_name, class_name.rsplit(".", 1)[-1]}
                if path_key in class_names:
                    return True
                receiver_names = set()
                if (
                    scope.class_owner is not None
                    and class_derives_from(scope.class_owner, class_name)
                    and scope.positional_parameters
                    and scope.method_kind in {"instance", "class"}
                ):
                    receiver_names.add(scope.positional_parameters[0])
                if path_key in receiver_names:
                    return True
                if isinstance(expression, ast.Call):
                    return refers_to_class(
                        expression.func, class_name, seen
                    )
                if path_key is None or path_key in seen:
                    return False
                return any(
                    refers_to_class(value, class_name, seen | {path_key})
                    for value in visible_values(expression)
                )

            def class_member_values(
                expression: ast.AST,
            ) -> tuple[ast.AST, ...]:
                if not isinstance(expression, ast.Attribute):
                    return ()
                return tuple(
                    value
                    for class_scope in class_scopes
                    if refers_to_class(
                        expression.value, class_scope.class_owner or ""
                    )
                    for member_key in (
                        expression.attr,
                        f"{class_scope.class_owner}.{expression.attr}",
                        f"{(class_scope.class_owner or '').rsplit('.', 1)[-1]}"
                        f".{expression.attr}",
                    )
                    for visible in dict.fromkeys(
                        (
                            *_visible_scope_names(
                                scope.name, known_scopes
                            ),
                            class_scope.name,
                        )
                    )
                    for assigned_key, assigned in assignments[visible].items()
                    if _alias_keys_overlap(member_key, assigned_key)
                    for value in assigned
                )

            def is_writer(
                expression: ast.AST,
                seen: frozenset[str] = frozenset(),
            ) -> bool:
                if (
                    isinstance(expression, ast.Attribute)
                    and expression.attr == "add_review_and_decision"
                ):
                    return True
                if (
                    isinstance(expression, ast.Name)
                    and expression.id in imported_aliases
                ):
                    return True
                if (
                    isinstance(expression, ast.Call)
                    and isinstance(expression.func, ast.Name)
                    and expression.func.id in {"classmethod", "staticmethod"}
                    and expression.args
                ):
                    return is_writer(expression.args[0], seen)
                path = _expression_alias_key(expression)
                if path in seen:
                    return False
                resolved = [
                    *class_member_values(expression),
                    *_subscript_container_values(expression, visible_values),
                ]
                if path is not None:
                    resolved.extend(visible_values(expression))
                return any(
                    is_writer(
                        value,
                        seen if path is None else seen | {path},
                    )
                    for value in resolved
                )

            calls_writer = any(
                isinstance(node, ast.Call)
                and is_writer(node.func)
                for node in nodes
            )
            if calls_writer:
                callers.append(f"{_relative(path)}:{scope.name}")
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
    qualifier = (
        r"(?:(?:[A-Za-z_]\w*|\"[^\"]+\"|`[^`]+`|\[[^\]]+\])"
        r"\s*\.\s*)?"
    )
    quoted = "|".join(
        rf'(?:{re.escape(table)}|"{re.escape(table)}"|'
        rf"`{re.escape(table)}`|\[{re.escape(table)}\])"
        for table in sorted(tables, key=len, reverse=True)
    )
    return re.search(
        rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE(?:\s+INTO)?|"
        rf"UPDATE|DELETE\s+FROM)\s+{qualifier}(?:{quoted})",
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


def _is_downstream_writer_method(
    name: str,
    derived_methods: frozenset[str],
) -> bool:
    if name in derived_methods:
        return True
    verb = re.match(
        r"^(?:add|append|create|delete|insert|promote|save|store|update|write)_",
        name,
    )
    domain = re.search(
        r"(?:^|_)(?:analysis|speaker|subject|transcript)(?:_|$)",
        name,
    )
    return verb is not None and domain is not None


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
    sql_writer_scopes = {
        path: {
            writer.rsplit(":", 1)[-1]
            for writer in _presence_writer_details(
                (path,),
                writes_sql=lambda sql: _writes_any_table(sql, tables),
            )
        }
        for path in trees
    }
    assignments = {
        (path, scope.name): _scope_assignment_values(_scope_nodes(scope.node))
        for path, scopes in scopes_by_path.items()
        for scope in scopes
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
        direct_sql = scope.name in sql_writer_scopes[path]
        method_reference = any(
            isinstance(node, ast.Attribute)
            and _is_downstream_writer_method(node.attr, writer_methods)
            for node in nodes
        )
        if direct_sql or method_reference:
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = _relative(path)
            writers.add(f"{relative}:{scope.name}")
        local = {
            item.name.rsplit(".", 1)[-1]: item
            for item in scopes_by_path[path]
        }

        def call_targets(
            expression: ast.AST,
            seen: frozenset[str] = frozenset(),
        ) -> tuple[tuple[Path, _ExecutableScope], ...]:
            path_key = _expression_alias_key(expression)
            if path_key is not None and path_key not in seen:
                assigned_targets = []
                for visible_scope in (scope.name, "<module>"):
                    for value in _assigned_expression_values(
                        assignments[(path, visible_scope)], expression
                    ):
                        assigned_targets.extend(
                            call_targets(value, seen | {path_key})
                        )
                if assigned_targets:
                    return tuple(dict.fromkeys(assigned_targets))
            if isinstance(expression, ast.Name):
                if expression.id in seen:
                    return ()
                targets = []
                if expression.id in local:
                    targets.append((path, local[expression.id]))
                imported = imports_by_path[path].get(expression.id)
                if imported is not None and imported[1] is not None:
                    target_path, symbol = imported
                    target_scope = scope_lookup.get(
                        (target_path.resolve(), symbol)
                    )
                    if target_scope is not None:
                        targets.append((target_path.resolve(), target_scope))
                return tuple(dict.fromkeys(targets))
            if isinstance(expression, ast.Attribute):
                targets = []
                for target_path in module_paths(expression.value):
                    target_scope = scope_lookup.get(
                        (target_path, expression.attr)
                    )
                    if target_scope is not None:
                        targets.append((target_path, target_scope))
                return tuple(dict.fromkeys(targets))
            return ()

        def module_paths(
            expression: ast.AST,
            seen: frozenset[str] = frozenset(),
        ) -> tuple[Path, ...]:
            path_key = _expression_alias_key(expression)
            if path_key is not None and path_key not in seen:
                targets = []
                for visible_scope in (scope.name, "<module>"):
                    for value in _assigned_expression_values(
                        assignments[(path, visible_scope)], expression
                    ):
                        targets.extend(
                            module_paths(value, seen | {path_key})
                        )
                if targets:
                    return tuple(dict.fromkeys(targets))
            if isinstance(expression, ast.Name):
                imported = imports_by_path[path].get(expression.id)
                if imported is not None and imported[1] is None:
                    return (imported[0].resolve(),)
            return ()

        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            queue.extend(call_targets(node.func))
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
    "pattern",
    (
        "CHECK",
        "{'value': CHECK}",
        "{'value': _, **CHECK}",
        "[CHECK]",
        "[*CHECK]",
        "Point(CHECK)",
        "_ as CHECK",
        "(1 as CHECK) | (2 as CHECK)",
    ),
)
def test_adapter_guard_treats_every_match_capture_as_a_scope_binding(
    monkeypatch: pytest.MonkeyPatch,
    pattern: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "match value:\n"
        f"    case {pattern}:\n"
        "        pass\n"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("adapter-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_mutation.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/adapter_mutation.py", 6, "sqlite3"),
    )


def test_adapter_guard_keeps_unshadowed_type_checking_after_other_match_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "match value:\n"
        "    case {'value': other}:\n"
        "        pass\n"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("adapter-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_control.py"
    )

    assert runtime_import_violations((fake,)) == ()


@pytest.mark.parametrize(
    "comprehension",
    (
        "[(CHECK := runtime_flag) for item in values]",
        "[item for item in values if (CHECK := runtime_flag)]",
        "[item for item in values for nested in (CHECK := more_values)]",
    ),
)
def test_adapter_guard_binds_comprehension_walrus_in_containing_scope(
    monkeypatch: pytest.MonkeyPatch,
    comprehension: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        f"loaded = {comprehension}\n"
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


def test_adapter_guard_keeps_unshadowed_type_checking_after_other_walrus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "loaded = [(other := runtime_flag) for item in values]\n"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("adapter-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/adapter_control.py"
    )

    assert runtime_import_violations((fake,)) == ()


@pytest.mark.parametrize(
    "mutation",
    (
        "type CHECK = int\n"
        "if CHECK:\n"
        "    import sqlite3\n",
        "def runtime[CHECK]():\n"
        "    if CHECK:\n"
        "        import sqlite3\n",
        "class Runtime[CHECK]:\n"
        "    if CHECK:\n"
        "        import sqlite3\n",
    ),
)
def test_adapter_guard_models_python_314_type_bindings(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n" f"{mutation}"
    )
    fake = Path("type-binding-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/type_binding.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/type_binding.py", 4, "sqlite3"),
    )


def test_adapter_guard_does_not_leak_class_alias_into_generic_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "import typing\n"
        "class Runtime:\n"
        "    CHECK = typing.TYPE_CHECKING\n"
        "    def run[T](self):\n"
        "        if CHECK:\n"
        "            import sqlite3\n"
    )
    fake = Path("generic-method-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/generic_method.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/generic_method.py", 6, "sqlite3"),
    )


def test_adapter_guard_keeps_module_alias_in_generic_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "class Runtime:\n"
        "    def run[T](self):\n"
        "        if CHECK:\n"
        "            import sqlite3\n"
    )
    fake = Path("generic-method-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/generic_control.py"
    )

    assert runtime_import_violations((fake,)) == ()


@pytest.mark.parametrize(
    "neighbor",
    (
        "type Neighbor = int\n",
        "def runtime[Neighbor]():\n    return Neighbor\n",
        "class Runtime[Neighbor]:\n    marker = Neighbor\n",
        "loaded = [item for CHECK in values]\n",
        "loaded = {CHECK: item for CHECK, item in values}\n",
    ),
)
def test_adapter_guard_keeps_unshadowed_alias_after_neighboring_bindings(
    monkeypatch: pytest.MonkeyPatch,
    neighbor: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        f"{neighbor}"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("type-binding-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/type_control.py"
    )

    assert runtime_import_violations((fake,)) == ()


@pytest.mark.parametrize(
    "binding",
    (
        "CHECK: bool\n",
        "CHECK += runtime_flag\n",
        "for CHECK in values:\n    pass\n",
        "with manager() as CHECK:\n    pass\n",
        "try:\n    work()\nexcept RuntimeError as CHECK:\n    pass\n",
        "del CHECK\n",
    ),
)
def test_adapter_guard_inventories_neighboring_binding_statements(
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        f"{binding}"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("binding-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/binding_mutation.py"
    )

    import_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and node.names[0].name == "sqlite3"
    )
    assert runtime_import_violations((fake,)) == (
        ("voice/binding_mutation.py", import_node.lineno, "sqlite3"),
    )


def test_adapter_guard_fails_closed_after_star_import_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n"
        "from runtime_flags import *\n"
        "if CHECK:\n"
        "    import sqlite3\n"
    )
    fake = Path("star-import-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/star_import.py"
    )

    assert runtime_import_violations((fake,)) == (
        ("voice/star_import.py", 2, "runtime_flags.*"),
        ("voice/star_import.py", 4, "sqlite3"),
    )


@pytest.mark.parametrize(
    "declaration",
    (
        "def runtime():\n"
        "    global CHECK\n"
        "    if CHECK:\n"
        "        import sqlite3\n",
        "def outer():\n"
        "    from typing import TYPE_CHECKING as CHECK\n"
        "    def runtime():\n"
        "        nonlocal CHECK\n"
        "        if CHECK:\n"
        "            import sqlite3\n",
    ),
)
def test_adapter_guard_resolves_unmodified_global_and_nonlocal_aliases(
    monkeypatch: pytest.MonkeyPatch,
    declaration: str,
) -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING as CHECK\n" f"{declaration}"
    )
    fake = Path("declaration-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "voice/declaration_control.py"
    )

    assert runtime_import_violations((fake,)) == ()


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
    "sql",
    (
        "REPLACE presence_decisions(candidate_id) VALUES (1)",
        "REPLACE INTO main.presence_decisions(candidate_id) VALUES (1)",
        (
            'INSERT OR ABORT INTO "main"."presence_decisions"'
            "(candidate_id) VALUES (1)"
        ),
        (
            "UPDATE [main].[subject_video_candidates] "
            'SET "current_presence_decision_id"=1'
        ),
        (
            "UPDATE `main`.`subject_video_candidates` "
            "SET [current_presence_decision_id]=1"
        ),
    ),
)
def test_raw_presence_writer_guard_handles_replace_and_qualified_sql(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    tree = ast.parse(
        "def raw_writer(conn):\n"
        f"    conn.executescript({sql!r})\n"
    )
    fake = Path("qualified-sql-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/qualified_sql.py"
    )

    assert presence_decision_writer_calls((fake,)) == (
        "workers/qualified_sql.py:raw_writer",
    )


@pytest.mark.parametrize(
    "sql",
    (
        "REPLACE INTO presence_decision_archive(candidate_id) VALUES (1)",
        "INSERT INTO main.audit_presence_decisions(candidate_id) VALUES (1)",
        "UPDATE subject_video_candidates SET updated_at='synthetic'",
    ),
)
def test_raw_presence_writer_guard_ignores_similar_nonwriter_sql(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    tree = ast.parse(
        "def near_miss(conn):\n"
        f"    conn.execute({sql!r})\n"
    )
    fake = Path("sql-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/sql_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "class SqlHelpers:\n"
            "    def execute_sql(self, conn, statement, values):\n"
            "        conn.execute(statement, values)\n"
            "    def write(self, conn):\n"
            "        sql = 'REPLACE INTO presence_decisions '"
            "              '(candidate_id, state) VALUES (?, ?)'\n"
            "        state = 'presence_confirmed'\n"
            "        self.execute_sql(conn, sql, (1, state))\n",
            (
                "workers/helper_sql.py:SqlHelpers.execute_sql",
                "workers/helper_sql.py:SqlHelpers.write",
            ),
        ),
        (
            "execute_sql = lambda conn, statement, values: "
            "conn.executemany(statement, values)\n"
            "def write(conn):\n"
            "    sql = 'INSERT INTO presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    state = 'presence_confirmed'\n"
            "    execute_sql(conn, sql, [(1, state)])\n",
            (
                "workers/helper_sql.py:<lambda>@1",
                "workers/helper_sql.py:write",
            ),
        ),
        (
            "def execute_sql(conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "run_sql = execute_sql\n"
            "def write(conn):\n"
            "    sql = 'REPLACE presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    state = 'presence_confirmed'\n"
            "    run_sql(conn, sql, (1, state))\n",
            (
                "workers/helper_sql.py:execute_sql",
                "workers/helper_sql.py:write",
            ),
        ),
    ),
)
def test_raw_presence_writer_guard_traces_class_lambda_and_alias_helpers(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    fake = Path("helper-sql-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/helper_sql.py"
    )

    assert presence_decision_writer_calls((fake,)) == writers
    assert _presence_writer_details((fake,)) == {
        writer: frozenset({"presence_confirmed"}) for writer in writers
    }


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "def execute_sql(conn, statement="
            "'INSERT INTO presence_decisions(candidate_id, state) '"
            "'VALUES (?, ?)', state='presence_confirmed'):\n"
            "    conn.execute(statement, (1, state))\n"
            "def write(conn):\n"
            "    execute_sql(conn)\n",
            (
                "workers/resolved_helper.py:execute_sql",
                "workers/resolved_helper.py:write",
            ),
        ),
        (
            "class SqlHelpers:\n"
            "    def execute_sql(self, conn, statement, state):\n"
            "        conn.execute(statement, (1, state))\n"
            "    def write(self, conn):\n"
            "        sql = 'INSERT INTO presence_decisions '"
            "              '(candidate_id, state) VALUES (?, ?)'\n"
            "        state = 'presence_rejected'\n"
            "        SqlHelpers.execute_sql(self, conn, sql, state)\n",
            (
                "workers/resolved_helper.py:SqlHelpers.execute_sql",
                "workers/resolved_helper.py:SqlHelpers.write",
            ),
        ),
        (
            "def execute_sql(conn, statement, state):\n"
            "    conn.execute(statement, (1, state))\n"
            "class Holder:\n"
            "    pass\n"
            "holder = Holder()\n"
            "holder.run = execute_sql\n"
            "def write(conn):\n"
            "    sql = 'INSERT INTO presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    holder.run(conn, sql, 'presence_confirmed')\n",
            (
                "workers/resolved_helper.py:execute_sql",
                "workers/resolved_helper.py:write",
            ),
        ),
        (
            "def execute_sql(conn, statement, state):\n"
            "    conn.execute(statement, (1, state))\n"
            "helpers = {}\n"
            "helpers['run'] = execute_sql\n"
            "def write(conn):\n"
            "    sql = 'INSERT INTO presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    helpers['run'](conn, sql, 'presence_confirmed')\n",
            (
                "workers/resolved_helper.py:execute_sql",
                "workers/resolved_helper.py:write",
            ),
        ),
        (
            "def execute_sql(conn, statement, state):\n"
            "    conn.execute(statement, (1, state))\n"
            "helpers = {}\n"
            "key = 'run'\n"
            "helpers[key] = execute_sql\n"
            "def write(conn):\n"
            "    sql = 'INSERT INTO presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    helpers[key](conn, sql, 'presence_confirmed')\n",
            (
                "workers/resolved_helper.py:execute_sql",
                "workers/resolved_helper.py:write",
            ),
        ),
        (
            "def write(conn):\n"
            "    sql = 'INSERT INTO presence_decisions '"
            "          '(candidate_id, state) VALUES (?, ?)'\n"
            "    state = 'presence_confirmed'\n"
            "    unresolved(conn, sql, state)\n",
            ("workers/resolved_helper.py:write",),
        ),
        (
            "class SqlHelpers:\n"
            "    def execute_sql(self, conn, statement, state):\n"
            "        conn.execute(statement, (1, state))\n"
            "    def write("
            "self, conn, sql='INSERT INTO presence_decisions '"
            "'(candidate_id, state) VALUES (?, ?)', "
            "state='presence_rejected'):\n"
            "        alias = SqlHelpers\n"
            "        alias.execute_sql(self, conn, sql, state)\n",
            (
                "workers/resolved_helper.py:SqlHelpers.execute_sql",
                "workers/resolved_helper.py:SqlHelpers.write",
            ),
        ),
        (
            "class StaticHelpers:\n"
            "    @classmethod\n"
            "    def execute_sql(cls, conn, statement, state):\n"
            "        conn.execute(statement, (1, state))\n"
            "    @staticmethod\n"
            "    def write(conn):\n"
            "        sql = 'INSERT INTO presence_decisions '"
            "              '(candidate_id, state) VALUES (?, ?)'\n"
            "        StaticHelpers.execute_sql("
            "conn, sql, 'presence_confirmed')\n",
            (
                "workers/resolved_helper.py:StaticHelpers.execute_sql",
                "workers/resolved_helper.py:StaticHelpers.write",
            ),
        ),
        (
            "def write(conn, statement):\n"
            "    state = 'presence_confirmed'\n"
            "    unresolved(conn, statement, state)\n",
            ("workers/resolved_helper.py:write",),
        ),
    ),
)
def test_raw_presence_writer_guard_resolves_complete_helper_calls(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    fake = Path("resolved-helper-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/resolved_helper.py"
    )

    assert presence_decision_writer_calls((fake,)) == writers
    assert _presence_writer_details((fake,)) == {
        writer: frozenset(
            {"presence_rejected"}
            if "SqlHelpers" in writer
            else {"presence_confirmed"}
        )
        for writer in writers
    }


@pytest.mark.parametrize(
    "mutation",
    (
        (
            "def execute_sql(conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def write(conn):\n"
            "    statement = 'INSERT INTO presence_decisions '"
            "                '(candidate_id, state) VALUES (?, ?)'\n"
            "    values = (1, 'presence_confirmed')\n"
            "    args = (conn, statement, values)\n"
            "    execute_sql(*args)\n"
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def write(conn):\n"
            "    statement = 'INSERT INTO presence_decisions '"
            "                '(candidate_id, state) VALUES (?, ?)'\n"
            "    values = (1, 'presence_confirmed')\n"
            "    kwargs = {\n"
            "        'conn': conn, 'statement': statement, 'values': values\n"
            "    }\n"
            "    execute_sql(**kwargs)\n"
        ),
    ),
)
def test_raw_presence_writer_guard_expands_executed_argument_containers(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE presence_decisions "
            "(candidate_id INTEGER, state TEXT)"
        )
        namespace: dict[str, object] = {}
        exec(compile(tree, "<argument-container-mutation>", "exec"), namespace)
        namespace["write"](conn)  # type: ignore[operator]
        assert conn.execute(
            "SELECT candidate_id, state FROM presence_decisions"
        ).fetchone() == (1, "presence_confirmed")
    finally:
        conn.close()

    fake = Path("argument-container-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/argument_container.py"
    )

    writers = (
        "workers/argument_container.py:execute_sql",
        "workers/argument_container.py:write",
    )
    assert presence_decision_writer_calls((fake,)) == writers
    assert _presence_writer_details((fake,)) == {
        writer: frozenset({"presence_confirmed"}) for writer in writers
    }


@pytest.mark.parametrize(
    "control",
    (
        (
            "def execute_sql(conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    args = (conn, 'SELECT ?', ('control',))\n"
            "    return execute_sql(*args)\n"
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    kwargs = {\n"
            "        'conn': conn, 'statement': 'SELECT ?',\n"
            "        'values': ('control',)\n"
            "    }\n"
            "    return execute_sql(**kwargs)\n"
        ),
    ),
)
def test_raw_presence_writer_guard_accepts_executed_read_only_containers(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        namespace: dict[str, object] = {}
        exec(compile(tree, "<argument-container-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == "control"  # type: ignore[operator]
    finally:
        conn.close()

    fake = Path("argument-container-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/container_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    "mutation",
    (
        (
            "def write(conn):\n"
            "    args = (\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n"
            "    conn.execute(*args)\n"
        ),
        (
            "def write(conn):\n"
            "    execute_sql = conn.execute\n"
            "    args = (\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n"
            "    execute_sql(*args)\n"
        ),
    ),
)
def test_raw_presence_writer_guard_expands_executed_sql_sink_arguments(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE presence_decisions "
            "(candidate_id INTEGER, state TEXT)"
        )
        namespace: dict[str, object] = {}
        exec(compile(tree, "<sql-sink-argument-mutation>", "exec"), namespace)
        namespace["write"](conn)  # type: ignore[operator]
        assert conn.execute(
            "SELECT candidate_id, state FROM presence_decisions"
        ).fetchone() == (1, "presence_confirmed")
    finally:
        conn.close()

    fake = Path("sql-sink-argument-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/sql_sink_args.py"
    )

    assert presence_decision_writer_calls((fake,)) == (
        "workers/sql_sink_args.py:write",
    )


@pytest.mark.parametrize(
    "control",
    (
        (
            "def inspect(conn):\n"
            "    args = ('SELECT ?', ('control',))\n"
            "    return conn.execute(*args).fetchone()[0]\n"
        ),
        (
            "def inspect(conn):\n"
            "    execute_sql = conn.execute\n"
            "    args = ('SELECT ?', ('control',))\n"
            "    return execute_sql(*args).fetchone()[0]\n"
        ),
    ),
)
def test_raw_presence_writer_guard_accepts_executed_read_only_sql_sink_star(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        namespace: dict[str, object] = {}
        exec(compile(tree, "<sql-sink-argument-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == "control"  # type: ignore[operator]
    finally:
        conn.close()

    fake = Path("sql-sink-argument-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/sql_sink_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    "mutation",
    (
        "def execute_sql(conn, statement='SELECT 1'):\n"
        "    conn.execute(statement)\n"
        "def inspect(conn):\n"
        "    execute_sql(conn)\n",
        "class SqlHelpers:\n"
        "    @staticmethod\n"
        "    def execute_sql(conn, statement):\n"
        "        conn.execute(statement)\n"
        "    @classmethod\n"
        "    def inspect(cls, conn):\n"
        "        cls.execute_sql(conn, 'SELECT 1')\n",
        "def inspect(conn):\n"
        "    unresolved(conn, 'SELECT 1', 'not-a-presence-state')\n",
    ),
)
def test_raw_presence_writer_guard_ignores_nonwriter_helpers(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("resolved-helper-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/helper_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


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
        "review_write = repository.add_review_and_decision\n"
        "def save(command):\n"
        "    return review_write(command)\n",
        "aliases = {}\n"
        "aliases['review'] = repository.add_review_and_decision\n"
        "def save(command):\n"
        "    return aliases['review'](command)\n",
        "aliases = {}\n"
        "key = 'review'\n"
        "aliases[key] = repository.add_review_and_decision\n"
        "def save(command):\n"
        "    return aliases[key](command)\n",
    ),
)
def test_review_writer_caller_guard_follows_module_container_aliases(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    tree = ast.parse(mutation)
    fake = Path("review-alias-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_alias.py"
    )

    assert review_writer_callers((fake,)) == (
        "workers/review_alias.py:save",
    )


@pytest.mark.parametrize(
    ("mutation", "caller"),
    (
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "    def save(self, command):\n"
            "        return self.writer(command)\n",
            "Writer.save",
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer.writer(command)\n",
            "save",
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            "save",
        ),
        (
            "class Base:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "class Writer(Base):\n"
            "    def save(self, command):\n"
            "        return self.writer(command)\n",
            "Writer.save",
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "Writer.writer = staticmethod(\n"
            "    repository.add_review_and_decision\n"
            ")\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            "save",
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "Writer.writer = staticmethod(\n"
            "    repository.add_review_and_decision\n"
            ")\n"
            "instance = Writer()\n"
            "def save(command):\n"
            "    return instance.writer(command)\n",
            "save",
        ),
        (
            "def save(command, writer=repository.add_review_and_decision):\n"
            "    return writer(command)\n",
            "save",
        ),
        (
            "def save(command, *, writer=repository.add_review_and_decision):\n"
            "    return writer(command)\n",
            "save",
        ),
        (
            "def save(\n"
            "    command, writers=(repository.add_review_and_decision,)\n"
            "):\n"
            "    return writers[0](command)\n",
            "save",
        ),
        (
            "def save(\n"
            "    command, writers={'review': repository.add_review_and_decision}\n"
            "):\n"
            "    return writers['review'](command)\n",
            "save",
        ),
    ),
)
def test_review_writer_guard_resolves_executed_class_and_default_aliases(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    caller: str,
) -> None:
    class Repository:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def add_review_and_decision(self, command: str) -> str:
            self.conn.execute(
                "INSERT INTO review_effects(command) VALUES (?)", (command,)
            )
            return command

    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE review_effects (command TEXT NOT NULL)")
        namespace: dict[str, object] = {"repository": Repository(conn)}
        exec(compile(tree, "<review-alias-mutation>", "exec"), namespace)
        if caller == "Writer.save":
            result = namespace["Writer"]().save("confirm")  # type: ignore[operator]
        else:
            result = namespace["save"]("confirm")  # type: ignore[operator]
        assert result == "confirm"
        assert conn.execute(
            "SELECT command FROM review_effects"
        ).fetchone() == ("confirm",)
    finally:
        conn.close()

    fake = Path("review-binding-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_binding.py"
    )

    assert review_writer_callers((fake,)) == (
        f"workers/review_binding.py:{caller}",
    )


@pytest.mark.parametrize(
    "control",
    (
        (
            "class Reader:\n"
            "    reader = staticmethod(repository.describe)\n"
            "    def inspect(self, command):\n"
            "        return self.reader(command)\n"
            "def run(command):\n"
            "    return Reader().inspect(command)\n"
        ),
        (
            "class Reader:\n"
            "    reader = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Reader.reader(command)\n"
        ),
        (
            "class Reader:\n"
            "    reader = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Reader().reader(command)\n"
        ),
        (
            "class Base:\n"
            "    reader = staticmethod(repository.describe)\n"
            "class Reader(Base):\n"
            "    def inspect(self, command):\n"
            "        return self.reader(command)\n"
            "def run(command):\n"
            "    return Reader().inspect(command)\n"
        ),
        (
            "class Reader:\n"
            "    pass\n"
            "Reader.reader = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Reader().reader(command)\n"
        ),
        (
            "class Reader:\n"
            "    pass\n"
            "Reader.reader = staticmethod(repository.describe)\n"
            "instance = Reader()\n"
            "def run(command):\n"
            "    return instance.reader(command)\n"
        ),
        (
            "def run(command, reader=repository.describe):\n"
            "    return reader(command)\n"
        ),
        (
            "def run(command, *, reader=repository.describe):\n"
            "    return reader(command)\n"
        ),
        (
            "def run(command, readers=(repository.describe,)):\n"
            "    return readers[0](command)\n"
        ),
        (
            "def run(command, readers={'review': repository.describe}):\n"
            "    return readers['review'](command)\n"
        ),
    ),
)
def test_review_writer_guard_accepts_executed_nonwriter_alias_bindings(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    class Repository:
        @staticmethod
        def describe(command: str) -> str:
            return f"control:{command}"

    tree = ast.parse(control)
    namespace: dict[str, object] = {"repository": Repository()}
    exec(compile(tree, "<review-alias-control>", "exec"), namespace)
    assert namespace["run"]("inspect") == "control:inspect"  # type: ignore[operator]

    fake = Path("review-binding-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_control.py"
    )

    assert review_writer_callers((fake,)) == ()


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
    "method",
    (
        "create_subject",
        "create_analysis_export",
        "add_transcript_segment",
        "update_speaker_assignment",
    ),
)
def test_presence_expansion_guard_flags_writer_method_alias_before_call(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    tree = ast.parse(
        "def escape(repository):\n"
        f"    writer = repository.{method}\n"
    )
    fake = Path("presence-alias-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/presence_alias.py"
    )

    assert presence_expansion_writer_calls((fake,)) == (
        "workers/presence_alias.py:escape",
    )


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "def execute_sql(conn, statement):\n"
            "    conn.execute(statement)\n"
            "def run(conn):\n"
            "    sql = 'INSERT INTO analysis_runs(id) VALUES (1)'\n"
            "    execute_sql(conn, sql)\n",
            (
                "workers/downstream_helper.py:execute_sql",
                "workers/downstream_helper.py:run",
            ),
        ),
        (
            "def run(conn):\n"
            "    sql = 'INSERT INTO analysis_runs(id) VALUES (1)'\n"
            "    unresolved(conn, sql)\n",
            ("workers/downstream_helper.py:run",),
        ),
    ),
)
def test_presence_expansion_guard_traces_generic_or_unresolved_sql_helpers(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    fake = Path("downstream-helper-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/downstream_helper.py"
    )

    assert presence_expansion_writer_calls((fake,)) == writers


def test_presence_expansion_guard_ignores_nonwriter_generic_sql_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "def execute_sql(conn, statement='SELECT 1'):\n"
        "    conn.execute(statement)\n"
        "def inspect(conn):\n"
        "    execute_sql(conn)\n"
    )
    fake = Path("downstream-helper-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/downstream_control.py"
    )

    assert presence_expansion_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    "method",
    (
        "get_analysis_run",
        "create_candidate",
        "analysis_summary",
    ),
)
def test_presence_expansion_guard_ignores_readers_and_unrelated_aliases(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    tree = ast.parse(
        "def inspect(repository):\n"
        f"    reader = repository.{method}\n"
    )
    fake = Path("presence-alias-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/presence_control.py"
    )

    assert presence_expansion_writer_calls((fake,)) == ()


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
        (
            "from ..helpers import analysis_escape as helper\n"
            "def run(repository):\n"
            "    escape = helper.run_analysis\n"
            "    escape(repository)\n"
        ),
        (
            "from ..helpers import analysis_escape\n"
            "helper = analysis_escape\n"
            "def run(repository):\n"
            "    helper.run_analysis(repository)\n"
        ),
        (
            "from ..helpers import analysis_escape\n"
            "helpers = {}\n"
            "helpers['analysis'] = analysis_escape\n"
            "def run(repository):\n"
            "    helpers['analysis'].run_analysis(repository)\n"
        ),
        (
            "from ..helpers import analysis_escape\n"
            "helpers = {}\n"
            "key = 'analysis'\n"
            "helpers[key] = analysis_escape\n"
            "def run(repository):\n"
            "    helpers[key].run_analysis(repository)\n"
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
