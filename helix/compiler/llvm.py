"""Direct LLVM IR lowering for the first stateful actor slice."""

import ast
import shutil
import subprocess
from pathlib import Path

from .frontend import CompileError, INTEGER_TYPES
from .targets import TARGETS


class _FunctionEmitter:
    def __init__(self, model, callback):
        self.model = model
        self.callback = callback
        self.lines = []
        self.counter = 0
        self.terminated = False

    def temporary(self, stem):
        self.counter += 1
        return "%%%s.%d" % (stem, self.counter)

    def emit(self, text):
        self.lines.append("  " + text)

    def state_field(self, node):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "state"
        ):
            raise CompileError(
                self.model.source_path,
                node,
                "only self.state.<field> persistent access is supported",
            )
        try:
            return self.model.state.field(node.attr)
        except KeyError as exc:
            raise CompileError(
                self.model.source_path,
                node,
                "unknown state field %s" % node.attr,
            ) from exc

    def pointer(self, field):
        result = self.temporary(field.name + ".ptr")
        self.emit(
            "%s = getelementptr inbounds %%helix.state, ptr %%state,"
            " i32 0, i32 %d" % (result, field.index)
        )
        return result

    def expression(self, node, expected=None):
        if isinstance(node, ast.Attribute):
            field = self.state_field(node)
            pointer = self.pointer(field)
            result = self.temporary(field.name)
            self.emit(
                "%s = load %s, ptr %s, align %d"
                % (result, field.type.llvm, pointer, field.type.alignment)
            )
            return field.type, result
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            type_spec = expected or INTEGER_TYPES["i32"]
            try:
                type_spec.validate_literal(node.value)
            except OverflowError as exc:
                raise CompileError(
                    self.model.source_path, node, str(exc)
                ) from exc
            return type_spec, str(node.value)
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in INTEGER_TYPES
                and len(node.args) == 1
                and not node.keywords
            ):
                return self.expression(
                    node.args[0], INTEGER_TYPES[node.func.id]
                )
            raise CompileError(
                self.model.source_path,
                node,
                "only fixed-width scalar constructors are supported",
            )
        if isinstance(node, ast.UnaryOp):
            type_spec, operand = self.expression(node.operand, expected)
            if isinstance(node.op, ast.USub):
                result = self.temporary("neg")
                self.emit(
                    "%s = sub %s 0, %s"
                    % (result, type_spec.llvm, operand)
                )
                return type_spec, result
            if isinstance(node.op, ast.Invert):
                result = self.temporary("not")
                self.emit("%s = xor %s %s, -1" % (
                    result, type_spec.llvm, operand))
                return type_spec, result
        if isinstance(node, ast.BinOp):
            left_type, left = self.expression(node.left, expected)
            right_type, right = self.expression(node.right, left_type)
            if left_type != right_type:
                raise CompileError(
                    self.model.source_path,
                    node,
                    "binary operands have different fixed-width types",
                )
            operations = {
                ast.Add: "add",
                ast.Sub: "sub",
                ast.Mult: "mul",
                ast.BitAnd: "and",
                ast.BitOr: "or",
                ast.BitXor: "xor",
                ast.LShift: "shl",
                ast.RShift: "ashr" if left_type.signed else "lshr",
            }
            opcode = operations.get(type(node.op))
            if opcode is None:
                raise CompileError(
                    self.model.source_path, node, "unsupported binary operation"
                )
            result = self.temporary(opcode)
            self.emit(
                "%s = %s %s %s, %s"
                % (result, opcode, left_type.llvm, left, right)
            )
            return left_type, result
        raise CompileError(
            self.model.source_path,
            node,
            "unsupported expression %s" % type(node).__name__,
        )

    def statement(self, node):
        if isinstance(node, ast.Pass):
            return
        if isinstance(node, ast.Return):
            if node.value is not None:
                raise CompileError(
                    self.model.source_path,
                    node,
                    "callbacks currently return None",
                )
            self.emit("ret i32 0")
            self.terminated = True
            return
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise CompileError(
                    self.model.source_path,
                    node,
                    "chained assignment is not supported",
                )
            field = self.state_field(node.targets[0])
            value_type, value = self.expression(node.value, field.type)
            if value_type != field.type:
                raise CompileError(
                    self.model.source_path,
                    node,
                    "assignment to %s requires %s"
                    % (field.name, field.type.name),
                )
            pointer = self.pointer(field)
            self.emit(
                "store %s %s, ptr %s, align %d"
                % (field.type.llvm, value, pointer, field.type.alignment)
            )
            return
        if isinstance(node, ast.AugAssign):
            field = self.state_field(node.target)
            synthetic = ast.BinOp(
                left=node.target, op=node.op, right=node.value
            )
            ast.copy_location(synthetic, node)
            value_type, value = self.expression(synthetic, field.type)
            pointer = self.pointer(field)
            self.emit(
                "store %s %s, ptr %s, align %d"
                % (value_type.llvm, value, pointer, field.type.alignment)
            )
            return
        raise CompileError(
            self.model.source_path,
            node,
            "statement %s is not implemented in compiler slice 0.1"
            % type(node).__name__,
        )

    def render(self):
        symbol = "helix_module_on_%s" % self.callback.kind
        self.lines = [
            "define i32 @%s("
            "ptr nocapture %%state, ptr nocapture readnone %%api)"
            " nounwind {" % symbol,
            "entry:",
        ]
        for statement in self.callback.node.body:
            if self.terminated:
                raise CompileError(
                    self.model.source_path,
                    statement,
                    "statement follows terminal return",
                )
            self.statement(statement)
        if not self.terminated:
            self.emit("ret i32 0")
        self.lines.append("}")
        return "\n".join(self.lines)


def emit_llvm(model, target_name):
    try:
        target = TARGETS[target_name]
    except KeyError as exc:
        raise ValueError("unknown HELIX target %s" % target_name) from exc
    state_types = ", ".join(field.type.llvm for field in model.state.fields)
    callback_ir = "\n\n".join(
        _FunctionEmitter(model, callback).render()
        for callback in model.callbacks
    )
    return "\n".join(
        [
            '; HELIX portable module "%s" source API %s'
            % (model.name, model.api),
            'source_filename = "%s"' % model.source_path.name.replace('"', "'"),
            'target triple = "%s"' % target.triple,
            "",
            "%%helix.state = type { %s }" % state_types,
            "",
            callback_ir,
            "",
            "!helix.module = !{!0}",
            '!0 = !{!"%s", !"%s", !"%s"}'
            % (model.name, model.api, model.profile),
            "",
        ]
    )


def build_object(model, target_name, output, *, llvm_output=None):
    try:
        target = TARGETS[target_name]
    except KeyError as exc:
        raise ValueError("unknown HELIX target %s" % target_name) from exc
    llc = shutil.which("llc")
    if llc is None:
        raise RuntimeError("llc is required to build a native module")
    output = Path(output)
    ir = emit_llvm(model, target_name)
    if llvm_output is not None:
        Path(llvm_output).write_text(ir, encoding="utf-8")
    command = [
        llc,
        "-filetype=obj",
        "-O=2",
        "-mtriple=%s" % target.triple,
        "-mcpu=%s" % target.cpu,
        "-o",
        str(output),
        "-",
    ]
    result = subprocess.run(
        command,
        input=ir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "LLVM object generation failed:\n%s" % result.stderr.rstrip()
        )
    return output
