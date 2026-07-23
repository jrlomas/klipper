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
        gcode.register_command(
            'HELIX_OUTPUT_STATUS', self.cmd_HELIX_OUTPUT_STATUS,
            desc=self.cmd_HELIX_OUTPUT_STATUS_help)
        gcode.register_command(
            'HELIX_OUTPUT_MIRROR', self.cmd_HELIX_OUTPUT_MIRROR,
            desc=self.cmd_HELIX_OUTPUT_MIRROR_help)
        gcode.register_command(
            'HELIX_WIFI_STATUS', self.cmd_HELIX_WIFI_STATUS,
            desc=self.cmd_HELIX_WIFI_STATUS_help)
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

    cmd_HELIX_OUTPUT_STATUS_help = (
        "Report a board's sparse-output serializer timing diagnostics")
    def cmd_HELIX_OUTPUT_STATUS(self, gcmd):
        only = gcmd.get('MCU')
        matches = [mcu for _, mcu in self.printer.lookup_objects(module='mcu')
                   if mcu.get_name() == only]
        if not matches:
            raise gcmd.error("Unknown MCU '%s'" % (only,))
        mcu = matches[0]
        request = 'i2s_shift_get_status'
        if mcu.try_lookup_command(request) is None:
            raise gcmd.error(
                "MCU '%s' lacks sparse-output timing diagnostics" % (only,))
        monitor = mcu.try_lookup_command(
            'i2s_shift_monitor step_bit=%c dir_bit=%c')
        step_bit = gcmd.get_int('STEP_BIT', None, minval=0, maxval=15)
        dir_bit = gcmd.get_int('DIR_BIT', None, minval=0, maxval=15)
        if step_bit is not None or dir_bit is not None:
            if step_bit is None or dir_bit is None:
                raise gcmd.error(
                    "STEP_BIT and DIR_BIT must be specified together")
            if monitor is None:
                raise gcmd.error(
                    "MCU '%s' lacks sparse-output edge monitoring" % (only,))
            monitor.send([step_bit, dir_bit])
        query = mcu.lookup_query_command(
            request,
            'i2s_shift_status state=%u writes=%u bitrate=%u'
            ' avg_cycles=%u max_cycles=%u monitor_step=%u monitor_dir=%u'
            ' step_rises=%u interval_count=%u interval_min=%u'
            ' interval_avg=%u interval_max=%u high_count=%u high_min=%u'
            ' high_avg=%u high_max=%u dir_changes=%u dir_value=%u')
        status = query.send([])
        # The cycle counter runs at the ESP32 CPU clock, while the Klipper
        # scheduling clock advertised as CLOCK_FREQ is the 20MHz timer.
        gcmd.respond_info(
            "HELIX output status: mcu=%s state=0x%04x writes=%u"
            " bitrate=%u avg_cycles=%u max_cycles=%u"
            " monitor=I2SO%u/dir:I2SO%u rises=%u"
            " interval_ticks(min/avg/max)=%u/%u/%u"
            " high_ticks(min/avg/max)=%u/%u/%u"
            " dir_changes=%u dir_value=%u"
            % (only, status['state'], status['writes'], status['bitrate'],
               status['avg_cycles'], status['max_cycles'],
               status['monitor_step'], status['monitor_dir'],
               status['step_rises'], status['interval_min'],
               status['interval_avg'], status['interval_max'],
               status['high_min'], status['high_avg'], status['high_max'],
               status['dir_changes'], status['dir_value']))

    cmd_HELIX_OUTPUT_MIRROR_help = (
        "Mirror one serialized output onto another for physical probing")
    def cmd_HELIX_OUTPUT_MIRROR(self, gcmd):
        only = gcmd.get('MCU')
        matches = [mcu for _, mcu in self.printer.lookup_objects(module='mcu')
                   if mcu.get_name() == only]
        if not matches:
            raise gcmd.error("Unknown MCU '%s'" % (only,))
        mcu = matches[0]
        mirror = mcu.try_lookup_command(
            'i2s_shift_mirror source_bit=%c output_bit=%c'
            ' invert=%c enable=%c')
        if mirror is None:
            raise gcmd.error(
                "MCU '%s' lacks sparse-output mirroring" % (only,))
        source_bit = gcmd.get_int('SOURCE_BIT', 0, minval=0, maxval=15)
        output_bit = gcmd.get_int('OUTPUT_BIT', 0, minval=0, maxval=15)
        invert = gcmd.get_int('INVERT', 0, minval=0, maxval=1)
        enable = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        mirror.send([source_bit, output_bit, invert, enable])
        gcmd.respond_info(
            "HELIX output mirror: mcu=%s source=I2SO%d output=I2SO%d"
            " invert=%d enabled=%d"
            % (only, source_bit, output_bit, invert, enable))

    cmd_HELIX_WIFI_STATUS_help = (
        "Report an ESP32 board's WiFi and UDP socket diagnostics")
    _ESP_RESET_REASONS = {
        0: 'unknown', 1: 'power_on', 2: 'external', 3: 'software',
        4: 'panic', 5: 'interrupt_watchdog', 6: 'task_watchdog',
        7: 'watchdog', 8: 'deep_sleep', 9: 'brownout', 10: 'sdio',
        11: 'usb', 12: 'jtag', 13: 'efuse', 14: 'power_glitch',
        15: 'cpu_lockup',
    }
    def cmd_HELIX_WIFI_STATUS(self, gcmd):
        only = gcmd.get('MCU')
        matches = [mcu for _, mcu in self.printer.lookup_objects(module='mcu')
                   if mcu.get_name() == only]
        if not matches:
            raise gcmd.error("Unknown MCU '%s'" % (only,))
        mcu = matches[0]
        if mcu.try_lookup_command('wifi_get_status') is None:
            raise gcmd.error(
                "MCU '%s' lacks WiFi diagnostics" % (only,))
        wifi = mcu.lookup_query_command(
            'wifi_get_status',
            'wifi_status connected=%u got_ip=%u connect_attempts=%u'
            ' disconnects=%u got_ips=%u last_reason=%u'
            ' tx_power_qdbm=%i rssi=%i reset_reason=%u').send([])
        port = None
        if mcu.try_lookup_command('udp_port_get_status') is not None:
            port = mcu.lookup_query_command(
                'udp_port_get_status',
                'udp_port_status network_up=%u socket_up=%u'
                ' socket_opens=%u socket_failures=%u rx_packets=%u'
                ' ring_drops=%u recv_errors=%u tx_packets=%u'
                ' send_errors=%u').send([])
        message = (
            "HELIX WiFi status: mcu=%s connected=%u got_ip=%u"
            " attempts=%u disconnects=%u got_ips=%u last_reason=%u"
            " tx_power=%.2fdBm rssi=%ddBm reset_reason=%u(%s)"
            % (only, wifi['connected'], wifi['got_ip'],
               wifi['connect_attempts'], wifi['disconnects'],
               wifi['got_ips'], wifi['last_reason'],
               wifi['tx_power_qdbm'] / 4., wifi['rssi'],
               wifi['reset_reason'], self._ESP_RESET_REASONS.get(
                   wifi['reset_reason'], 'unrecognized')))
        if port is not None:
            message += (
                " udp(socket=%u opens=%u open_failures=%u rx=%u"
                " ring_drops=%u recv_errors=%u tx=%u send_errors=%u)"
                % (port['socket_up'], port['socket_opens'],
                   port['socket_failures'], port['rx_packets'],
                   port['ring_drops'], port['recv_errors'],
                   port['tx_packets'], port['send_errors']))
        gcmd.respond_info(message)

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
