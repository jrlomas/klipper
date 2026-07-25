"""Fixed-layout source types used by portable HELIX modules.

The host representation performs range checks at construction time.  The
compiler does not substitute Python's unbounded ``int`` for persistent state;
it resolves these marker classes to explicit LLVM integer widths.
"""

from dataclasses import dataclass
import struct


class _FixedInt(int):
    bits = 0
    signed = False

    def __new__(cls, value=0):
        value = int(value)
        minimum, maximum = cls.bounds()
        if value < minimum or value > maximum:
            raise OverflowError(
                "%s value %d is outside [%d, %d]"
                % (cls.__name__, value, minimum, maximum)
            )
        return int.__new__(cls, value)

    @classmethod
    def bounds(cls):
        if cls.signed:
            return -(1 << (cls.bits - 1)), (1 << (cls.bits - 1)) - 1
        return 0, (1 << cls.bits) - 1

    @classmethod
    def wrapping(cls, value):
        """Construct using two's-complement modular arithmetic.

        Portable integer arithmetic is explicitly wrapping.  Boundary
        constructors remain checked so a configuration cannot silently
        truncate, while arithmetic agrees with the emitted LLVM integer
        operations on every target.
        """
        modulus = 1 << cls.bits
        value = int(value) % modulus
        if cls.signed and value >= (1 << (cls.bits - 1)):
            value -= modulus
        return cls(value)

    def _binary(self, other, function):
        if not isinstance(other, type(self)):
            return NotImplemented
        return type(self).wrapping(function(int(self), int(other)))

    def __add__(self, other):
        return self._binary(other, lambda left, right: left + right)

    def __sub__(self, other):
        return self._binary(other, lambda left, right: left - right)

    def __mul__(self, other):
        return self._binary(other, lambda left, right: left * right)

    def __and__(self, other):
        return self._binary(other, lambda left, right: left & right)

    def __or__(self, other):
        return self._binary(other, lambda left, right: left | right)

    def __xor__(self, other):
        return self._binary(other, lambda left, right: left ^ right)

    def __lshift__(self, other):
        if not isinstance(other, _FixedInt):
            return NotImplemented
        return type(self).wrapping(int(self) << int(other))

    def __rshift__(self, other):
        if not isinstance(other, _FixedInt):
            return NotImplemented
        return type(self).wrapping(int(self) >> int(other))

    def __invert__(self):
        return type(self).wrapping(~int(self))

    def __neg__(self):
        return type(self).wrapping(-int(self))


class u8(_FixedInt):
    bits = 8


class u16(_FixedInt):
    bits = 16


class u32(_FixedInt):
    bits = 32


class u64(_FixedInt):
    bits = 64


class i8(_FixedInt):
    bits = 8
    signed = True


class i16(_FixedInt):
    bits = 16
    signed = True


class i32(_FixedInt):
    bits = 32
    signed = True


class i64(_FixedInt):
    bits = 64
    signed = True


class bool8(u8):
    def __new__(cls, value=False):
        value = int(bool(value)) if isinstance(value, bool) else int(value)
        if value not in (0, 1):
            raise ValueError("bool8 accepts only False, True, 0, or 1")
        return int.__new__(cls, value)


class f32(float):
    """IEEE-754 binary32 value, rounded on construction."""

    def __new__(cls, value=0.0):
        rounded = struct.unpack("<f", struct.pack("<f", float(value)))[0]
        return float.__new__(cls, rounded)


class f64(float):
    """IEEE-754 binary64 host representation."""


def record(cls):
    """Declare an immutable fixed-layout value record."""
    result = dataclass(frozen=True)(cls)
    result.__helix_layout_kind__ = "record"
    return result


def config(cls):
    """Declare immutable loader-bound module configuration."""
    result = dataclass(frozen=True)(cls)
    result.__helix_layout_kind__ = "config"
    return result


def state(cls):
    """Declare mutable, module-owned persistent state."""
    result = dataclass(frozen=False)(cls)
    result.__helix_layout_kind__ = "state"
    return result


__all__ = [
    "bool8",
    "u8",
    "u16",
    "u32",
    "u64",
    "i8",
    "i16",
    "i32",
    "i64",
    "f32",
    "f64",
    "record",
    "config",
    "state",
]
