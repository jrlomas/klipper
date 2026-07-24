# -*- coding: utf-8 -*-
# klippy config glue for the intentproto v2 transport bridge.
#
# Lets klippy speak intentproto v2 to an MCU by re-framing its stock v1
# serial stream, without touching serialqueue/serialhdl/msgproto. A
# [intentproto_transport NAME] section starts an in-process bridge that
# publishes a PTY; point [mcu NAME] serial: at that PTY.
#
#   [intentproto_transport toolhead]
#   mode: datagram                 # or: bch
#   board_address: 192.168.1.50:41414   # datagram mode
#   listen_port: 41414                  # datagram mode
#   psk_file: ~/printer_data/config/toolhead.psk
#   fec_k: 2                         # pair-block FEC; 0 disables
#   session: True                       # datagram mode: DTLS-class session
#   board_id: helix-board               # identity the board MUST present
#                                       # (handshake rejected on mismatch)
#   # bch mode instead:
#   #   device: /dev/ttyACM0
#   #   baud: 250000
#
#   [mcu toolhead]
#   serial: /tmp/intentproto-toolhead
#
# Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import intentproto_transport as ipt


UDP_CONSOLE_STATUS_V1 = (
    'udp_console_status decoded=%u responses=%u response_drops=%u'
    ' flushes=%u no_peer_drops=%u send_failures=%u'
    ' session_tx_epoch=%u session_tx_seq=%u'
    ' session_rx_epoch=%u session_rx_top=%u'
    ' session_auth_failures=%u session_replays=%u'
    ' session_old_epoch=%u')
UDP_CONSOLE_STATUS_V2 = (
    UDP_CONSOLE_STATUS_V1 + ' session_tx_copies=%u session_redundant_tx=%u')
DATAGRAM_MULTI_MCU_HOMING_TIMEOUT = .250
ETH_MAC_STATUS_F7 = (
    'eth_mac_status ready=%c init_error=%c link=%c speed100=%c'
    ' full_duplex=%c phy_addr=%c phy_id1=%hu phy_id2=%hu'
    ' transitions=%u irq=%u rx=%u rx_errors=%u tx=%u tx_errors=%u'
    ' tx_busy=%u tx_underflows=%u'
    ' udp_rx=%u udp_slot_drops=%u udp_queue_depth=%c'
    ' udp_queue_highwater=%c overruns=%u'
    ' dma_errors=%u mdio_errors=%u'
    ' ready_highwater=%c dma_pool=%hu dma_used=%hu')
ETH_MAC_STATUS_H7 = (
    'eth_mac_status ready=%c link=%c rx=%u tx=%u'
    ' udp_rx=%u udp_slot_drops=%u udp_queue_depth=%c'
    ' udp_queue_highwater=%c overruns=%u'
    ' dma_errors=%u tx_errors=%u ready_highwater=%c'
    ' dma_pool=%hu dma_used=%hu')


class IntentprotoTransport:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.mode = config.getchoice(
            'mode', {'bch': 'bch', 'datagram': 'datagram'})
        self.send_ahead = config.getfloat(
            'send_ahead', 1.0 if self.mode == 'datagram' else .100,
            minval=.100, maxval=30.0)
        self.multi_mcu_homing_timeout = config.getfloat(
            'multi_mcu_homing_timeout',
            DATAGRAM_MULTI_MCU_HOMING_TIMEOUT
            if self.mode == 'datagram' else .025,
            minval=.025, maxval=.250)
        self.urgent_rto = config.getfloat(
            'urgent_rto', .025, minval=.025, maxval=1.)
        self.buffered_rto = config.getfloat(
            'buffered_rto', .100, minval=.025, maxval=1.)
        if self.buffered_rto < self.urgent_rto:
            raise config.error(
                "buffered_rto must be greater than or equal to urgent_rto")
        self.retry_deadline_margin = config.getfloat(
            'retry_deadline_margin', .100, minval=0., maxval=5.)
        default_pty = '/tmp/intentproto-%s' % (self.name,)
        self.pty_link = config.get('pty', default_pty)
        # Authentication: a PSK file, or the explicit trust-network confession.
        psk = None
        psk_file = config.get('psk_file', None)
        if psk_file is not None:
            with open(os.path.expanduser(psk_file), 'rb') as f:
                psk = f.read().strip()
            if not psk:
                raise config.error("empty psk_file for %s" % (self.name,))
        elif not config.getboolean('trust_network', False):
            raise config.error(
                "intentproto_transport %s: authentication is mandatory — set"
                " psk_file, or trust_network: True for an isolated segment"
                % (self.name,))
        self.fec_k = config.getint('fec_k', 0, minval=0)
        if self.fec_k not in (0, 2):
            raise config.error(
                "intentproto_transport %s: fec_k must be 0 or 2"
                % (self.name,))
        self._serial = None
        self._bridge = None
        self._udp_status_cmd = None
        self._eth_status_cmd = None
        self._mcu_diagnostics = {}
        if self.mode == 'datagram':
            addr = config.get('board_address')
            host, port = addr.rsplit(':', 1)
            listen = config.getint('listen_port', 41414)
            session = config.getboolean('session', False)
            session_tx_copies = config.getint(
                'session_tx_copies', 2 if session else 1,
                minval=1, maxval=3)
            board_id = config.get('board_id', '').encode()
            if session and psk is None:
                raise config.error(
                    "intentproto_transport %s: session: True requires a"
                    " psk_file (the session is keyed from the PSK)"
                    % (self.name,))
            if session and not board_id:
                raise config.error(
                    "intentproto_transport %s: session: True requires an"
                    " expected board_id" % (self.name,))
            self._bridge = ipt.TransportBridge(
                'datagram', self.pty_link, psk=psk, fec_k=self.fec_k,
                udp_board=(host, int(port)), udp_listen=listen,
                session=session, board_id=board_id,
                session_tx_copies=session_tx_copies)
        else:  # bch — a real serial device to the MCU
            device = config.get('device')
            baud = config.getint('baud', 250000)
            import serial
            self._serial = serial.Serial(device, baud, timeout=0,
                                         exclusive=True)
            self._bridge = ipt.TransportBridge(
                'bch', self.pty_link, stream_wire_fd=self._serial.fileno())
        # Open early (at config load) so the PTY exists before [mcu] connects.
        # A bch bridge starts in v1 pass-through; the identify happens in
        # plain v1 and _connect() upgrades once the dictionary confirms
        # FRAMING_V2 (the MCU console accepts both framings at all times).
        self._bridge.open()
        logging.info("intentproto_transport %s: %s bridge on %s%s",
                     self.name, self.mode, self.pty_link,
                     " (v1 pass-through until negotiated)"
                     if self.mode == 'bch' else "")
        self.printer.register_event_handler('klippy:connect', self._connect)
        self.printer.register_event_handler('klippy:disconnect',
                                             self._disconnect)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            'HELIX_DATAGRAM_STATUS', 'TRANSPORT', self.name,
            self.cmd_HELIX_DATAGRAM_STATUS,
            desc='Query Helix datagram session and Ethernet drop counters')

    def _connect(self):
        if self._bridge is None:
            return
        mcu_name = 'mcu' if self.name == 'mcu' else 'mcu ' + self.name
        mcu = self.printer.lookup_object(mcu_name, None)
        if mcu is None:
            logging.warning(
                "intentproto_transport %s: no [%s] section found — leaving"
                " the link in v1 pass-through", self.name, mcu_name)
            return
        if self.mode == 'datagram':
            # Klipper's serialqueue normally releases reqclock-tagged
            # commands only 100ms before their MCU deadline.  A datagram ARQ
            # can legitimately back off longer than that while the toolhead
            # still has seconds of planned motion.  Release this link's
            # commands early enough for the configured network buffer to be
            # physically staged instead of merely present in lookahead.
            self._configure_datagram_serial(mcu)
            self._lookup_datagram_diagnostics(mcu)
            return
        # Capability-driven negotiation for the bch envelope.
        try:
            consts = mcu.get_constants()
        except Exception:
            consts = {}
        if consts.get('FRAMING_V2'):
            self._bridge.enable_v2()
            logging.info("intentproto_transport %s: board advertises"
                         " FRAMING_V2 — envelope upgraded to v2 (BCH)",
                         self.name)
        else:
            logging.warning(
                "intentproto_transport %s: board does not advertise"
                " FRAMING_V2 (stock firmware, or WANT_CONSOLE_FRAMING_V2"
                " not built) — staying in plain v1 pass-through", self.name)

    def _configure_datagram_serial(self, mcu):
        mcu.set_serial_send_ahead(self.send_ahead)
        mcu.set_serial_retransmit_policy(
            self.urgent_rto, self.buffered_rto,
            self.retry_deadline_margin)
        mcu.set_transport_homing_timeout(self.multi_mcu_homing_timeout)
        logging.info(
            "intentproto_transport %s: datagram retry policy urgent=%.1fms"
            " buffered=%.1fms deadline_margin=%.1fms;"
            " multi-MCU homing liveness floor %.1fms",
            self.name, self.urgent_rto * 1000.,
            self.buffered_rto * 1000.,
            self.retry_deadline_margin * 1000.,
            self.multi_mcu_homing_timeout * 1000.)

    def _lookup_datagram_diagnostics(self, mcu):
        if mcu.try_lookup_command('udp_console_get_status') is not None:
            if mcu.check_valid_response(UDP_CONSOLE_STATUS_V2):
                response = UDP_CONSOLE_STATUS_V2
            elif mcu.check_valid_response(UDP_CONSOLE_STATUS_V1):
                response = UDP_CONSOLE_STATUS_V1
            else:
                logging.warning(
                    "intentproto_transport %s: udp_console_get_status has"
                    " an unrecognized response schema", self.name)
                response = None
            if response is not None:
                self._udp_status_cmd = mcu.lookup_query_command(
                    'udp_console_get_status', response)
        if mcu.try_lookup_command('eth_mac_get_status') is None:
            return
        if mcu.check_valid_response(ETH_MAC_STATUS_F7):
            response = ETH_MAC_STATUS_F7
        elif mcu.check_valid_response(ETH_MAC_STATUS_H7):
            response = ETH_MAC_STATUS_H7
        else:
            logging.warning(
                "intentproto_transport %s: eth_mac_get_status has an"
                " unrecognized response schema", self.name)
            return
        self._eth_status_cmd = mcu.lookup_query_command(
            'eth_mac_get_status', response)

    @staticmethod
    def _format_diagnostics(label, params):
        return "%s: %s" % (
            label, " ".join("%s=%s" % item
                            for item in sorted(params.items())))

    def cmd_HELIX_DATAGRAM_STATUS(self, gcmd):
        if self.mode != 'datagram':
            raise gcmd.error(
                "intentproto_transport %s is not a datagram transport"
                % self.name)
        diagnostics = {}
        bridge = getattr(self, '_bridge', None)
        if bridge is not None:
            diagnostics['bridge'] = bridge.stats()
        if self._udp_status_cmd is not None:
            diagnostics['console'] = dict(self._udp_status_cmd.send())
        if self._eth_status_cmd is not None:
            diagnostics['ethernet'] = dict(self._eth_status_cmd.send())
        if not diagnostics:
            raise gcmd.error(
                "intentproto_transport %s exposes no datagram diagnostics"
                % self.name)
        self._mcu_diagnostics = diagnostics
        for label, params in sorted(diagnostics.items()):
            gcmd.respond_info(self._format_diagnostics(label, params))

    def get_status(self, eventtime):
        if self._bridge is None:
            return {'mode': self.mode, 'v2_active': False}
        status = dict(self._bridge.stats())
        status['mcu_diagnostics'] = dict(self._mcu_diagnostics)
        return status

    def _disconnect(self):
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None


def load_config_prefix(config):
    return IntentprotoTransport(config)
