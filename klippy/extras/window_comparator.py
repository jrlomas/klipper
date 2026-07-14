# Window Comparator support for STM32G0B1
#
# Copyright (C) 2025 JR Lomas (discord:knight_rad.iant) <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import mcu


class WindowComparator:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.reactor = self.printer.get_reactor()
        self.mcu_name = config.get('mcu', 'mcu')
        self.mcu = mcu.get_printer_mcu(self.printer, self.mcu_name)

        # Configuration parameters
        self.pin = config.get('pin')  # PA1 or PA3 for the STM32G0B1
        self.lower_threshold = int(config.getfloat(
            'lower_threshold', 0.25, minval=0, maxval=1) * 4095)
        self.upper_threshold = int(config.getfloat(
            'upper_threshold', 0.75, minval=0, maxval=1) * 4095)

        # Validate thresholds
        if self.upper_threshold <= self.lower_threshold:
            raise config.error(
                "upper_threshold must be greater than lower_threshold")

        # Internal state
        self.oids = None
        self.last_state = None
        self.callbacks = []

        # Event tracking
        self._last_upper_trigger_time = 0.0
        self._last_lower_trigger_time = 0.0

        # Register response handlers with OID
        self.mcu.register_serial_response(
            self._handle_comp_upper_trigger, "comp_upper_trigger")
        self.mcu.register_serial_response(
            self._handle_comp_lower_trigger, "comp_lower_trigger")

        # Register with MCU - moved to build_config
        self.mcu.register_config_callback(self._build_config)
        gcode = self.printer.lookup_object('gcode')

        # Register commands
        gcode.register_command('COMP_QUERY_STATE', self.cmd_COMP_QUERY_STATE,
                               desc=self.cmd_COMP_QUERY_STATE_help)

        # Register with printer
        self.printer.add_object("comp " + self.name, self)

        logging.info(
            "Window comparator '%s' configured: pin=%s, upper=%d, lower=%d",
            self.name, self.pin, self.upper_threshold, self.lower_threshold)

    cmd_COMP_QUERY_STATE_help = (
        "Query comparator state: COMP_QUERY_STATE COMP=<name>")

    def cmd_COMP_QUERY_STATE(self, gcmd):
        comp_name = gcmd.get('COMP')

        comp_obj = self.printer.lookup_object('comp ' + comp_name, None)
        if comp_obj is None:
            raise gcmd.error("Unknown comparator '%s'" % comp_name)

        # Trigger state query
        comp_obj.query_state()

        # Give a short delay for the response
        reactor = self.printer.get_reactor()
        eventtime = reactor.pause(reactor.monotonic() + 0.010)  # 10ms delay

        # Get status
        status = comp_obj.get_status(eventtime)

        response = "Comparator '%s': pin=%s, thresholds=%d/%d" % (
            comp_name, status['pin'], status['lower_threshold'],
            status['upper_threshold'])

        if 'in_window' in status:
            response += ", in_window=%s, upper_out=%s, lower_out=%s" % (
                status['in_window'], status['upper_out'], status['lower_out'])

        gcmd.respond_info(response)

    def _build_config(self):
        """Configure the window comparator on the MCU"""
        self.oid = self.mcu.create_oid()
        self.mcu.add_config_cmd(
            "config_comp oid=%d pin=%s upper=%d lower=%d" % (
                self.oid, self.pin, self.upper_threshold, self.lower_threshold))
        self.mcu.register_serial_response(
            self._handle_comp_state, "comp_state", self.oid)

    def _handle_comp_upper_trigger(self, params):
        """Handle upper threshold exceeded trigger"""
        pin = params['pin']

        current_time = self.reactor.monotonic()
        self._last_upper_trigger_time = current_time

        logging.info(
            "Window comparator '%s' upper threshold exceeded on pin %s",
            self.name, pin)

        # Call registered callbacks
        for callback in self.callbacks:
            try:
                callback(current_time, 'upper', pin)
            except Exception as e:
                logging.exception("Error in comp callback: %s", e)

    def _handle_comp_lower_trigger(self, params):
        """Handle lower threshold exceeded trigger"""
        pin = params['pin']

        current_time = self.reactor.monotonic()
        self._last_lower_trigger_time = current_time

        logging.info(
            "Window comparator '%s' lower threshold exceeded on pin %s",
            self.name, pin)

        # Call registered callbacks
        for callback in self.callbacks:
            try:
                callback(current_time, 'lower', pin)
            except Exception as e:
                logging.exception("Error in comp callback: %s", e)

    def _handle_comp_state(self, params):
        """Handle comparator state query response"""
        oid = params['oid']

        if oid != self.oid:
            return

        pin = params['pin']
        in_window = bool(params['in_window'])
        upper_out = bool(params['upper_out'])
        lower_out = bool(params['lower_out'])

        self.last_state = {
            'pin': pin,
            'in_window': in_window,
            'upper_out': upper_out,
            'lower_out': lower_out,
            'timestamp': self.reactor.monotonic()
        }

        logging.debug(
            "Comp state: pin=%s, in_window=%s, upper_out=%s, lower_out=%s",
            pin, in_window, upper_out, lower_out)

    def add_callback(self, callback):
        """Add a callback for comparator events

        Callback signature: callback(eventtime, trigger_type, pin)
        trigger_type is either 'upper' or 'lower'
        """
        self.callbacks.append(callback)

    def remove_callback(self, callback):
        """Remove a callback"""
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def set_irq_enable(self, upper_enable=True, lower_enable=True):
        """IRQ management removed - fixed edge triggering enabled"""
        raise self.printer.command_error(
            "IRQ management removed - fixed edge triggering enabled")

    def reset_irq(self):
        """IRQ management removed - fixed edge triggering enabled"""
        raise self.printer.command_error(
            "IRQ management removed - fixed edge triggering enabled")

    def query_state(self):
        """Query current comparator state

        Returns the last received state or triggers a new query.
        The response will be available in self.last_state after a short delay.
        """
        if self.oid is None:
            raise self.printer.command_error("Comparator not configured")

        cmd = self.mcu.lookup_command("comp_query_state oid=%c")
        cmd.send([self.oid])

        return self.last_state

    def get_status(self, eventtime):
        """Get status for STATUS command"""
        status = {
            'pin': self.pin,
            'upper_threshold': self.upper_threshold,
            'lower_threshold': self.lower_threshold,
            'last_upper_trigger': self._last_upper_trigger_time,
            'last_lower_trigger': self._last_lower_trigger_time,
        }

        if self.last_state:
            status.update({
                'in_window': self.last_state['in_window'],
                'upper_out': self.last_state['upper_out'],
                'lower_out': self.last_state['lower_out'],
                'state_timestamp': self.last_state['timestamp'],
            })

        return status


def load_config_prefix(config):
    """Load window comparator configuration"""
    return WindowComparator(config)
