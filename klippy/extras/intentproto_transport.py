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


class IntentprotoTransport:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.mode = config.getchoice(
            'mode', {'bch': 'bch', 'datagram': 'datagram'})
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
        if self.mode == 'datagram':
            addr = config.get('board_address')
            host, port = addr.rsplit(':', 1)
            listen = config.getint('listen_port', 41414)
            session = config.getboolean('session', False)
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
                session=session, board_id=board_id)
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

    def _connect(self):
        # Capability-driven negotiation for the bch envelope.
        if self._bridge is None or self.mode != 'bch':
            return
        mcu_name = 'mcu' if self.name == 'mcu' else 'mcu ' + self.name
        mcu = self.printer.lookup_object(mcu_name, None)
        if mcu is None:
            logging.warning(
                "intentproto_transport %s: no [%s] section found — leaving"
                " the link in v1 pass-through", self.name, mcu_name)
            return
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

    def get_status(self, eventtime):
        if self._bridge is None:
            return {'mode': self.mode, 'v2_active': False}
        return dict(self._bridge.stats())

    def _disconnect(self):
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
        if self._serial is not None:
            self._serial.close()
            self._serial = None


def load_config_prefix(config):
    return IntentprotoTransport(config)
