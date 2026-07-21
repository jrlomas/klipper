# -*- coding: utf-8 -*-
# HELIX built-in self test: run the boards' live verification gates
# through the protocol (FD-0001; docs/Helix_Test_Plan.md).
#
# The firmware side (src/self_test.c, WANT_SELF_TEST) executes the same
# invariants the desktop suites enforce — wire CRC vector, timer
# monotonicity, RAM pattern, the trajectory fixed-point kernel against
# host golden vectors — live on the silicon. This module drives them:
#
#   [helix_self_test]
#   #on_connect: False     # run the suite automatically at connect
#   #required: False       # a failure blocks startup (with on_connect)
#
# and registers HELIX_SELF_TEST [MCU=<name>] as an on-demand diagnostic.
# The report also measures host<->board round-trip latency, so a green
# run certifies both the board and the link it arrived over.
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import time

_MONOTONIC = getattr(time, 'monotonic', time.time)

STATUS = {0: 'PASS', 1: 'FAIL', 2: 'skip'}


class HelixSelfTest:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.on_connect = config.getboolean('on_connect', False)
        self.required = config.getboolean('required', False)
        self.last_results = {}
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('HELIX_SELF_TEST', self.cmd_HELIX_SELF_TEST,
                               desc=self.cmd_HELIX_SELF_TEST_help)
        gcode.register_command(
            'HELIX_CAN_RX_STRESS', self.cmd_HELIX_CAN_RX_STRESS,
            desc=self.cmd_HELIX_CAN_RX_STRESS_help)
        if self.on_connect:
            self.printer.register_event_handler('klippy:connect',
                                                 self._connect_run)

    # ---- running the suite against one MCU ----
    def _mcu_test_names(self, mcu):
        # Test ids/names ride the dictionary as the 'self_test'
        # enumeration; SELF_TEST_COUNT bounds the id range.
        try:
            consts = mcu.get_constants()
        except Exception:
            return None
        count = consts.get('SELF_TEST_COUNT')
        if not count:
            return None
        names = {}
        try:
            enums = mcu._serial.get_msgparser().get_enumerations()
            for name, val in enums.get('self_test', {}).items():
                names[val] = name
        except Exception:
            pass
        return [(i, names.get(i, 'test_%d' % i)) for i in range(count)]

    def _run_mcu(self, mcu):
        tests = self._mcu_test_names(mcu)
        if tests is None:
            return None
        query = mcu.lookup_query_command(
            "run_self_test id=%c",
            "self_test_result id=%c status=%c value=%u")
        results = []
        for tid, name in tests:
            params = query.send([tid])
            results.append((name, params['status'], params['value']))
        # Link round-trip fingerprint: time a burst of no-op queries.
        uptime = mcu.lookup_query_command("get_uptime",
                                          "uptime high=%u clock=%u")
        t0 = _MONOTONIC()
        n = 8
        for _ in range(n):
            uptime.send([])
        rtt_ms = (_MONOTONIC() - t0) * 1000.0 / n
        return results, rtt_ms

    def _run_all(self, only=None):
        report = {}
        for name, mcu in self.printer.lookup_objects(module='mcu'):
            short = mcu.get_name()
            if only is not None and short != only:
                continue
            try:
                r = self._run_mcu(mcu)
            except Exception:
                logging.exception("helix_self_test: mcu '%s'", short)
                r = None
            report[short] = r
        self.last_results = report
        return report

    @staticmethod
    def _format(report):
        lines = ["HELIX self test", "==============="]
        all_pass = True
        for short, r in sorted(report.items()):
            if r is None:
                lines.append("MCU '%s': self test not built"
                             " (WANT_SELF_TEST)" % (short,))
                continue
            results, rtt_ms = r
            fails = [x for x in results if x[1] == 1]
            all_pass = all_pass and not fails
            lines.append("MCU '%s': %s  (link rtt %.2f ms)"
                         % (short, "FAIL" if fails else "PASS", rtt_ms))
            for name, status, value in results:
                lines.append("    %-18s %-4s (value=%d / 0x%x)"
                             % (name, STATUS.get(status, status),
                                value, value))
        return "\n".join(lines), all_pass

    # ---- entry points ----
    def _connect_run(self):
        report = self._run_all()
        text, all_pass = self._format(report)
        logging.info("%s", text)
        if not all_pass and self.required:
            raise self.printer.config_error(
                "HELIX self test failed at connect:\n" + text)

    cmd_HELIX_SELF_TEST_help = (
        "Run the boards' built-in self tests (live verification gates /"
        " diagnostics)")
    def cmd_HELIX_SELF_TEST(self, gcmd):
        only = gcmd.get('MCU', None)
        report = self._run_all(only)
        if only is not None and not report:
            raise gcmd.error("Unknown MCU '%s'" % (only,))
        text, _ = self._format(report)
        gcmd.respond_info(text)

    cmd_HELIX_CAN_RX_STRESS_help = (
        "Exercise a CAN MCU's partitioned receive FIFOs without I/O")
    def cmd_HELIX_CAN_RX_STRESS(self, gcmd):
        only = gcmd.get('MCU')
        iterations = gcmd.get_int('ITERATIONS', 100, minval=1, maxval=1000)
        hold_us = gcmd.get_int('HOLD_US', 2000, minval=50, maxval=2000)
        matches = [mcu for _, mcu in self.printer.lookup_objects(module='mcu')
                   if mcu.get_name() == only]
        if not matches:
            raise gcmd.error("Unknown MCU '%s'" % (only,))
        mcu = matches[0]
        hold_format = 'self_test_irq_hold duration=%u padding=%*s'
        if mcu.try_lookup_command(hold_format) is None:
            raise gcmd.error("MCU '%s' lacks CAN RX stress support" % (only,))
        hold = mcu.lookup_command(hold_format)
        nop = mcu.lookup_command('self_test_rx_nop padding=%*s')
        uptime = mcu.lookup_query_command('get_uptime',
                                          'uptime high=%u clock=%u')
        duration = mcu.seconds_to_clock(hold_us / 1000000.)
        # Forty-four payload bytes make each record too large to share a
        # 64-byte carrier frame.  Hold plus two nops therefore consume exactly
        # the protocol's three-frame receive window.
        padding = bytes(range(44))
        started = _MONOTONIC()
        for _ in range(iterations):
            hold.send([duration, padding])
            nop.send([padding])
            nop.send([padding])
            # Wait until the three-record burst has executed before repeating;
            # this preserves the bounded protocol credit under test.
            uptime.send([])
        elapsed = _MONOTONIC() - started
        gcmd.respond_info(
            "HELIX CAN RX stress complete: mcu=%s iterations=%d"
            " hold_us=%d elapsed=%.3fs"
            % (only, iterations, hold_us, elapsed))

    def get_status(self, eventtime):
        out = {}
        for short, r in self.last_results.items():
            if r is None:
                out[short] = None
                continue
            results, rtt_ms = r
            out[short] = {
                'passed': all(s != 1 for _, s, _ in results),
                'rtt_ms': round(rtt_ms, 3),
                'tests': {n: STATUS.get(s, s) for n, s, _ in results},
            }
        return out


def load_config(config):
    return HelixSelfTest(config)
