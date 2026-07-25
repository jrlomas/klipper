"""Typed compiler model between Python AST resolution and LLVM emission."""

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class IntegerType:
    name: str
    bits: int
    signed: bool

    @property
    def llvm(self):
        return "i%d" % self.bits

    @property
    def alignment(self):
        return max(1, min(self.bits // 8, 8))

    @property
    def bounds(self):
        if self.signed:
            return -(1 << (self.bits - 1)), (1 << (self.bits - 1)) - 1
        return 0, (1 << self.bits) - 1

    def validate_literal(self, value):
        minimum, maximum = self.bounds
        if value < minimum or value > maximum:
            raise OverflowError(
                "%s literal %d is outside [%d, %d]"
                % (self.name, value, minimum, maximum)
            )


@dataclass(frozen=True)
class StateField:
    name: str
    type: IntegerType
    index: int


@dataclass(frozen=True)
class StateLayout:
    name: str
    fields: Tuple[StateField, ...]

    def field(self, name):
        for item in self.fields:
            if item.name == name:
                return item
        raise KeyError(name)


@dataclass(frozen=True)
class Callback:
    kind: str
    method_name: str
    node: object


@dataclass(frozen=True)
class ModuleModel:
    source_path: Path
    class_name: str
    name: str
    api: str
    profile: str
    state: StateLayout
    callbacks: Tuple[Callback, ...]
