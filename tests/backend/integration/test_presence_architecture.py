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
    vararg_index: int | None
    kwarg_index: int | None
    defaults: tuple[tuple[str, ast.AST], ...]
    method_kind: str
    class_owner: str | None

    def default_for(self, parameter: str) -> ast.AST | None:
        return dict(self.defaults).get(parameter)


@dataclass(frozen=True, slots=True)
class _ResolvedCallable:
    name: str
    bound_offset: int


@dataclass(frozen=True, slots=True)
class _AstBindingEvent:
    key: str
    value: ast.AST
    position: tuple[int, int, int]
    branches: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _ParameterSelector:
    index: int
    path: tuple[str | int, ...] = ()


def _scope_binding_events(
    nodes: tuple[ast.AST, ...],
) -> tuple[_AstBindingEvent, ...]:
    if not nodes:
        return ()
    root = nodes[0]
    parents = {
        id(child): parent
        for parent in ast.walk(root)
        for child in ast.iter_child_nodes(parent)
    }

    def branch_path(node: ast.AST) -> tuple[tuple[int, str], ...]:
        branches = []
        child = node
        parent = parents.get(id(child))
        while parent is not None:
            if isinstance(parent, ast.If):
                arm = "body" if child in parent.body else "else"
                branches.append((id(parent), arm))
            child = parent
            parent = parents.get(id(child))
        return tuple(branches)

    events = []
    counter = 0

    def add(key: str, value: ast.AST, owner: ast.AST) -> None:
        nonlocal counter
        counter += 1
        events.append(
            _AstBindingEvent(
                key,
                value,
                (
                    getattr(owner, "end_lineno", owner.lineno),
                    getattr(owner, "end_col_offset", owner.col_offset),
                    counter,
                ),
                branch_path(owner),
            )
        )

    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            if node.value is None:
                continue
            targets = (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            for target in targets:
                path = _expression_alias_key(target)
                if path is not None:
                    add(path, node.value, node)
                else:
                    for name in _binding_target_names(target):
                        add(name, node.value, node)

    def declarations(statements: Iterable[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                add(statement.name, statement, statement)
                continue
            for field_name in ("body", "orelse", "finalbody"):
                body = getattr(statement, field_name, None)
                if isinstance(body, list):
                    declarations(body)
            if isinstance(statement, ast.Try):
                for handler in statement.handlers:
                    declarations(handler.body)

    body = getattr(root, "body", None)
    if isinstance(body, list):
        declarations(body)
    return tuple(sorted(events, key=lambda event: event.position))


def _assigned_event_values(
    events: tuple[_AstBindingEvent, ...],
    expression: ast.AST,
    cutoff: tuple[int, int, int],
) -> tuple[ast.AST, ...]:
    key = _expression_alias_key(expression)
    if key is None:
        return ()
    candidates = sorted(
        (
            event
            for event in events
            if event.position < cutoff
            and _alias_keys_overlap(key, event.key)
        ),
        key=lambda event: event.position,
        reverse=True,
    )
    if not candidates:
        return ()
    selected = [candidates[0]]
    for candidate in candidates[1:]:
        candidate_branches = dict(candidate.branches)
        if any(
            branch_id in candidate_branches
            and candidate_branches[branch_id] != arm
            for current in selected
            for branch_id, arm in current.branches
        ):
            selected.append(candidate)
    return tuple(event.value for event in selected)


def _executable_scopes(tree: ast.Module) -> tuple[_ExecutableScope, ...]:
    scopes = [
        _ExecutableScope(
            "<module>", tree, (), 0, None, None, (), "module", None
        )
    ]

    def function_scope(
        child: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        name: str,
        class_owner: str | None,
    ) -> _ExecutableScope:
        positional = (*child.args.posonlyargs, *child.args.args)
        keyword_only = child.args.kwonlyargs
        fixed_parameters = tuple(
            argument.arg for argument in (*positional, *keyword_only)
        )
        parameters = (
            *fixed_parameters,
            *((child.args.vararg.arg,) if child.args.vararg else ()),
            *((child.args.kwarg.arg,) if child.args.kwarg else ()),
        )
        vararg_index = (
            len(fixed_parameters) if child.args.vararg is not None else None
        )
        kwarg_index = (
            len(fixed_parameters) + int(child.args.vararg is not None)
            if child.args.kwarg is not None
            else None
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
            vararg_index,
            kwarg_index,
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
                        f"{name}.<body>",
                        child,
                        (),
                        0,
                        None,
                        None,
                        (),
                        "class",
                        name,
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
    assigned_values_at: Callable[
        [ast.AST, tuple[int, int, int]], tuple[ast.AST, ...]
    ]
    | None = None,
) -> tuple[tuple[ast.Call, ast.AST], ...]:
    events = _scope_binding_events(nodes)

    def is_executor(
        expression: ast.AST,
        assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
        seen: frozenset[str] = frozenset(),
    ) -> bool:
        if (
            isinstance(expression, ast.Attribute)
            and expression.attr in {"execute", "executemany", "executescript"}
        ):
            return True
        path = _expression_alias_key(expression)
        if path in seen:
            return False
        resolved = _subscript_container_values(expression, assigned_values)
        if path is not None:
            resolved = (*resolved, *assigned_values(expression))
        return any(
            is_executor(
                value,
                assigned_values,
                seen if path is None else seen | {path},
            )
            for value in resolved
        )

    calls = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        cutoff = (node.lineno, node.col_offset, 2**31 - 1)

        def assigned_values(expression: ast.AST) -> tuple[ast.AST, ...]:
            if assigned_values_at is not None:
                return assigned_values_at(expression, cutoff)
            return _assigned_event_values(events, expression, cutoff)

        if not is_executor(node.func, assigned_values):
            continue
        options = _expanded_positional_options(node.args, assigned_values)
        if (
            node.args
            and isinstance(node.args[0], ast.Starred)
            and not _sequence_expression_options(
                node.args[0].value, assigned_values
            )
        ):
            calls.append(
                (
                    node,
                    ast.Subscript(
                        value=node.args[0].value,
                        slice=ast.Constant(value=0),
                        ctx=ast.Load(),
                    ),
                )
            )
        else:
            calls.extend((node, option[0]) for option in options if option)
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
            if isinstance(item, ast.Starred):
                item_options = _sequence_expression_options(
                    item.value, assigned_values, seen
                ) or ((item.value,),)
            else:
                item_options = ((item,),)
            if not item_options:
                return ()
            options = tuple(
                (*prefix, *suffix)
                for prefix in options
                for suffix in item_options
            )
        return options
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id in {"list", "tuple"}
        and not assigned_values(expression.func)
    ):
        if not expression.args:
            return ((),)
        if len(expression.args) != 1:
            return ()
        return _sequence_expression_options(
            expression.args[0], assigned_values, seen
        )
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


def _mapping_expression_options(
    expression: ast.AST,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[dict[str, ast.AST], ...]:
    if isinstance(expression, ast.Dict):
        options: tuple[dict[str, ast.AST], ...] = ({},)
        for key_node, value in zip(
            expression.keys, expression.values, strict=True
        ):
            if key_node is None:
                additions = _mapping_expression_options(
                    value, assigned_values, seen
                )
                if not additions:
                    continue
                options = tuple(
                    {**prefix, **addition}
                    for prefix in options
                    for addition in additions
                )
            elif (
                isinstance(key_node, ast.Constant)
                and type(key_node.value) is str
            ):
                options = tuple(
                    {**prefix, key_node.value: value} for prefix in options
                )
        return options
    if isinstance(expression, ast.BinOp) and isinstance(
        expression.op, ast.BitOr
    ):
        left = _mapping_expression_options(
            expression.left, assigned_values, seen
        )
        right = _mapping_expression_options(
            expression.right, assigned_values, seen
        )
        return tuple(
            {**left_option, **right_option}
            for left_option in left
            for right_option in right
        )
    if (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Name)
        and expression.func.id == "dict"
        and not assigned_values(expression.func)
    ):
        options: tuple[dict[str, ast.AST], ...] = ({},)
        for argument in expression.args:
            additions = _mapping_expression_options(
                argument, assigned_values, seen
            )
            if not additions:
                pairs = _sequence_expression_options(
                    argument, assigned_values, seen
                )
                pair_options: list[dict[str, ast.AST]] = []
                for pair_sequence in pairs:
                    mapping: dict[str, ast.AST] = {}
                    valid = True
                    for pair in pair_sequence:
                        pair_values = _sequence_expression_options(
                            pair, assigned_values, seen
                        )
                        if len(pair_values) != 1 or len(pair_values[0]) != 2:
                            valid = False
                            break
                        key_node, value_node = pair_values[0]
                        if not (
                            isinstance(key_node, ast.Constant)
                            and type(key_node.value) is str
                        ):
                            valid = False
                            break
                        mapping[key_node.value] = value_node
                    if valid:
                        pair_options.append(mapping)
                additions = tuple(pair_options)
            if not additions:
                return ()
            options = tuple(
                {**prefix, **addition}
                for prefix in options
                for addition in additions
            )
        for keyword in expression.keywords:
            if keyword.arg is None:
                additions = _mapping_expression_options(
                    keyword.value, assigned_values, seen
                )
                if not additions:
                    continue
                options = tuple(
                    {**prefix, **addition}
                    for prefix in options
                    for addition in additions
                )
            else:
                options = tuple(
                    {**prefix, keyword.arg: keyword.value}
                    for prefix in options
                )
        return options
    key = _expression_alias_key(expression)
    if key is None or key in seen:
        return ()
    return tuple(
        option
        for assigned in assigned_values(expression)
        for option in _mapping_expression_options(
            assigned, assigned_values, seen | {key}
        )
    )


def _mapping_expression_values(
    expression: ast.AST,
    parameter: str,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[ast.AST, ...]:
    return tuple(
        option[parameter]
        for option in _mapping_expression_options(
            expression, assigned_values, seen
        )
        if parameter in option
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
        if isinstance(argument, ast.Starred) and not argument_options:
            argument_options = ((argument.value,),)
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

    values = []
    if type(key) is int:
        for option in _sequence_expression_options(
            expression.value, assigned_values
        ):
            if -len(option) <= key < len(option):
                values.append(option[key])
    elif type(key) is str:
        for option in _mapping_expression_options(
            expression.value, assigned_values
        ):
            if key in option:
                values.append(option[key])
    else:
        for option in _sequence_expression_options(
            expression.value, assigned_values
        ):
            values.extend(option)
        for option in _mapping_expression_options(
            expression.value, assigned_values
        ):
            values.extend(option.values())
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
    options = _expanded_positional_options(call.args, assigned_values)
    if parameter_index == callee.vararg_index:
        supplied_count = callee.positional_count - bound_offset
        return tuple(
            dict.fromkeys(
                expression
                for option in options
                for expression in option[supplied_count:]
            )
        )
    if parameter_index == callee.kwarg_index:
        return tuple(
            value
            for keyword in call.keywords
            if keyword.arg is None
            for option in _mapping_expression_options(
                keyword.value, assigned_values
            )
            for value in option.values()
        )
    positional_index = parameter_index - bound_offset
    if parameter_index < callee.positional_count:
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


def _select_ast_values(
    expressions: Iterable[ast.AST],
    selector: tuple[str | int, ...],
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
) -> tuple[ast.AST, ...]:
    selected = tuple(expressions)
    for key in selector:
        next_values = []
        for expression in selected:
            subscript = ast.Subscript(
                value=expression,
                slice=ast.Constant(value=key),
                ctx=ast.Load(),
            )
            resolved = _subscript_container_values(
                subscript, assigned_values
            )
            next_values.extend(resolved or (subscript,))
        selected = tuple(dict.fromkeys(next_values))
    return selected


def _call_selector_values(
    call: ast.Call,
    callee: _ExecutableScope,
    selector: _ParameterSelector,
    bound_offset: int,
    assigned_values: Callable[[ast.AST], tuple[ast.AST, ...]],
) -> tuple[ast.AST, ...]:
    parameter_index = selector.index
    if parameter_index < bound_offset:
        return ()
    if parameter_index == callee.vararg_index:
        options = _expanded_positional_options(call.args, assigned_values)
        supplied_count = callee.positional_count - bound_offset
        extras = tuple(
            option[supplied_count:]
            for option in options
        )
        if not selector.path:
            return tuple(
                dict.fromkeys(
                    expression
                    for option in extras
                    for expression in option
                )
            )
        first, *remaining = selector.path
        if type(first) is not int:
            return ()
        selected = tuple(
            option[first]
            for option in extras
            if -len(option) <= first < len(option)
        )
        return _select_ast_values(
            selected, tuple(remaining), assigned_values
        )
    if parameter_index == callee.kwarg_index:
        if not selector.path:
            return tuple(
                value
                for keyword in call.keywords
                if keyword.arg is None
                for option in _mapping_expression_options(
                    keyword.value, assigned_values
                )
                for value in option.values()
            )
        first, *remaining = selector.path
        if type(first) is not str:
            return ()
        selected = tuple(
            keyword.value
            for keyword in call.keywords
            if keyword.arg == first
        )
        selected += tuple(
            value
            for keyword in call.keywords
            if keyword.arg is None
            for value in _mapping_expression_values(
                keyword.value, first, assigned_values
            )
        )
        return _select_ast_values(
            selected, tuple(remaining), assigned_values
        )
    expressions = _call_argument_values(
        call,
        callee,
        parameter_index,
        bound_offset,
        assigned_values,
    )
    return _select_ast_values(expressions, selector.path, assigned_values)


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
        binding_events_by_name = {
            scope.name: _scope_binding_events(nodes_by_name[scope.name])
            for scope in scopes
        }
        constants_by_name = {}
        assignments_by_name = {}
        for scope in scopes:
            constants = dict(module_constants)
            constants.update(_string_constants(nodes_by_name[scope.name]))
            constants_by_name[scope.name] = constants
            assignments_by_name[scope.name] = _scope_binding_values(scope)
        infinity = (2**31 - 1, 2**31 - 1, 2**31 - 1)

        def visible_assigned_values(
            expression: ast.AST,
            scope_name: str,
            cutoff: tuple[int, int, int] = infinity,
        ) -> tuple[ast.AST, ...]:
            values = []
            for visible in _visible_scope_names(scope_name, known_scopes):
                visible_cutoff = cutoff if visible == scope_name else infinity
                resolved = _assigned_event_values(
                    binding_events_by_name[visible],
                    expression,
                    visible_cutoff,
                )
                if resolved:
                    values.extend(resolved)
                    break
            return tuple(dict.fromkeys(values))

        def rendered_expression(
            expression: ast.AST,
            scope_name: str,
            cutoff: tuple[int, int, int] = infinity,
            seen: frozenset[str] = frozenset(),
        ) -> str | None:
            if isinstance(expression, ast.Constant) and type(
                expression.value
            ) is str:
                return expression.value
            if isinstance(expression, ast.Name):
                if expression.id in seen:
                    return None
                rendered = {
                    value
                    for assigned in visible_assigned_values(
                        expression, scope_name, cutoff
                    )
                    if (
                        value := rendered_expression(
                            assigned,
                            scope_name,
                            cutoff,
                            seen | {expression.id},
                        )
                    )
                    is not None
                }
                return next(iter(rendered)) if len(rendered) == 1 else None
            if isinstance(expression, ast.BinOp) and isinstance(
                expression.op, ast.Add
            ):
                left = rendered_expression(
                    expression.left, scope_name, cutoff, seen
                )
                right = rendered_expression(
                    expression.right, scope_name, cutoff, seen
                )
                return None if left is None or right is None else left + right
            if isinstance(expression, ast.JoinedStr):
                parts = []
                for value in expression.values:
                    target = (
                        value.value
                        if isinstance(value, ast.FormattedValue)
                        else value
                    )
                    rendered = rendered_expression(
                        target, scope_name, cutoff, seen
                    )
                    if rendered is None:
                        return None
                    parts.append(rendered)
                return "".join(parts)
            if isinstance(expression, ast.Subscript):
                assigned_values = lambda item: visible_assigned_values(
                    item, scope_name, cutoff
                )
                rendered = {
                    value
                    for selected in _subscript_container_values(
                        expression, assigned_values
                    )
                    if (
                        value := rendered_expression(
                            selected, scope_name, cutoff, seen
                        )
                    )
                    is not None
                }
                return next(iter(rendered)) if len(rendered) == 1 else None
            return None

        sql_call_ids_by_name = {
            scope.name: {
                id(call)
                for call, _expression in _sql_expression_calls(
                    nodes_by_name[scope.name],
                    lambda expression, cutoff, scope_name=scope.name: (
                        visible_assigned_values(
                            expression, scope_name, cutoff
                        )
                    ),
                )
            }
            for scope in scopes
        }

        callables_by_short: dict[str, set[str]] = {}
        lambda_by_node = {}
        callable_by_node = {}
        for scope in scopes:
            if scope.name == "<module>" or ".<body>" in scope.name:
                continue
            callables_by_short.setdefault(
                scope.name.rsplit(".", 1)[-1], set()
            ).add(scope.name)
            if isinstance(scope.node, ast.Lambda):
                lambda_by_node[id(scope.node)] = scope.name
            callable_by_node[id(scope.node)] = scope.name

        def is_class_reference(
            expression: ast.AST,
            class_owner: str | None,
            caller_name: str,
            cutoff: tuple[int, int, int],
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
                    cutoff,
                    seen | {key},
                )
                for value in visible_assigned_values(
                    expression, caller_name, cutoff
                )
            )

        def callable_targets(
            expression: ast.AST,
            caller_name: str,
            cutoff: tuple[int, int, int] = infinity,
            seen: frozenset[tuple[str, str]] = frozenset(),
        ) -> frozenset[_ResolvedCallable]:
            direct = callable_by_node.get(id(expression))
            if direct is not None:
                return frozenset({_ResolvedCallable(direct, 0)})
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
                assigned_values = lambda item: visible_assigned_values(
                    item, caller_name, cutoff
                )
                candidates = (
                    *_subscript_container_values(
                        expression, assigned_values
                    ),
                    *visible_assigned_values(
                        expression, caller_name, cutoff
                    ),
                )
                for value in candidates:
                    resolved.update(
                        callable_targets(
                            value, caller_name, cutoff, seen | {key}
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
                                cutoff,
                            )
                            else 1
                        )
                    resolved.add(_ResolvedCallable(target, offset))
                return frozenset(resolved)
            return frozenset()

        def parameter_selectors(
            expression: ast.AST,
            scope: _ExecutableScope,
            cutoff: tuple[int, int, int] = infinity,
            seen: frozenset[str] = frozenset(),
        ) -> frozenset[_ParameterSelector]:
            if isinstance(expression, ast.Subscript):
                key_node = expression.slice
                if isinstance(key_node, ast.Constant) and type(
                    key_node.value
                ) in {str, int}:
                    return frozenset(
                        _ParameterSelector(
                            selector.index,
                            (*selector.path, key_node.value),
                        )
                        for selector in parameter_selectors(
                            expression.value, scope, cutoff, seen
                        )
                    )
            if isinstance(expression, ast.Name) and (
                expression.id in scope.positional_parameters
            ):
                return frozenset(
                    {
                        _ParameterSelector(
                            scope.positional_parameters.index(expression.id)
                        )
                    }
                )
            path = _expression_alias_key(expression)
            if path is None or path in seen:
                return frozenset()
            selectors = set()
            for value in _assigned_event_values(
                binding_events_by_name[scope.name], expression, cutoff
            ):
                selectors.update(
                    parameter_selectors(
                        value, scope, cutoff, seen | {path}
                    )
                )
            return frozenset(selectors)

        def argument_states(
            expression: ast.AST,
            scope_name: str,
            cutoff: tuple[int, int, int] = infinity,
            seen: frozenset[str] = frozenset(),
        ) -> frozenset[str]:
            rendered = rendered_expression(expression, scope_name, cutoff)
            if rendered in _PRESENCE_STATES:
                return frozenset({rendered})
            if isinstance(expression, ast.Attribute):
                state = _PRESENCE_STATE_BY_ATTRIBUTE.get(expression.attr)
                return frozenset() if state is None else frozenset({state})
            path = _expression_alias_key(expression)
            if path is not None and path not in seen:
                states = set()
                for value in visible_assigned_values(
                    expression, scope_name, cutoff
                ):
                    states.update(
                        argument_states(
                            value, scope_name, cutoff, seen | {path}
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
                for state in argument_states(
                    child, scope_name, cutoff, seen
                )
            )

        sink_parameters: dict[str, set[_ParameterSelector]] = {
            scope.name: set() for scope in scopes
        }
        writer_names = set()
        for scope in scopes:
            for sql_call, expression in _sql_expression_calls(
                nodes_by_name[scope.name],
                lambda item, cutoff, scope_name=scope.name: (
                    visible_assigned_values(item, scope_name, cutoff)
                ),
            ):
                cutoff = (
                    sql_call.lineno,
                    sql_call.col_offset,
                    infinity[2],
                )
                rendered = rendered_expression(
                    expression, scope.name, cutoff
                )
                if rendered is not None and writes_sql(rendered):
                    writer_names.add(scope.name)
                sink_parameters[scope.name].update(
                    parameter_selectors(expression, scope, cutoff)
                )

        edges: list[tuple[str, _ResolvedCallable, ast.Call]] = []
        for scope in scopes:
            for node in nodes_by_name[scope.name]:
                if not isinstance(node, ast.Call):
                    continue
                cutoff = (node.lineno, node.col_offset, infinity[2])
                resolved_targets = callable_targets(
                    node.func, scope.name, cutoff
                )
                for target in resolved_targets:
                    edges.append((scope.name, target, node))
                arguments = (
                    *node.args,
                    *(keyword.value for keyword in node.keywords),
                )
                rendered_arguments = tuple(
                    rendered_expression(argument, scope.name, cutoff)
                    for argument in arguments
                )
                if (
                    not resolved_targets
                    and id(node) not in sql_call_ids_by_name[scope.name]
                    and any(
                        rendered is not None and writes_sql(rendered)
                        for rendered in rendered_arguments
                    )
                ):
                    writer_names.add(scope.name)
                elif (
                    not resolved_targets
                    and id(node) not in sql_call_ids_by_name[scope.name]
                    and any(
                        argument_states(argument, scope.name, cutoff)
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
                cutoff = (call.lineno, call.col_offset, infinity[2])
                for selector in tuple(sink_parameters[callee_name]):
                    expressions = _call_selector_values(
                        call,
                        callee,
                        selector,
                        resolved.bound_offset,
                        lambda expression: visible_assigned_values(
                            expression, caller_name, cutoff
                        ),
                    )
                    for expression in expressions:
                        propagated = parameter_selectors(
                            expression, caller, cutoff
                        )
                        if not propagated <= sink_parameters[caller_name]:
                            sink_parameters[caller_name].update(propagated)
                            changed = True

        for scope in scopes:
            for selector in sink_parameters[scope.name]:
                parameter = scope.positional_parameters[selector.index]
                default = scope.default_for(parameter)
                selected_defaults = (
                    ()
                    if default is None
                    else _select_ast_values(
                        (default,),
                        selector.path,
                        lambda expression: visible_assigned_values(
                            expression, scope.name, infinity
                        ),
                    )
                )
                rendered = (
                    None
                    if len(selected_defaults) != 1
                    else rendered_expression(
                        selected_defaults[0], scope.name, infinity
                    )
                )
                if rendered is not None and writes_sql(rendered):
                    writer_names.add(scope.name)

        active_edges: set[tuple[str, str]] = set()
        for caller_name, resolved, call in edges:
            callee_name = resolved.name
            callee = scopes_by_name[callee_name]
            cutoff = (call.lineno, call.col_offset, infinity[2])
            for selector in sink_parameters[callee_name]:
                expressions = _call_selector_values(
                    call,
                    callee,
                    selector,
                    resolved.bound_offset,
                    lambda expression: visible_assigned_values(
                        expression, caller_name, cutoff
                    ),
                )
                if any(
                    (rendered := rendered_expression(
                        expression, caller_name, cutoff
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
                cutoff = (call.lineno, call.col_offset, infinity[2])
                for selector in sink_parameters[callee_name]:
                    expressions = _call_selector_values(
                        call,
                        callee,
                        selector,
                        resolved.bound_offset,
                        lambda expression: visible_assigned_values(
                            expression, caller_name, cutoff
                        ),
                    )
                    if not any(
                        parameter_selectors(expression, caller, cutoff)
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


@dataclass(frozen=True, slots=True)
class _ReviewValue:
    kind: str
    name: str = ""
    detail: str = ""
    selector: tuple[str | int, ...] = ()
    members: tuple[tuple[str, "_ReviewValue"], ...] = ()


@dataclass(frozen=True, slots=True)
class _ReviewBindingEvent:
    key: str
    position: tuple[int, int, int]
    kind: str
    node: ast.AST
    target: str = ""
    branches: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _ReviewFormal:
    scope: str
    parameter: str
    selector: tuple[str | int, ...] = ()


class _ReviewResolver:
    _INFINITY = (10**9, 10**9, 10**9)
    _FACTORIES = frozenset(
        {
            "classmethod",
            "dict",
            "getattr",
            "list",
            "property",
            "setattr",
            "staticmethod",
            "tuple",
            "vars",
        }
    )

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.parents_by_node = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        self.scopes = _executable_scopes(tree)
        self.scopes_by_name = {
            scope.name: scope for scope in self.scopes
        }
        self.function_scopes_by_node = {
            id(scope.node): scope
            for scope in self.scopes
            if isinstance(
                scope.node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
            )
        }
        self.class_scopes_by_node = {
            id(scope.node): scope
            for scope in self.scopes
            if isinstance(scope.node, ast.ClassDef)
        }
        self.classes = {
            scope.class_owner or "": scope
            for scope in self.scopes
            if isinstance(scope.node, ast.ClassDef)
        }
        self.class_names_by_short: dict[str, list[str]] = {}
        for name in self.classes:
            self.class_names_by_short.setdefault(
                name.rsplit(".", 1)[-1], []
            ).append(name)
        self.parents: dict[str, str | None] = {"<module>": None}
        self.events: dict[str, list[_ReviewBindingEvent]] = {
            scope.name: [] for scope in self.scopes
        }
        self.local_names: dict[str, set[str]] = {
            scope.name: set(scope.positional_parameters)
            for scope in self.scopes
        }
        self._pending_setattrs: list[tuple[str, ast.Call]] = []
        self._event_ordinal = 0
        self._record_definitions(tree, "<module>")
        self._record_bindings()
        self._record_setattr_bindings()
        for events in self.events.values():
            events.sort(key=lambda event: event.position)

    def _position(self, node: ast.AST) -> tuple[int, int, int]:
        self._event_ordinal += 1
        return (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
            self._event_ordinal,
        )

    def _add_event(
        self,
        scope: str,
        key: str,
        kind: str,
        node: ast.AST,
        target: str = "",
    ) -> None:
        self.events[scope].append(
            _ReviewBindingEvent(
                key,
                self._position(node),
                kind,
                node,
                target,
                self._branch_path(node),
            )
        )
        self.local_names[scope].add(key)

    def _branch_path(
        self, node: ast.AST
    ) -> tuple[tuple[int, str], ...]:
        branches = []
        child = node
        parent = self.parents_by_node.get(id(child))
        while parent is not None:
            if isinstance(parent, ast.If):
                arm = "body" if child in parent.body else "else"
                branches.append((id(parent), arm))
            child = parent
            parent = self.parents_by_node.get(id(child))
        return tuple(reversed(branches))

    def _record_definitions(self, node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                class_scope = self.class_scopes_by_node[id(child)]
                self._add_event(
                    owner,
                    child.name,
                    "class",
                    child,
                    class_scope.class_owner or child.name,
                )
                self.parents[class_scope.name] = owner
                self._record_definitions(child, class_scope.name)
            elif isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                function_scope = self.function_scopes_by_node[id(child)]
                self._add_event(
                    owner,
                    child.name,
                    "function",
                    child,
                    function_scope.name,
                )
                self.parents[function_scope.name] = owner
                self._record_definitions(child, function_scope.name)
            elif isinstance(child, ast.Lambda):
                function_scope = self.function_scopes_by_node[id(child)]
                self.parents[function_scope.name] = owner
                self._record_definitions(child, function_scope.name)
            else:
                self._record_definitions(child, owner)

    def _record_bindings(self) -> None:
        for scope in self.scopes:
            for node in _scope_nodes(scope.node):
                if isinstance(
                    node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)
                ):
                    value = node.value
                    if value is None:
                        continue
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else (node.target,)
                    )
                    for target in targets:
                        key = _expression_alias_key(target)
                        if key is not None:
                            self._add_event(
                                scope.name, key, "assignment", value
                            )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self._add_event(
                            scope.name,
                            alias.asname or alias.name.split(".", 1)[0],
                            "import",
                            node,
                            alias.name,
                        )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        self._add_event(
                            scope.name,
                            alias.asname or alias.name,
                            "from_import",
                            node,
                            f"{module}:{alias.name}",
                        )
                elif (
                    isinstance(node, ast.Call)
                    and len(node.args) >= 3
                    and isinstance(node.args[1], ast.Constant)
                    and type(node.args[1].value) is str
                ):
                    self._pending_setattrs.append((scope.name, node))

    @staticmethod
    def _is_setattr_syntax(expression: ast.AST) -> bool:
        return (
            isinstance(expression, ast.Name)
            and expression.id == "setattr"
        ) or (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id == "builtins"
            and expression.attr == "setattr"
        )

    def _is_builtin_setattr(
        self,
        expression: ast.AST,
        scope: str,
        cutoff: tuple[int, int, int],
        seen: frozenset[str] = frozenset(),
    ) -> bool:
        if isinstance(expression, ast.Attribute):
            return (
                isinstance(expression.value, ast.Name)
                and expression.value.id == "builtins"
                and expression.attr == "setattr"
            )
        if not isinstance(expression, ast.Name) or expression.id in seen:
            return False
        events = self._matching_events(scope, expression.id, cutoff)
        if events:
            return any(
                event.kind == "from_import"
                and event.target == "builtins:setattr"
                or event.kind == "assignment"
                and self._is_builtin_setattr(
                    event.node,
                    scope,
                    event.position,
                    seen | {expression.id},
                )
                for event in events
            )
        if expression.id == "setattr":
            return scope == "<module>" or expression.id not in self.local_names[
                scope
            ]
        outer = self._outer_lookup(scope)
        return (
            outer is not None
            and self._is_builtin_setattr(
                expression, outer[0], outer[1], seen | {expression.id}
            )
        )

    def _record_setattr_bindings(self) -> None:
        for scope, call in self._pending_setattrs:
            cutoff = (
                call.lineno,
                call.col_offset,
                self._INFINITY[2],
            )
            if not self._is_builtin_setattr(call.func, scope, cutoff):
                continue
            owner = _expression_alias_key(call.args[0])
            if owner is None:
                continue
            assert isinstance(call.args[1], ast.Constant)
            self._add_event(
                scope,
                f"{owner}.{call.args[1].value}",
                "assignment",
                call.args[2],
            )

    @staticmethod
    def _mutually_exclusive(
        left: _ReviewBindingEvent,
        right: _ReviewBindingEvent,
    ) -> bool:
        right_branches = dict(right.branches)
        return any(
            branch_id in right_branches
            and right_branches[branch_id] != arm
            for branch_id, arm in left.branches
        )

    def _matching_events(
        self,
        scope: str,
        key: str,
        cutoff: tuple[int, int, int],
    ) -> tuple[_ReviewBindingEvent, ...]:
        candidates = sorted(
            (
                event
                for event in self.events[scope]
                if event.position < cutoff
                and _alias_keys_overlap(event.key, key)
            ),
            key=lambda event: event.position,
            reverse=True,
        )
        if not candidates:
            return ()
        selected = [candidates[0]]
        selected.extend(
            candidate
            for candidate in candidates[1:]
            if any(
                self._mutually_exclusive(candidate, current)
                for current in selected
            )
        )
        return tuple(selected)

    def _matching_event(
        self,
        scope: str,
        key: str,
        cutoff: tuple[int, int, int],
    ) -> _ReviewBindingEvent | None:
        return next(iter(self._matching_events(scope, key, cutoff)), None)

    def _outer_lookup(
        self,
        scope: str,
    ) -> tuple[str, tuple[int, int, int]] | None:
        parent = self.parents.get(scope)
        if parent is None:
            return None
        current = self.scopes_by_name[scope]
        if parent.endswith(".<body>") and current.method_kind in {
            "instance",
            "class",
            "static",
        }:
            parent = self.parents.get(parent)
            if parent is None:
                return None
        if scope.endswith(".<body>"):
            return (
                parent,
                (
                    getattr(current.node, "lineno", 0),
                    getattr(current.node, "col_offset", 0),
                    self._INFINITY[2],
                ),
            )
        return parent, self._INFINITY

    def _event_values(
        self,
        event: _ReviewBindingEvent,
        scope: str,
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        if event.kind == "assignment":
            return self.evaluate(
                event.node,
                scope,
                event.position,
                environment,
                seen,
            )
        if event.kind == "class":
            return (_ReviewValue("class", event.target),)
        if event.kind == "function":
            value = _ReviewValue("function", event.target, "0")
            function = event.node
            if not isinstance(
                function, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                return (value,)
            values = (value,)
            for decorator in reversed(function.decorator_list):
                factories = self.evaluate(
                    decorator,
                    scope,
                    event.position,
                    environment,
                    seen,
                )
                wrapped = []
                for factory in factories:
                    if factory.kind == "factory" and factory.name in {
                        "classmethod",
                        "property",
                        "staticmethod",
                    }:
                        wrapped.extend(
                            _ReviewValue(
                                "descriptor",
                                factory.name,
                                members=(("wrapped", item),),
                            )
                            for item in values
                        )
                if wrapped:
                    values = tuple(wrapped)
            return values
        if event.kind == "import":
            if event.target in {"builtins", "functools"}:
                return (_ReviewValue("module", event.target),)
            return (_ReviewValue("unknown", event.target),)
        if event.kind == "from_import":
            module, symbol = event.target.split(":", 1)
            if module == "builtins" and symbol in self._FACTORIES:
                return (_ReviewValue("factory", symbol),)
            if module == "functools" and symbol == "partial":
                return (_ReviewValue("factory", "partial"),)
            if symbol == "add_review_and_decision":
                return (_ReviewValue("writer"),)
            return (_ReviewValue("unknown", event.target),)
        return ()

    def _default_values(
        self,
        scope: _ExecutableScope,
        parameter: str,
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        default = scope.default_for(parameter)
        if default is None:
            return ()
        parent = self.parents.get(scope.name) or "<module>"
        cutoff = (
            getattr(scope.node, "lineno", 0),
            getattr(scope.node, "col_offset", 0),
            self._INFINITY[2],
        )
        return self.evaluate(default, parent, cutoff, {}, seen)

    def _lookup_name(
        self,
        name: str,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        if name in environment:
            return environment[name]
        scope = self.scopes_by_name[scope_name]
        events = self._matching_events(scope_name, name, cutoff)
        if events:
            values = tuple(
                dict.fromkeys(
                    value
                    for event in events
                    for key in [(scope_name, name, event.position)]
                    if key not in seen
                    for value in self._event_values(
                        event, scope_name, environment, seen | {key}
                    )
                )
            )
            if values and all(value.kind == "instance" for value in values):
                values = tuple(
                    _ReviewValue(
                        value.kind,
                        value.name,
                        name,
                        value.selector,
                        value.members,
                    )
                    for value in values
                )
            return values
        if name in scope.positional_parameters:
            index = scope.positional_parameters.index(name)
            values: tuple[_ReviewValue, ...] = (
                _ReviewValue("parameter", f"{scope_name}:{name}"),
            )
            if (
                index == 0
                and scope.class_owner is not None
                and scope.method_kind in {"instance", "class"}
            ):
                receiver_kind = (
                    "instance"
                    if scope.method_kind == "instance"
                    else "class"
                )
                values = (
                    _ReviewValue(receiver_kind, scope.class_owner, name),
                    *values,
                )
            return tuple(
                dict.fromkeys(
                    (*values, *self._default_values(scope, name, seen))
                )
            )
        if (
            scope_name != "<module>"
            and name in self.local_names[scope_name]
        ):
            return (_ReviewValue("unknown", name),)
        outer = self._outer_lookup(scope_name)
        if outer is not None:
            return self._lookup_name(
                name, outer[0], outer[1], environment, seen
            )
        if name in self._FACTORIES:
            return (_ReviewValue("factory", name),)
        if name == "partial":
            return (_ReviewValue("unknown", name),)
        return (_ReviewValue("unknown", name),)

    @staticmethod
    def _literal_key(expression: ast.AST) -> str | int | None:
        if (
            isinstance(expression, ast.Constant)
            and type(expression.value) in {str, int}
        ):
            return expression.value
        return None

    @staticmethod
    def _container(
        kind: str,
        entries: Iterable[tuple[str | int, _ReviewValue]],
    ) -> _ReviewValue:
        return _ReviewValue(
            kind,
            members=tuple((str(key), value) for key, value in entries),
        )

    @staticmethod
    def _select_values(
        values: Iterable[_ReviewValue],
        selector: tuple[str | int, ...],
    ) -> tuple[_ReviewValue, ...]:
        selected = tuple(values)
        for key in selector:
            next_values = []
            rendered = str(key)
            for value in selected:
                if value.kind == "parameter":
                    next_values.append(
                        _ReviewValue(
                            "parameter",
                            value.name,
                            value.detail,
                            (*value.selector, key),
                        )
                    )
                elif value.kind in {"mapping", "sequence"}:
                    next_values.extend(
                        item
                        for item_key, item in value.members
                        if item_key == rendered
                    )
            selected = tuple(dict.fromkeys(next_values))
        return selected

    def _class_name(self, expression: ast.AST) -> str | None:
        path = _expression_alias_key(expression)
        if path in self.classes:
            return path
        if path is None:
            return None
        candidates = self.class_names_by_short.get(path, ())
        return candidates[0] if len(candidates) == 1 else None

    def _base_names(self, class_name: str) -> tuple[str, ...]:
        scope = self.classes[class_name]
        assert isinstance(scope.node, ast.ClassDef)
        names = []
        for base in scope.node.bases:
            resolved = self._class_name(base)
            if resolved is not None:
                names.append(resolved)
        return tuple(names)

    def _mro(
        self,
        class_name: str,
        seen: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        if class_name in seen:
            return (class_name,)
        bases = self._base_names(class_name)
        sequences = [
            list(self._mro(base, seen | {class_name})) for base in bases
        ]
        sequences.append(list(bases))
        result = [class_name]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(
                        sequence[0] not in other[1:]
                        for other in sequences
                    )
                ),
                None,
            )
            if candidate is None:
                result.extend(
                    item
                    for sequence in sequences
                    for item in sequence
                    if item not in result
                )
                break
            result.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        return tuple(dict.fromkeys(result))

    def _raw_class_member(
        self,
        class_name: str,
        member: str,
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        short = class_name.rsplit(".", 1)[-1]
        parent = self.parents.get(self.classes[class_name].name)
        if parent is not None:
            event = self._matching_event(
                parent, f"{short}.{member}", self._INFINITY
            )
            if event is not None:
                return self._event_values(event, parent, {}, seen)
        class_scope = self.classes[class_name]
        event = self._matching_event(
            class_scope.name, member, self._INFINITY
        )
        if event is None:
            return ()
        return self._event_values(event, class_scope.name, {}, seen)

    def _effective_raw_member(
        self,
        class_name: str,
        member: str,
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        for owner in self._mro(class_name):
            values = self._raw_class_member(owner, member, seen)
            if values:
                return values
        return ()

    def _instance_member(
        self,
        value: _ReviewValue,
        member: str,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        raw = self._effective_raw_member(value.name, member, seen)
        properties = tuple(
            item
            for item in raw
            if item.kind == "descriptor" and item.name == "property"
        )
        if properties:
            return tuple(
                returned
                for descriptor in properties
                for _key, getter in descriptor.members
                for returned in self._function_returns(
                    getter, (), scope_name, cutoff, environment, seen
                )
            )
        if value.detail:
            event = self._matching_event(
                scope_name, f"{value.detail}.{member}", cutoff
            )
            if event is None and scope_name != "<module>":
                event = self._matching_event(
                    "<module>",
                    f"{value.detail}.{member}",
                    self._INFINITY,
                )
            if event is not None:
                return self._event_values(
                    event,
                    scope_name if event in self.events[scope_name] else "<module>",
                    environment,
                    seen,
                )
        for owner in self._mro(value.name):
            init = self._raw_class_member(owner, "__init__", seen)
            for function in init:
                if function.kind != "function":
                    continue
                init_scope = self.scopes_by_name[function.name]
                if not init_scope.positional_parameters:
                    continue
                receiver = init_scope.positional_parameters[0]
                event = self._matching_event(
                    init_scope.name,
                    f"{receiver}.{member}",
                    self._INFINITY,
                )
                if event is not None:
                    return self._event_values(
                        event, init_scope.name, {}, seen
                    )
            if init:
                break
        return self._bind_descriptor(raw, instance=True)

    @staticmethod
    def _bind_descriptor(
        values: Iterable[_ReviewValue],
        *,
        instance: bool,
    ) -> tuple[_ReviewValue, ...]:
        bound = []
        for value in values:
            if value.kind != "descriptor":
                if value.kind == "function" and instance:
                    bound.append(
                        _ReviewValue("function", value.name, "1")
                    )
                else:
                    bound.append(value)
                continue
            wrapped = tuple(item for _key, item in value.members)
            if value.name == "property":
                bound.append(value)
            elif value.name == "classmethod":
                bound.extend(
                    _ReviewValue(
                        item.kind,
                        item.name,
                        "1" if item.kind == "function" else item.detail,
                        item.selector,
                        item.members,
                    )
                    for item in wrapped
                )
            else:
                bound.extend(wrapped)
        return tuple(dict.fromkeys(bound))

    def _attribute_values(
        self,
        receivers: tuple[_ReviewValue, ...],
        attribute: str,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        values = []
        for receiver in receivers:
            if receiver.kind == "module":
                if receiver.name == "builtins" and attribute in self._FACTORIES:
                    values.append(_ReviewValue("factory", attribute))
                elif receiver.name == "functools" and attribute == "partial":
                    values.append(_ReviewValue("factory", "partial"))
                else:
                    values.append(
                        _ReviewValue(
                            "unknown", f"{receiver.name}.{attribute}"
                        )
                    )
            elif receiver.kind == "instance":
                member_values = self._instance_member(
                    receiver,
                    attribute,
                    scope_name,
                    cutoff,
                    environment,
                    seen,
                )
                if member_values:
                    values.extend(member_values)
                elif attribute == "add_review_and_decision":
                    values.append(_ReviewValue("writer"))
            elif receiver.kind == "class":
                if attribute == "__dict__":
                    values.append(_ReviewValue("classdict", receiver.name))
                else:
                    raw = self._effective_raw_member(
                        receiver.name, attribute, seen
                    )
                    if raw:
                        values.extend(
                            self._bind_descriptor(raw, instance=False)
                        )
                    elif attribute == "add_review_and_decision":
                        values.append(_ReviewValue("writer"))
            elif receiver.kind == "classdict" and attribute == "get":
                values.append(
                    _ReviewValue("classdict_get", receiver.name)
                )
            elif receiver.kind == "descriptor":
                if attribute == "__get__":
                    values.append(
                        _ReviewValue(
                            "descriptor_get",
                            receiver.name,
                            members=receiver.members,
                        )
                    )
                elif attribute in {"__func__", "__call__"}:
                    values.extend(item for _key, item in receiver.members)
            elif attribute == "__call__":
                values.append(receiver)
            elif attribute == "add_review_and_decision":
                values.append(_ReviewValue("writer"))
            else:
                values.append(
                    _ReviewValue(
                        "unknown", f"{receiver.name}.{attribute}"
                    )
                )
        return tuple(dict.fromkeys(values))

    def _mapping_values(
        self,
        expression: ast.AST,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        entries: list[tuple[str, _ReviewValue]] = []

        def merge(additions: Iterable[tuple[str, _ReviewValue]]) -> None:
            additions = tuple(additions)
            keys = {key for key, _value in additions}
            entries[:] = [
                item for item in entries if item[0] not in keys
            ]
            entries.extend(additions)

        if isinstance(expression, ast.Dict):
            for key_node, value in zip(
                expression.keys, expression.values, strict=True
            ):
                if key_node is None:
                    for mapping in self.evaluate(
                        value, scope_name, cutoff, environment, seen
                    ):
                        if mapping.kind == "mapping":
                            merge(mapping.members)
                else:
                    key = self._literal_key(key_node)
                    if type(key) is str:
                        merge(
                            (str(key), item)
                            for item in self.evaluate(
                                value,
                                scope_name,
                                cutoff,
                                environment,
                                seen,
                            )
                        )
            return (self._container("mapping", entries),)
        if isinstance(expression, ast.BinOp) and isinstance(
            expression.op, ast.BitOr
        ):
            for side in (expression.left, expression.right):
                for mapping in self.evaluate(
                    side, scope_name, cutoff, environment, seen
                ):
                    if mapping.kind == "mapping":
                        merge(mapping.members)
            return (self._container("mapping", entries),)
        return ()

    def _call_arguments(
        self,
        call: ast.Call,
        callee: _ExecutableScope,
        bound_offset: int,
        caller_scope: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> dict[str, tuple[_ReviewValue, ...]]:
        def assigned_values(expression: ast.AST) -> tuple[ast.AST, ...]:
            key = _expression_alias_key(expression)
            if key is None:
                return ()
            event = self._matching_event(caller_scope, key, cutoff)
            if event is None or event.kind != "assignment":
                return ()
            return (event.node,)

        arguments = {}
        for index, parameter in enumerate(callee.positional_parameters):
            expressions = _call_argument_values(
                call,
                callee,
                index,
                bound_offset,
                assigned_values,
            )
            values = tuple(
                value
                for expression in expressions
                for value in self.evaluate(
                    expression,
                    caller_scope,
                    cutoff,
                    environment,
                    seen,
                )
            )
            if values:
                arguments[parameter] = tuple(dict.fromkeys(values))
        return arguments

    def _function_returns(
        self,
        function: _ReviewValue,
        call_args: tuple[ast.Call, int] | tuple[()],
        caller_scope: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        if function.kind != "function":
            return ()
        scope = self.scopes_by_name[function.name]
        bound = int(function.detail or "0")
        bound_environment = {}
        if call_args:
            bound_environment = self._call_arguments(
                call_args[0],
                scope,
                call_args[1] + bound,
                caller_scope,
                cutoff,
                environment,
                seen,
            )
        return tuple(
            value
            for node in _scope_nodes(scope.node)
            if isinstance(node, ast.Return) and node.value is not None
            for value in self.evaluate(
                node.value,
                scope.name,
                (
                    node.lineno,
                    node.col_offset,
                    self._INFINITY[2],
                ),
                bound_environment,
                seen,
            )
        )

    def _call_values(
        self,
        expression: ast.Call,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]],
        seen: frozenset[tuple[str, str, tuple[int, int, int]]],
    ) -> tuple[_ReviewValue, ...]:
        functions = self.evaluate(
            expression.func, scope_name, cutoff, environment, seen
        )
        values = []
        for function in functions:
            if function.kind == "factory":
                if function.name in {
                    "classmethod",
                    "property",
                    "staticmethod",
                } and expression.args:
                    values.extend(
                        _ReviewValue(
                            "descriptor",
                            function.name,
                            members=(("wrapped", item),),
                        )
                        for item in self.evaluate(
                            expression.args[0],
                            scope_name,
                            cutoff,
                            environment,
                            seen,
                        )
                    )
                elif function.name == "partial" and expression.args:
                    values.extend(
                        self.evaluate(
                            expression.args[0],
                            scope_name,
                            cutoff,
                            environment,
                            seen,
                        )
                    )
                elif function.name == "dict":
                    entries: list[tuple[str, _ReviewValue]] = []
                    for argument in expression.args:
                        for mapping in self.evaluate(
                            argument,
                            scope_name,
                            cutoff,
                            environment,
                            seen,
                        ):
                            if mapping.kind == "mapping":
                                keys = {
                                    key for key, _value in mapping.members
                                }
                                entries = [
                                    item
                                    for item in entries
                                    if item[0] not in keys
                                ]
                                entries.extend(mapping.members)
                    for keyword in expression.keywords:
                        if keyword.arg is None:
                            continue
                        entries = [
                            item
                            for item in entries
                            if item[0] != keyword.arg
                        ]
                        entries.extend(
                            (keyword.arg, item)
                            for item in self.evaluate(
                                keyword.value,
                                scope_name,
                                cutoff,
                                environment,
                                seen,
                            )
                        )
                    values.append(self._container("mapping", entries))
                elif function.name in {"list", "tuple"}:
                    if expression.args:
                        values.extend(
                            self.evaluate(
                                expression.args[0],
                                scope_name,
                                cutoff,
                                environment,
                                seen,
                            )
                        )
                    else:
                        values.append(self._container("sequence", ()))
                elif function.name == "getattr" and len(expression.args) >= 2:
                    attribute = self._literal_key(expression.args[1])
                    if type(attribute) is str:
                        receivers = self.evaluate(
                            expression.args[0],
                            scope_name,
                            cutoff,
                            environment,
                            seen,
                        )
                        values.extend(
                            self._attribute_values(
                                receivers,
                                attribute,
                                scope_name,
                                cutoff,
                                environment,
                                seen,
                            )
                        )
                elif function.name == "vars" and expression.args:
                    for item in self.evaluate(
                        expression.args[0],
                        scope_name,
                        cutoff,
                        environment,
                        seen,
                    ):
                        if item.kind == "class":
                            values.append(
                                _ReviewValue("classdict", item.name)
                            )
            elif function.kind == "class":
                values.append(_ReviewValue("instance", function.name))
            elif function.kind == "function":
                values.extend(
                    self._function_returns(
                        function,
                        (expression, 0),
                        scope_name,
                        cutoff,
                        environment,
                        seen,
                    )
                )
            elif function.kind == "classdict_get" and expression.args:
                member = self._literal_key(expression.args[0])
                if type(member) is str:
                    values.extend(
                        self._raw_class_member(function.name, member, seen)
                    )
            elif function.kind == "descriptor_get":
                values.extend(item for _key, item in function.members)
            else:
                values.append(_ReviewValue("unknown", "call-result"))
        return tuple(dict.fromkeys(values))

    def evaluate(
        self,
        expression: ast.AST,
        scope_name: str,
        cutoff: tuple[int, int, int],
        environment: dict[str, tuple[_ReviewValue, ...]] | None = None,
        seen: frozenset[
            tuple[str, str, tuple[int, int, int]]
        ] = frozenset(),
    ) -> tuple[_ReviewValue, ...]:
        environment = environment or {}
        if isinstance(expression, ast.Name):
            return self._lookup_name(
                expression.id, scope_name, cutoff, environment, seen
            )
        if isinstance(expression, ast.Constant):
            if type(expression.value) in {str, int}:
                return (
                    _ReviewValue("literal", repr(expression.value)),
                )
            return (_ReviewValue("unknown", "literal"),)
        if isinstance(expression, ast.Attribute):
            receivers = self.evaluate(
                expression.value,
                scope_name,
                cutoff,
                environment,
                seen,
            )
            return self._attribute_values(
                receivers,
                expression.attr,
                scope_name,
                cutoff,
                environment,
                seen,
            )
        if isinstance(expression, ast.Call):
            return self._call_values(
                expression, scope_name, cutoff, environment, seen
            )
        if isinstance(expression, (ast.Tuple, ast.List)):
            entries = []
            index = 0
            for item in expression.elts:
                if isinstance(item, ast.Starred):
                    for sequence in self.evaluate(
                        item.value,
                        scope_name,
                        cutoff,
                        environment,
                        seen,
                    ):
                        if sequence.kind != "sequence":
                            continue
                        for _key, value in sequence.members:
                            entries.append((index, value))
                            index += 1
                else:
                    for value in self.evaluate(
                        item,
                        scope_name,
                        cutoff,
                        environment,
                        seen,
                    ):
                        entries.append((index, value))
                    index += 1
            return (self._container("sequence", entries),)
        if isinstance(expression, (ast.Dict, ast.BinOp)):
            mappings = self._mapping_values(
                expression,
                scope_name,
                cutoff,
                environment,
                seen,
            )
            if mappings:
                return mappings
        if isinstance(expression, ast.Subscript):
            path = _expression_alias_key(expression)
            if path is not None:
                event = self._matching_event(scope_name, path, cutoff)
                if event is None and scope_name != "<module>":
                    event = self._matching_event(
                        "<module>", path, self._INFINITY
                    )
                if event is not None:
                    owner = (
                        scope_name
                        if event in self.events[scope_name]
                        else "<module>"
                    )
                    return self._event_values(
                        event, owner, environment, seen
                    )
            keys = self.evaluate(
                expression.slice,
                scope_name,
                cutoff,
                environment,
                seen,
            )
            selectors = tuple(
                ast.literal_eval(value.name)
                for value in keys
                if value.kind == "literal"
            )
            containers = self.evaluate(
                expression.value,
                scope_name,
                cutoff,
                environment,
                seen,
            )
            if any(
                value.kind == "classdict" for value in containers
            ):
                return tuple(
                    item
                    for container in containers
                    if container.kind == "classdict"
                    for selector in selectors
                    if type(selector) is str
                    for item in self._raw_class_member(
                        container.name, selector, seen
                    )
                )
            return tuple(
                dict.fromkeys(
                    item
                    for selector in selectors
                    for item in self._select_values(
                        containers, (selector,)
                    )
                )
            )
        return (_ReviewValue("unknown", type(expression).__name__),)

    @staticmethod
    def origins(
        values: Iterable[_ReviewValue],
    ) -> tuple[bool, frozenset[_ReviewFormal]]:
        writer = False
        formals = set()
        for value in values:
            if value.kind == "writer":
                writer = True
            elif value.kind == "parameter":
                scope, parameter = value.name.rsplit(":", 1)
                formals.add(
                    _ReviewFormal(scope, parameter, value.selector)
                )
            elif value.kind == "descriptor":
                nested_writer, nested_formals = _ReviewResolver.origins(
                    item for _key, item in value.members
                )
                writer = writer or nested_writer
                formals.update(nested_formals)
        return writer, frozenset(formals)

    def writer_scopes(self) -> frozenset[str]:
        sinks: dict[str, set[_ReviewFormal]] = {
            scope.name: set() for scope in self.scopes
        }
        writers = set()
        edges: list[tuple[str, _ReviewValue, ast.Call]] = []
        dependencies: set[tuple[str, str]] = set()
        for scope in self.scopes:
            for node in _scope_nodes(scope.node):
                if not isinstance(node, ast.Call):
                    continue
                cutoff = (
                    node.lineno,
                    node.col_offset,
                    self._INFINITY[2],
                )
                values = self.evaluate(
                    node.func, scope.name, cutoff, {}
                )
                writer, formals = self.origins(values)
                if writer:
                    writers.add(scope.name)
                sinks[scope.name].update(
                    formal
                    for formal in formals
                    if formal.scope == scope.name
                )
                edges.extend(
                    (scope.name, value, node)
                    for value in values
                    if value.kind == "function"
                )
        changed = True
        while changed:
            changed = False
            for caller_name, function, call in edges:
                callee = self.scopes_by_name[function.name]
                cutoff = (
                    call.lineno,
                    call.col_offset,
                    self._INFINITY[2],
                )
                for formal in tuple(sinks[callee.name]):
                    if formal.parameter not in callee.positional_parameters:
                        continue
                    index = callee.positional_parameters.index(
                        formal.parameter
                    )
                    arguments = self._call_arguments(
                        call,
                        callee,
                        int(function.detail or "0"),
                        caller_name,
                        cutoff,
                        {},
                        frozenset(),
                    ).get(formal.parameter, ())
                    selected = self._select_values(
                        arguments, formal.selector
                    )
                    writer, caller_formals = self.origins(selected)
                    if writer:
                        dependencies.add((caller_name, callee.name))
                        before = len(writers)
                        writers.update({caller_name, callee.name})
                        changed = changed or len(writers) != before
                    propagated = {
                        item
                        for item in caller_formals
                        if item.scope == caller_name
                    }
                    if not propagated <= sinks[caller_name]:
                        dependencies.add((caller_name, callee.name))
                        sinks[caller_name].update(propagated)
                        changed = True
        changed = True
        while changed:
            changed = False
            for caller_name, callee_name in dependencies:
                if caller_name not in writers and callee_name not in writers:
                    continue
                before = len(writers)
                writers.update({caller_name, callee_name})
                changed = changed or len(writers) != before
        return frozenset(writers)


def review_writer_callers(
    files: Iterable[Path] = PRODUCTION_FILES,
) -> tuple[str, ...]:
    callers = []
    for path in files:
        resolver = _ReviewResolver(_tree(path))
        callers.extend(
            f"{_relative(path)}:{scope}"
            for scope in resolver.writer_scopes()
        )
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
    ("mutation", "writers"),
    (
        (
            "def execute_sql(conn, *sql_args):\n"
            "    conn.execute(*sql_args)\n"
            "def write(conn):\n"
            "    sql_args = (\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n"
            "    execute_sql(conn, *sql_args)\n",
            ("execute_sql", "write"),
        ),
        (
            "def write(conn):\n"
            "    sinks = (conn.execute,)\n"
            "    args = (\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n"
            "    sinks[0](*args)\n",
            ("write",),
        ),
        (
            "def write(conn):\n"
            "    sinks = {'write': conn.execute}\n"
            "    args = (\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n"
            "    sinks['write'](*args)\n",
            ("write",),
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def write(conn):\n"
            "    left = {'conn': conn, 'statement': "
            "            'INSERT INTO presence_decisions '"
            "            '(candidate_id, state) VALUES (?, ?)'}\n"
            "    right = {'values': (1, 'presence_confirmed')}\n"
            "    kwargs = left | right\n"
            "    execute_sql(**kwargs)\n",
            ("execute_sql", "write"),
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def write(conn):\n"
            "    kwargs = dict({\n"
            "        'conn': conn,\n"
            "        'statement': 'INSERT INTO presence_decisions '"
            "                     '(candidate_id, state) VALUES (?, ?)',\n"
            "        'values': (1, 'presence_confirmed'),\n"
            "    })\n"
            "    execute_sql(**kwargs)\n",
            ("execute_sql", "write"),
        ),
    ),
)
def test_raw_presence_writer_guard_resolves_runtime_call_dataflow(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE presence_decisions "
            "(candidate_id INTEGER, state TEXT)"
        )
        namespace: dict[str, object] = {}
        exec(compile(tree, "<runtime-sql-mutation>", "exec"), namespace)
        namespace["write"](conn)  # type: ignore[operator]
        assert conn.execute(
            "SELECT candidate_id, state FROM presence_decisions"
        ).fetchone() == (1, "presence_confirmed")
    finally:
        conn.close()

    fake = Path("runtime-sql-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/runtime_sql.py"
    )

    expected = tuple(
        f"workers/runtime_sql.py:{writer}" for writer in writers
    )
    assert presence_decision_writer_calls((fake,)) == expected
    assert _presence_writer_details((fake,)) == {
        writer: frozenset({"presence_confirmed"}) for writer in expected
    }


@pytest.mark.parametrize(
    "control",
    (
        (
            "def execute_sql(conn, *sql_args):\n"
            "    return conn.execute(*sql_args).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    sql_args = ('SELECT ?', ('control',))\n"
            "    return execute_sql(conn, *sql_args)\n"
        ),
        (
            "def inspect(conn):\n"
            "    sinks = (conn.execute,)\n"
            "    args = ('SELECT ?', ('control',))\n"
            "    return sinks[0](*args).fetchone()[0]\n"
        ),
        (
            "def inspect(conn):\n"
            "    sinks = {'read': conn.execute}\n"
            "    args = ('SELECT ?', ('control',))\n"
            "    return sinks['read'](*args).fetchone()[0]\n"
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    left = {'conn': conn, 'statement': 'SELECT ?'}\n"
            "    right = {'values': ('control',)}\n"
            "    kwargs = left | right\n"
            "    return execute_sql(**kwargs)\n"
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    kwargs = dict({\n"
            "        'conn': conn, 'statement': 'SELECT ?',\n"
            "        'values': ('control',),\n"
            "    })\n"
            "    return execute_sql(**kwargs)\n"
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    left = {\n"
            "        'conn': conn,\n"
            "        'statement': 'INSERT INTO presence_decisions '"
            "                     '(candidate_id, state) VALUES (?, ?)',\n"
            "    }\n"
            "    right = {\n"
            "        'statement': 'SELECT ?', 'values': ('control',),\n"
            "    }\n"
            "    return execute_sql(**(left | right))\n"
        ),
    ),
)
def test_raw_presence_writer_guard_accepts_runtime_read_only_dataflow(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        namespace: dict[str, object] = {}
        exec(compile(tree, "<runtime-sql-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == "control"  # type: ignore[operator]
    finally:
        conn.close()

    fake = Path("runtime-sql-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/runtime_sql_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "def execute_sql(conn, prefix, *sql_args):\n"
            "    conn.execute(sql_args[1], sql_args[2])\n"
            "def write(conn):\n"
            "    execute_sql(\n"
            "        conn, 'prefix', 'unused',\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n",
            ("execute_sql", "write"),
        ),
        (
            "def execute_sql(conn, **sql_kwargs):\n"
            "    conn.execute(\n"
            "        sql_kwargs['statement'], sql_kwargs['values']\n"
            "    )\n"
            "def write(conn):\n"
            "    execute_sql(\n"
            "        conn,\n"
            "        statement='INSERT INTO presence_decisions '"
            "                  '(candidate_id, state) VALUES (?, ?)',\n"
            "        values=(1, 'presence_confirmed'),\n"
            "    )\n",
            ("execute_sql", "write"),
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def write(conn):\n"
            "    kwargs = dict([\n"
            "        ('conn', conn),\n"
            "        ('statement', 'INSERT INTO presence_decisions '"
            "                      '(candidate_id, state) VALUES (?, ?)'),\n"
            "        ('values', (1, 'presence_confirmed')),\n"
            "    ])\n"
            "    execute_sql(**kwargs)\n",
            ("execute_sql", "write"),
        ),
        (
            "def write(conn):\n"
            "    sinks = tuple((conn.execute,))\n"
            "    sinks[0](\n"
            "        'INSERT INTO presence_decisions '"
            "        '(candidate_id, state) VALUES (?, ?)',\n"
            "        (1, 'presence_confirmed'),\n"
            "    )\n",
            ("write",),
        ),
        (
            "def write(conn):\n"
            "    statement = 'SELECT ?'\n"
            "    statement = 'INSERT INTO presence_decisions '"
            "                '(candidate_id, state) VALUES (?, ?)'\n"
            "    conn.execute(statement, (1, 'presence_confirmed'))\n",
            ("write",),
        ),
    ),
)
def test_raw_presence_writer_guard_tracks_precise_runtime_selectors(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE presence_decisions "
            "(candidate_id INTEGER, state TEXT)"
        )
        namespace: dict[str, object] = {}
        exec(compile(tree, "<selector-mutation>", "exec"), namespace)
        namespace["write"](conn)  # type: ignore[operator]
        assert conn.execute(
            "SELECT candidate_id, state FROM presence_decisions"
        ).fetchall() == [(1, "presence_confirmed")]
    finally:
        conn.close()

    fake = Path("selector-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/selectors.py"
    )

    assert presence_decision_writer_calls((fake,)) == tuple(
        f"workers/selectors.py:{writer}" for writer in writers
    )


@pytest.mark.parametrize(
    "control",
    (
        (
            "def execute_sql(conn, *sql_args):\n"
            "    return conn.execute(\n"
            "        sql_args[0], sql_args[1]\n"
            "    ).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    return execute_sql(\n"
            "        conn, 'SELECT ?', ('control',),\n"
            "        'INSERT INTO presence_decisions DEFAULT VALUES',\n"
            "    )\n"
        ),
        (
            "def ignore(*args):\n"
            "    return 'control'\n"
            "def inspect(conn):\n"
            "    sinks = (conn.execute,)\n"
            "    sinks = (ignore,)\n"
            "    return sinks[0](\n"
            "        'INSERT INTO presence_decisions DEFAULT VALUES'\n"
            "    )\n"
        ),
        (
            "def dict(mapping):\n"
            "    return {\n"
            "        'conn': mapping['conn'],\n"
            "        'statement': 'SELECT ?',\n"
            "        'values': ('control',),\n"
            "    }\n"
            "def execute_sql(*, conn, statement, values):\n"
            "    return conn.execute(statement, values).fetchone()[0]\n"
            "def inspect(conn):\n"
            "    kwargs = dict({\n"
            "        'conn': conn,\n"
            "        'statement': 'INSERT INTO presence_decisions '"
            "                     'DEFAULT VALUES',\n"
            "    })\n"
            "    return execute_sql(**kwargs)\n"
        ),
        (
            "def inspect(conn):\n"
            "    statement = 'INSERT INTO presence_decisions '"
            "                'DEFAULT VALUES'\n"
            "    statement = 'SELECT ?'\n"
            "    return conn.execute(\n"
            "        statement, ('control',)\n"
            "    ).fetchone()[0]\n"
        ),
    ),
)
def test_raw_presence_writer_guard_ignores_unused_or_overridden_values(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE presence_decisions (id INTEGER)")
        namespace: dict[str, object] = {}
        exec(compile(tree, "<selector-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == "control"  # type: ignore[operator]
        assert conn.execute(
            "SELECT COUNT(*) FROM presence_decisions"
        ).fetchone() == (0,)
    finally:
        conn.close()

    fake = Path("selector-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/selector_control.py"
    )

    assert presence_decision_writer_calls((fake,)) == ()


@pytest.mark.parametrize(
    "control",
    (
        (
            "class Writer:\n"
            "    @staticmethod\n"
            "    def execute_sql(conn):\n"
            "        conn.execute(\n"
            "            'INSERT INTO presence_decisions DEFAULT VALUES'\n"
            "        )\n"
            "class Reader:\n"
            "    @staticmethod\n"
            "    def execute_sql(conn):\n"
            "        return conn.execute('SELECT 1').fetchone()[0]\n"
            "def inspect(conn):\n"
            "    return Reader.execute_sql(conn)\n"
        ),
        (
            "class Base:\n"
            "    @staticmethod\n"
            "    def execute_sql(conn):\n"
            "        conn.execute(\n"
            "            'INSERT INTO presence_decisions DEFAULT VALUES'\n"
            "        )\n"
            "class Reader(Base):\n"
            "    @staticmethod\n"
            "    def execute_sql(conn):\n"
            "        return conn.execute('SELECT 1').fetchone()[0]\n"
            "def inspect(conn):\n"
            "    return Reader.execute_sql(conn)\n"
        ),
    ),
)
def test_raw_presence_writer_guard_respects_effective_helper_receiver(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE presence_decisions (id INTEGER)")
        namespace: dict[str, object] = {}
        exec(compile(tree, "<helper-receiver-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == 1  # type: ignore[operator]
        assert conn.execute(
            "SELECT COUNT(*) FROM presence_decisions"
        ).fetchone() == (0,)
    finally:
        conn.close()

    fake = Path("helper-receiver-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/helper_receiver.py"
    )

    assert presence_decision_writer_calls((fake,)) == (
        "workers/helper_receiver.py:"
        f"{'Writer' if 'class Writer' in control else 'Base'}.execute_sql",
    )


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
    ("mutation", "entrypoint", "writers"),
    (
        (
            "def dispatch(writer, command):\n"
            "    return writer(command)\n"
            "def save(command):\n"
            "    return dispatch(\n"
            "        repository.add_review_and_decision, command\n"
            "    )\n",
            "save",
            ("dispatch", "save"),
        ),
        (
            "def dispatch(*, writer, command):\n"
            "    return writer(command)\n"
            "def save(command):\n"
            "    return dispatch(\n"
            "        writer=repository.add_review_and_decision,\n"
            "        command=command,\n"
            "    )\n",
            "save",
            ("dispatch", "save"),
        ),
        (
            "sm = staticmethod\n"
            "class Writer:\n"
            "    writer = sm(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "import builtins\n"
            "class Writer:\n"
            "    writer = builtins.staticmethod(\n"
            "        repository.add_review_and_decision\n"
            "    )\n"
            "def save(command):\n"
            "    return Writer.writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "from builtins import staticmethod as sm\n"
            "class Writer:\n"
            "    writer = sm(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    @property\n"
            "    def writer(self):\n"
            "        return repository.add_review_and_decision\n"
            "    def save(self, command):\n"
            "        return self.writer(command)\n"
            "def run(command):\n"
            "    return Writer().save(command)\n",
            "run",
            ("Writer.save",),
        ),
        (
            "class Writer:\n"
            "    def __init__(self):\n"
            "        self.writer = repository.add_review_and_decision\n"
            "    def save(self, command):\n"
            "        return self.writer(command)\n"
            "def run(command):\n"
            "    return Writer().save(command)\n",
            "run",
            ("Writer.save",),
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "instance = Writer()\n"
            "instance.writer = repository.add_review_and_decision\n"
            "def save(command):\n"
            "    return instance.writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "setattr(\n"
            "    Writer, 'writer',\n"
            "    staticmethod(repository.add_review_and_decision),\n"
            ")\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "instance = Writer()\n"
            "setattr(\n"
            "    instance, 'writer', repository.add_review_and_decision\n"
            ")\n"
            "def save(command):\n"
            "    return instance.writer(command)\n",
            "save",
            ("save",),
        ),
        (
            "import functools\n"
            "def save(command):\n"
            "    writer = functools.partial(\n"
            "        repository.add_review_and_decision, command\n"
            "    )\n"
            "    return writer()\n",
            "save",
            ("save",),
        ),
        (
            "def save(\n"
            "    command,\n"
            "    writers=dict(review=repository.add_review_and_decision),\n"
            "):\n"
            "    return writers['review'](command)\n",
            "save",
            ("save",),
        ),
        (
            "def save(\n"
            "    command,\n"
            "    writers=(*(repository.add_review_and_decision,),),\n"
            "):\n"
            "    return writers[0](command)\n",
            "save",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer.__dict__['writer'](command)\n",
            "save",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    writer = classmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    descriptor = Writer.__dict__['writer']\n"
            "    return descriptor.__func__(command)\n",
            "save",
            ("save",),
        ),
    ),
)
def test_review_writer_guard_resolves_runtime_callable_dataflow(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    entrypoint: str,
    writers: tuple[str, ...],
) -> None:
    class Repository:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def add_review_and_decision(self, command: str) -> str:
            self.conn.execute(
                "INSERT INTO review_effects(command) VALUES (?)", (command,)
            )
            return command

        @staticmethod
        def describe(command: str) -> str:
            return f"control:{command}"

    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE review_effects (command TEXT NOT NULL)")
        namespace: dict[str, object] = {"repository": Repository(conn)}
        exec(compile(tree, "<review-dataflow-mutation>", "exec"), namespace)
        namespace[entrypoint]("confirm")  # type: ignore[operator]
        assert conn.execute(
            "SELECT command FROM review_effects"
        ).fetchall() == [("confirm",)]
    finally:
        conn.close()

    fake = Path("review-dataflow-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_dataflow.py"
    )

    assert review_writer_callers((fake,)) == tuple(
        f"workers/review_dataflow.py:{writer}" for writer in writers
    )


@pytest.mark.parametrize(
    "control",
    (
        (
            "class Audit:\n"
            "    def add_review_and_decision(self, command):\n"
            "        return repository.describe(command)\n"
            "audit = Audit()\n"
            "def run(command):\n"
            "    return audit.add_review_and_decision(command)\n"
        ),
        (
            "class Base:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "class Reader(Base):\n"
            "    writer = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Reader().writer(command)\n"
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "instance = Writer()\n"
            "instance.writer = repository.describe\n"
            "def run(command):\n"
            "    return instance.writer(command)\n"
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "Writer.writer = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Writer().writer(command)\n"
        ),
        (
            "writer = repository.add_review_and_decision\n"
            "writer = repository.describe\n"
            "def run(command, callback=writer):\n"
            "    return callback(command)\n"
        ),
        (
            "import builtins\n"
            "def staticmethod(callback):\n"
            "    return builtins.staticmethod(repository.describe)\n"
            "class Reader:\n"
            "    reader = staticmethod(\n"
            "        repository.add_review_and_decision\n"
            "    )\n"
            "def run(command):\n"
            "    return Reader().reader(command)\n"
        ),
        (
            "class Reader:\n"
            "    @property\n"
            "    def reader(self):\n"
            "        return repository.describe\n"
            "    def inspect(self, command):\n"
            "        return self.reader(command)\n"
            "def run(command):\n"
            "    return Reader().inspect(command)\n"
        ),
        (
            "class Reader:\n"
            "    def __init__(self):\n"
            "        self.reader = repository.describe\n"
            "    def inspect(self, command):\n"
            "        return self.reader(command)\n"
            "def run(command):\n"
            "    return Reader().inspect(command)\n"
        ),
        (
            "class Reader:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "setattr(Reader, 'writer', staticmethod(repository.describe))\n"
            "def run(command):\n"
            "    return Reader().writer(command)\n"
        ),
        (
            "import functools\n"
            "def run(command):\n"
            "    reader = functools.partial(repository.describe, command)\n"
            "    return reader()\n"
        ),
        (
            "def run(\n"
            "    command, readers=dict(review=repository.describe),\n"
            "):\n"
            "    return readers['review'](command)\n"
        ),
        (
            "def run(\n"
            "    command, readers=(*(repository.describe,),),\n"
            "):\n"
            "    return readers[0](command)\n"
        ),
        (
            "class Reader:\n"
            "    reader = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return Reader.__dict__['reader'](command)\n"
        ),
        (
            "def run(command):\n"
            "    callback = repository.add_review_and_decision\n"
            "    callback = repository.describe\n"
            "    return callback(command)\n"
        ),
    ),
)
def test_review_writer_guard_accepts_effective_runtime_reader_bindings(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    class Repository:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def add_review_and_decision(self, command: str) -> str:
            self.conn.execute(
                "INSERT INTO review_effects(command) VALUES (?)", (command,)
            )
            return command

        @staticmethod
        def describe(command: str) -> str:
            return f"control:{command}"

    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE review_effects (command TEXT NOT NULL)")
        namespace: dict[str, object] = {"repository": Repository(conn)}
        exec(compile(tree, "<review-dataflow-control>", "exec"), namespace)
        assert (
            namespace["run"]("inspect")  # type: ignore[operator]
            == "control:inspect"
        )
        assert conn.execute("SELECT COUNT(*) FROM review_effects").fetchone() == (
            0,
        )
    finally:
        conn.close()

    fake = Path("review-dataflow-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_control.py"
    )

    assert review_writer_callers((fake,)) == ()


@pytest.mark.parametrize(
    ("mutation", "writers"),
    (
        (
            "def invoke(callback, command):\n"
            "    return callback(command)\n"
            "def dispatch(callback, command):\n"
            "    return invoke(callback, command)\n"
            "def save(command):\n"
            "    return dispatch(\n"
            "        repository.add_review_and_decision, command\n"
            "    )\n",
            ("dispatch", "invoke", "save"),
        ),
        (
            "def save(command):\n"
            "    writer = repository.add_review_and_decision\n"
            "    return writer.__call__(command)\n",
            ("save",),
        ),
        (
            "def save(command):\n"
            "    writer = getattr(\n"
            "        repository, 'add_review_and_decision'\n"
            "    )\n"
            "    return writer(command)\n",
            ("save",),
        ),
        (
            "from builtins import setattr as assign\n"
            "class Writer:\n"
            "    pass\n"
            "assign(\n"
            "    Writer, 'writer',\n"
            "    staticmethod(repository.add_review_and_decision),\n"
            ")\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            ("save",),
        ),
        (
            "from functools import partial as bind\n"
            "def save(command):\n"
            "    return bind(\n"
            "        repository.add_review_and_decision, command\n"
            "    )()\n",
            ("save",),
        ),
        (
            "def get_writer(self):\n"
            "    return repository.add_review_and_decision\n"
            "class Writer:\n"
            "    writer = property(get_writer)\n"
            "def save(command):\n"
            "    return Writer().writer(command)\n",
            ("save",),
        ),
        (
            "callback = repository.add_review_and_decision\n"
            "def save(command, writer=callback):\n"
            "    return writer(command)\n"
            "callback = repository.describe\n",
            ("save",),
        ),
        (
            "def save(command):\n"
            "    enabled = True\n"
            "    if enabled:\n"
            "        callback = repository.add_review_and_decision\n"
            "    else:\n"
            "        callback = repository.describe\n"
            "    return callback(command)\n",
            ("save",),
        ),
        (
            "class WriterBase:\n"
            "    writer = staticmethod(\n"
            "        repository.add_review_and_decision\n"
            "    )\n"
            "class ReaderBase:\n"
            "    writer = staticmethod(repository.describe)\n"
            "class Combined(WriterBase, ReaderBase):\n"
            "    pass\n"
            "def save(command):\n"
            "    return Combined().writer(command)\n",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return vars(Writer)['writer'](command)\n",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    return Writer.__dict__.get('writer')(command)\n",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    writer = staticmethod(repository.add_review_and_decision)\n"
            "def save(command):\n"
            "    descriptor = Writer.__dict__['writer']\n"
            "    bound = descriptor.__get__(None, Writer)\n"
            "    return bound(command)\n",
            ("save",),
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "first = Writer()\n"
            "second = Writer()\n"
            "first.writer = repository.add_review_and_decision\n"
            "second.writer = repository.describe\n"
            "def save(command):\n"
            "    return first.writer(command)\n",
            ("save",),
        ),
        (
            "def save(command):\n"
            "    callback = repository.add_review_and_decision\n"
            "    result = callback(command)\n"
            "    callback = repository.describe\n"
            "    return result\n",
            ("save",),
        ),
    ),
)
def test_review_writer_guard_resolves_close_neighbor_callables(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    class Repository:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def add_review_and_decision(self, command: str) -> str:
            self.conn.execute(
                "INSERT INTO review_effects(command) VALUES (?)", (command,)
            )
            return command

        @staticmethod
        def describe(command: str) -> str:
            return f"control:{command}"

    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE review_effects (command TEXT NOT NULL)")
        namespace: dict[str, object] = {"repository": Repository(conn)}
        exec(compile(tree, "<review-neighbor-mutation>", "exec"), namespace)
        namespace["save"]("confirm")  # type: ignore[operator]
        assert conn.execute(
            "SELECT command FROM review_effects"
        ).fetchall() == [("confirm",)]
    finally:
        conn.close()

    fake = Path("review-neighbor-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_neighbor.py"
    )

    assert review_writer_callers((fake,)) == tuple(
        f"workers/review_neighbor.py:{writer}" for writer in writers
    )


@pytest.mark.parametrize(
    "control",
    (
        (
            "def setattr(target, name, value):\n"
            "    target.writer = staticmethod(repository.describe)\n"
            "class Reader:\n"
            "    pass\n"
            "setattr(\n"
            "    Reader, 'writer',\n"
            "    staticmethod(repository.add_review_and_decision),\n"
            ")\n"
            "def run(command):\n"
            "    return Reader().writer(command)\n"
        ),
        (
            "class Writer:\n"
            "    pass\n"
            "first = Writer()\n"
            "second = Writer()\n"
            "first.writer = repository.add_review_and_decision\n"
            "second.writer = repository.describe\n"
            "def run(command):\n"
            "    return second.writer(command)\n"
        ),
        (
            "class WriterBase:\n"
            "    writer = staticmethod(\n"
            "        repository.add_review_and_decision\n"
            "    )\n"
            "class ReaderBase:\n"
            "    writer = staticmethod(repository.describe)\n"
            "class Combined(ReaderBase, WriterBase):\n"
            "    pass\n"
            "def run(command):\n"
            "    return Combined().writer(command)\n"
        ),
        (
            "def partial(callback, command):\n"
            "    return lambda: repository.describe(command)\n"
            "def run(command):\n"
            "    return partial(\n"
            "        repository.add_review_and_decision, command\n"
            "    )()\n"
        ),
        (
            "def run(command):\n"
            "    enabled = True\n"
            "    if enabled:\n"
            "        callback = repository.describe\n"
            "    else:\n"
            "        callback = repository.describe\n"
            "    return callback(command)\n"
        ),
        (
            "class Reader:\n"
            "    reader = staticmethod(repository.describe)\n"
            "def run(command):\n"
            "    return getattr(Reader(), 'reader')(command)\n"
        ),
    ),
)
def test_review_writer_guard_accepts_close_neighbor_readers(
    monkeypatch: pytest.MonkeyPatch,
    control: str,
) -> None:
    class Repository:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def add_review_and_decision(self, command: str) -> str:
            self.conn.execute(
                "INSERT INTO review_effects(command) VALUES (?)", (command,)
            )
            return command

        @staticmethod
        def describe(command: str) -> str:
            return f"control:{command}"

    tree = ast.parse(control)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE review_effects (command TEXT NOT NULL)")
        namespace: dict[str, object] = {"repository": Repository(conn)}
        exec(compile(tree, "<review-neighbor-control>", "exec"), namespace)
        assert (
            namespace["run"]("inspect")  # type: ignore[operator]
            == "control:inspect"
        )
        assert conn.execute("SELECT COUNT(*) FROM review_effects").fetchone() == (
            0,
        )
    finally:
        conn.close()

    fake = Path("review-neighbor-control.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/review_neighbor_control.py"
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
    ("mutation", "writers"),
    (
        (
            "def execute_sql(conn, *sql_args):\n"
            "    conn.execute(*sql_args)\n"
            "def run(conn):\n"
            "    args = ('INSERT INTO analysis_runs(id) VALUES (?)', (1,))\n"
            "    execute_sql(conn, *args)\n",
            ("execute_sql", "run"),
        ),
        (
            "def run(conn):\n"
            "    sinks = {'analysis': conn.execute}\n"
            "    args = ('INSERT INTO analysis_runs(id) VALUES (?)', (1,))\n"
            "    sinks['analysis'](*args)\n",
            ("run",),
        ),
        (
            "def execute_sql(*, conn, statement, values):\n"
            "    conn.execute(statement, values)\n"
            "def run(conn):\n"
            "    left = {'conn': conn}\n"
            "    right = dict(\n"
            "        statement='INSERT INTO analysis_runs(id) VALUES (?)',\n"
            "        values=(1,),\n"
            "    )\n"
            "    execute_sql(**(left | right))\n",
            ("execute_sql", "run"),
        ),
    ),
)
def test_presence_expansion_guard_resolves_runtime_sql_dataflow(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    writers: tuple[str, ...],
) -> None:
    tree = ast.parse(mutation)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE analysis_runs (id INTEGER NOT NULL)")
        namespace: dict[str, object] = {}
        exec(compile(tree, "<downstream-dataflow-mutation>", "exec"), namespace)
        namespace["run"](conn)  # type: ignore[operator]
        assert conn.execute("SELECT id FROM analysis_runs").fetchall() == [(1,)]
    finally:
        conn.close()

    fake = Path("downstream-dataflow-mutation.py")
    monkeypatch.setitem(globals(), "_tree", lambda _path: tree)
    monkeypatch.setitem(
        globals(), "_relative", lambda _path: "workers/downstream_dataflow.py"
    )

    assert presence_expansion_writer_calls((fake,)) == tuple(
        f"workers/downstream_dataflow.py:{writer}" for writer in writers
    )


def test_presence_expansion_guard_honors_runtime_mapping_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "def execute_sql(*, conn, statement, values):\n"
        "    return conn.execute(statement, values).fetchone()[0]\n"
        "def inspect(conn):\n"
        "    left = {\n"
        "        'conn': conn,\n"
        "        'statement': 'INSERT INTO analysis_runs(id) VALUES (?)',\n"
        "    }\n"
        "    right = dict(statement='SELECT ?', values=('control',))\n"
        "    return execute_sql(**(left | right))\n"
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        namespace: dict[str, object] = {}
        exec(compile(tree, "<downstream-dataflow-control>", "exec"), namespace)
        assert namespace["inspect"](conn) == "control"  # type: ignore[operator]
    finally:
        conn.close()

    fake = Path("downstream-dataflow-control.py")
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
