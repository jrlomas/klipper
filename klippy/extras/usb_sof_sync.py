# USB Start-of-Frame cross-MCU timing commissioning test
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import statistics

try:
    from .machine_time_sync_line import fit_affine_residuals
except ImportError:
    from machine_time_sync_line import fit_affine_residuals


SOF_LATEST = 0xffff


def _lookup_mcu(printer, name):
    return printer.lookup_object('mcu' if name == 'mcu' else 'mcu ' + name)


class UsbSofLink:
    def __init__(self, mcu):
        self.mcu = mcu
        self.name = mcu.get_name()
        self.enable_cmd = self.query_cmd = None

    def setup(self):
        self.enable_cmd = self.mcu.try_lookup_command(
            'usb_sof_enable enable=%c')
        query = 'usb_sof_query frame=%hu'
        response = ('usb_sof_state requested=%hu found=%c frame=%hu'
                    ' clock=%u count=%u')
        if (self.enable_cmd is None
                or self.mcu.try_lookup_command(query) is None
                or not self.mcu.check_valid_response(response)):
            return False
        self.query_cmd = self.mcu.lookup_query_command(query, response)
        return True

    def enable(self, value):
        self.enable_cmd.send([bool(value)])

    def query(self, frame=SOF_LATEST):
        return self.query_cmd.send([frame])


class UsbSofSync:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.primary_name = config.get('primary', 'mcu')
        self.secondary_name = config.get('secondary')
        self.default_samples = config.getint(
            'samples', 50, minval=2, maxval=200)
        self.primary = self.secondary = None
        self.sync_line = None
        self.timesync = None
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('USB_SOF_TEST', self.cmd_USB_SOF_TEST,
                               desc=self.cmd_USB_SOF_TEST_help)

    def _handle_ready(self):
        self.primary = UsbSofLink(
            _lookup_mcu(self.printer, self.primary_name))
        self.secondary = UsbSofLink(
            _lookup_mcu(self.printer, self.secondary_name))
        for link in (self.primary, self.secondary):
            if not link.setup():
                raise self.printer.config_error(
                    "MCU '%s' firmware lacks USB SOF timestamps"
                    % (link.name,))
        self.sync_line = self.printer.lookup_object(
            'machine_time_sync_line', None)
        self.timesync = self.printer.lookup_object('timesync', None)

    def _pause(self, delay):
        waketime = self.reactor.monotonic() + delay
        self.reactor.pause(waketime)

    cmd_USB_SOF_TEST_help = "Compare matching USB SOF timestamps across MCUs"
    def cmd_USB_SOF_TEST(self, gcmd):
        count = gcmd.get_int(
            'SAMPLES', self.default_samples, minval=2, maxval=200)
        calibration = (self.sync_line.get_calibration()
                       if self.sync_line is not None else None)
        if calibration is None:
            raise gcmd.error(
                "Run SYNC_LINE_TEST first to establish the physical clock map")
        if (calibration['primary_mcu'] is not self.primary.mcu
                or calibration['secondary_mcu'] is not self.secondary.mcu):
            raise gcmd.error("SOF MCUs do not match sync-line calibration")
        pairs = []
        rows = []
        last_primary_count = None
        attempts = 0
        if self.timesync is not None:
            self.timesync.set_sof_commissioning(True)
        for link in (self.primary, self.secondary):
            link.enable(True)
        try:
            self._pause(.050)
            while len(pairs) < count and attempts < count * 5:
                attempts += 1
                primary = self.primary.query()
                if not primary['found']:
                    continue
                if primary['count'] == last_primary_count:
                    self._pause(.001)
                    continue
                secondary = self.secondary.query(primary['frame'])
                if not secondary['found']:
                    continue
                last_primary_count = primary['count']
                pclock = self.primary.mcu.clock32_to_clock64(
                    primary['clock'])
                sclock = self.secondary.mcu.clock32_to_clock64(
                    secondary['clock'])
                expected = (calibration['intercept']
                            + calibration['slope'] * pclock)
                phase_us = ((sclock - expected)
                            / calibration['secondary_freq'] * 1.e6)
                pairs.append((pclock, sclock))
                rows.append((len(pairs), primary['frame'], pclock, sclock,
                             phase_us))
        finally:
            for link in (self.primary, self.secondary):
                link.enable(False)
            if self.timesync is not None:
                self.timesync.set_sof_commissioning(False)
        if len(pairs) < count:
            raise gcmd.error("Only matched %d of %d requested SOF frames"
                             % (len(pairs), count))
        _, _, residual_ticks = fit_affine_residuals(pairs)
        residual_us = [
            value / calibration['secondary_freq'] * 1.e6
            for value in residual_ticks]
        phases = [row[4] for row in rows]
        gcmd.respond_info(
            "usb-sof %s->%s samples=%d attempts=%d\n"
            "physical-calibrated phase mean=%+.4fus sigma=%.4fus"
            " range=%+.4f..%+.4fus\n"
            "affine residual sigma=%.4fus peak=%.4fus\n"
            "sample,frame,primary_clock,secondary_clock,phase_us\n%s"
            % (self.primary.name, self.secondary.name, count, attempts,
               statistics.fmean(phases), statistics.pstdev(phases),
               min(phases), max(phases),
               statistics.pstdev(residual_us),
               max(abs(value) for value in residual_us),
               "\n".join("%d,%d,%d,%d,%+.6f" % row for row in rows)))


def load_config(config):
    return UsbSofSync(config)
