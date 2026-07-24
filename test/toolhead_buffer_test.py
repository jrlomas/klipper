#!/usr/bin/env python3
"""Regression tests for transport-aware toolhead queue reserves."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

import toolhead


class FakeTransport:
    def __init__(self, mode):
        self.mode = mode

    def get(self, key, default=None):
        return self.mode if key == 'mode' else default


class FakeConfig:
    def __init__(self, modes=(), values=None):
        self.modes = modes
        self.values = values or {}

    def get_prefix_sections(self, prefix):
        assert prefix == 'intentproto_transport '
        return [FakeTransport(mode) for mode in self.modes]

    def getfloat(self, key, default=None, above=None, minval=None,
                 maxval=None):
        value = float(self.values.get(key, default))
        if above is not None:
            assert value > above
        if minval is not None:
            assert value >= minval
        if maxval is not None:
            assert value <= maxval
        return value


def main():
    assert toolhead._get_buffer_times(FakeConfig()) == (1.0, 0.250)
    assert toolhead._get_buffer_times(
        FakeConfig(('bch',))) == (1.0, 0.250)
    assert toolhead._get_buffer_times(
        FakeConfig(('datagram',))) == (2.0, 1.0)
    assert toolhead._get_buffer_times(
        FakeConfig(('bch', 'datagram'),
                   {'buffer_time_high': 3.0,
                    'buffer_time_start': 1.5})) == (3.0, 1.5)
    print("PASS: datagram transports receive a larger motion reserve")


if __name__ == '__main__':
    main()
