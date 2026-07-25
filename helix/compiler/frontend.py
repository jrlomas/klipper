"""Resolve the initial portable-Python actor subset without executing it."""

import ast
from pathlib import Path

from .model import Callback, IntegerType, ModuleModel, StateField, StateLayout


INTEGER_TYPES = {
    "bool8": IntegerType("bool8", 8, False),
    "u8": IntegerType("u8", 8, False),
    "u16": IntegerType("u16", 16, False),
    "u32": IntegerType("u32", 32, False),
    "u64": IntegerType("u64", 64, False),
    "i8": IntegerType("i8", 8, True),
    "i16": IntegerType("i16", 16, True),
    "i32": IntegerType("i32", 32, True),
    "i64": IntegerType("i64", 64, True),
}

ALLOWED_IMPORTS = {
    "helix.module",
    "helix.types",
}


class CompileError(ValueError):
    def __init__(self, path, node, message):
        line = getattr(node, "lineno", 0)
        column = getattr(node, "col_offset", 0)
        super().__init__("%s:%d:%d: %s" % (path, line, column, message))
        self.path = path
        self.line = line
        self.column = column


def _decorator_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _constant_string(path, node, label):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise CompileError(path, node, "%s must be a string literal" % label)


def _module_metadata(path, cls):
    matches = [item for item in cls.decorator_list
               if _decorator_name(item) == "module"]
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0], ast.Call):
        raise CompileError(path, cls, "@module requires keyword arguments")
    values = {}
    for keyword in matches[0].keywords:
        if keyword.arg is None:
            raise CompileError(
                path, keyword, "@module does not accept **kwargs"
            )
        if keyword.arg not in ("name", "api", "profile"):
            raise CompileError(
                path, keyword, "unknown @module argument %s" % keyword.arg
            )
        values[keyword.arg] = _constant_string(
            path, keyword.value, "@module %s" % keyword.arg
        )
    for required in ("name", "api"):
        if required not in values:
            raise CompileError(
                path, matches[0], "@module requires %s=" % required
            )
    return values["name"], values["api"], values.get("profile", "application")


def _resolve_layouts(path, tree):
    layouts = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        kinds = {_decorator_name(item) for item in node.decorator_list}
        selected = kinds.intersection(("record", "config", "state"))
        if not selected:
            continue
        if len(selected) != 1:
            raise CompileError(path, node, "layout has conflicting decorators")
        fields = []
        for item in node.body:
            if isinstance(item, ast.Pass):
                continue
            if not isinstance(item, ast.AnnAssign) or not isinstance(
                    item.target, ast.Name
            ):
                raise CompileError(
                    path,
                    item,
                    "fixed layouts contain only annotated fields",
                )
            if item.value is not None:
                raise CompileError(
                    path,
                    item,
                    "persistent layout fields are zero-initialized;"
                    " defaults are not yet supported",
                )
            if not isinstance(item.annotation, ast.Name):
                raise CompileError(
                    path, item, "field type must be a fixed type"
                )
            type_spec = INTEGER_TYPES.get(item.annotation.id)
            if type_spec is None:
                raise CompileError(
                    path,
                    item.annotation,
                    "unsupported field type %s" % item.annotation.id,
                )
            fields.append(StateField(item.target.id, type_spec, len(fields)))
        if not fields:
            raise CompileError(
                path, node, "layout must contain at least one field"
            )
        layouts[node.name] = (
            selected.pop(),
            StateLayout(node.name, tuple(fields)),
        )
    return layouts


class _FunctionValidator(ast.NodeVisitor):
    """Reject syntax outside the first deterministic callback slice."""

    ALLOWED = (
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Return,
        ast.Assign,
        ast.AugAssign,
        ast.If,
        ast.Expr,
        ast.Pass,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Attribute,
        ast.Constant,
        ast.Call,
        ast.BinOp,
        ast.UnaryOp,
        ast.Compare,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.BitAnd,
        ast.BitOr,
        ast.BitXor,
        ast.LShift,
        ast.RShift,
        ast.USub,
        ast.Invert,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.keyword,
    )

    def __init__(self, path):
        self.path = path

    def generic_visit(self, node):
        if not isinstance(node, self.ALLOWED):
            raise CompileError(
                self.path,
                node,
                "%s is outside the bounded callback subset"
                % type(node).__name__,
            )
        super().generic_visit(node)


def _resolve_callbacks(path, cls):
    callbacks = []
    for item in cls.body:
        if isinstance(item, (ast.AnnAssign, ast.Pass)):
            continue
        if not isinstance(item, ast.FunctionDef):
            raise CompileError(
                path,
                item,
                "module bodies contain state/config fields and methods",
            )
        callback_decorators = [
            decorator for decorator in item.decorator_list
            if (_decorator_name(decorator) or "").startswith("on_")
        ]
        if not callback_decorators:
            raise CompileError(
                path,
                item,
                "module method %s lacks a callback decorator" % item.name,
            )
        if len(callback_decorators) != 1:
            raise CompileError(
                path, item, "method has multiple callback decorators"
            )
        kind = _decorator_name(callback_decorators[0])[3:]
        if kind != "start":
            raise CompileError(
                path,
                callback_decorators[0],
                "callback @on_%s is not implemented in compiler slice 0.1"
                % kind,
            )
        if len(item.args.args) != 2:
            raise CompileError(
                path, item, "@on_start method signature is (self, ctx)"
            )
        _FunctionValidator(path).visit(item)
        callbacks.append(Callback(kind, item.name, item))
    if not callbacks:
        raise CompileError(path, cls, "module has no callbacks")
    return tuple(callbacks)


def parse_module(path):
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CompileError(path, ast.Module(), str(exc)) from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        node = ast.Constant()
        node.lineno = exc.lineno or 0
        node.col_offset = exc.offset or 0
        raise CompileError(path, node, exc.msg) from exc

    for item in tree.body:
        if isinstance(item, ast.Import):
            raise CompileError(
                path, item, "use explicit 'from helix... import'"
            )
        if isinstance(item, ast.ImportFrom):
            if item.module not in ALLOWED_IMPORTS or any(
                    alias.name == "*" for alias in item.names):
                raise CompileError(
                    path,
                    item,
                    "portable import %r is not allowed" % item.module,
                )
        elif isinstance(item, ast.Expr):
            if not (
                isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                raise CompileError(
                    path,
                    item,
                    "top-level executable expressions are forbidden",
                )
        elif not isinstance(item, ast.ClassDef):
            raise CompileError(
                path,
                item,
                "portable modules contain imports and declarations only",
            )

    layouts = _resolve_layouts(path, tree)
    for item in tree.body:
        if not isinstance(item, ast.ClassDef):
            continue
        if item.name in layouts or _module_metadata(path, item) is not None:
            continue
        raise CompileError(
            path,
            item,
            "class %s must declare @record, @config, @state, or @module"
            % item.name,
        )
    module_classes = []
    for item in tree.body:
        if isinstance(item, ast.ClassDef):
            metadata = _module_metadata(path, item)
            if metadata is not None:
                module_classes.append((item, metadata))
    if len(module_classes) != 1:
        raise CompileError(
            path,
            tree,
            "source must declare exactly one @module class (found %d)"
            % len(module_classes),
        )

    cls, metadata = module_classes[0]
    state_annotations = [
        item for item in cls.body
        if isinstance(item, ast.AnnAssign)
        and isinstance(item.target, ast.Name)
        and item.target.id == "state"
    ]
    if len(state_annotations) != 1:
        raise CompileError(
            path, cls, "module requires exactly one state: annotation"
        )
    annotation = state_annotations[0].annotation
    if not isinstance(annotation, ast.Name) or annotation.id not in layouts:
        raise CompileError(
            path,
            annotation,
            "state annotation must name a @state layout",
        )
    kind, state_layout = layouts[annotation.id]
    if kind != "state":
        raise CompileError(
            path, annotation, "module state must use a @state layout"
        )

    callbacks = _resolve_callbacks(path, cls)
    return ModuleModel(
        source_path=path,
        class_name=cls.name,
        name=metadata[0],
        api=metadata[1],
        profile=metadata[2],
        state=state_layout,
        callbacks=callbacks,
    )
