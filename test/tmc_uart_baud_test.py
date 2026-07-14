#!/usr/bin/env python3
"""Regression checks for trajectory-safe TMC software-UART timing."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from klippy.extras import tmc_uart  # noqa: E402


class FakeMCU:
    def __init__(self, name, trajectory=False):
        self.name = name
        self.trajectory = trajectory

    def get_constants(self):
        return {"MCU": self.name}

    def try_lookup_command(self, command):
        assert command == tmc_uart.TRAJECTORY_CONFIG_COMMAND
        return object() if self.trajectory else None


def main():
    assert tmc_uart._default_tmc_uart_baud(
        FakeMCU("atmega2560", trajectory=True)) == 9000
    assert tmc_uart._default_tmc_uart_baud(
        FakeMCU("rp2040", trajectory=False)) == 40000
    assert tmc_uart._default_tmc_uart_baud(
        FakeMCU("rp2040", trajectory=True)) == 9000
    assert tmc_uart._default_tmc_uart_baud(
        FakeMCU("stm32g0b1xx", trajectory=True)) == 9000
    print("PASS: trajectory firmware receives software-UART timing margin")


if __name__ == "__main__":
    main()
