#!/usr/bin/env python3
"""Verify unanimous HELIX CAN profile transaction ordering."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras import helix_can


class ConfigError(Exception):
    pass


class Printer:
    config_error = ConfigError

    def __init__(self):
        self.handlers = {}
        self.events = []
        self.objects = {'gcode': GCode(), 'toolhead': Toolhead()}

    def register_event_handler(self, name, callback):
        self.handlers[name] = callback

    def get_reactor(self):
        return self

    NOW = 0.

    def register_timer(self, callback, waketime):
        return (callback, waketime)

    def send_event(self, name, payload):
        self.events.append((name, payload))

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class GCode:
    def __init__(self):
        self.mux = {}

    def register_mux_command(self, command, key, value, callback, desc=None):
        self.mux[(command, key, value)] = callback


class Toolhead:
    def __init__(self):
        self.waited = False

    def wait_moves(self):
        self.waited = True


class GCmd:
    def __init__(self, params=None):
        self.response = None
        self.params = params or {}

    def get(self, key, default=None):
        return self.params.get(key, default)

    def error(self, message):
        return ConfigError(message)

    def respond_info(self, message):
        self.response = message


class Config:
    error = ConfigError

    def __init__(self):
        self.printer = Printer()

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'helix_can helixcan0'

    def get(self, key, default=None):
        return default

    def getint(self, key, default, **kwargs):
        return default

    def getboolean(self, key, default):
        return default

    def getchoice(self, key, choices, default):
        return choices[default]


class Manager:
    def __init__(self):
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        return {'ok': True}


class Connection:
    def __init__(self, name, log, bitrate_mask=0x0f,
                 transceiver_max=8000000):
        self.name = name
        self.log = log
        self.bitrate_mask = bitrate_mask
        self.transceiver_max = transceiver_max

    def get_mcu(self):
        return self

    def get_name(self):
        return self.name

    def get_can_capabilities(self):
        return {'fd': True, 'bitrate_mask': self.bitrate_mask,
                'max_payload': 64,
                'transceiver_max': self.transceiver_max}

    def prepare_can_profile(self, profile, epoch):
        self.log.append(('prepare', self.name, profile['name'], epoch))

    def commit_can_profile(self, profile, epoch):
        self.log.append(('commit', self.name, profile['name'], epoch))

    def enable_can_profile(self, profile, epoch):
        self.log.append(('enable', self.name, profile['name'], epoch))

    def abort_can_profile(self, epoch):
        self.log.append(('abort', self.name, epoch))


class CANStats:
    def __init__(self, status):
        self.status = status

    def get_status(self, eventtime):
        return dict(self.status)


class Query:
    def __init__(self, calls):
        self.calls = calls

    def send(self, args):
        self.calls.append(tuple(args))
        return {'enabled': bool(args[1]), 'epoch': args[0], 'quality': args[2],
                'sync_count': 0, 'followup_count': 0, 'invalid_count': 0}


class Bridge:
    def __init__(self, calls):
        self.calls = calls

    def lookup_query_command(self, command, response):
        return Query(self.calls)


def main():
    serialqueue = open(os.path.join(
        ROOT, 'klippy', 'chelper', 'serialqueue.c')).read()
    serialhdl = open(os.path.join(ROOT, 'klippy', 'serialhdl.py')).read()
    assert 'errno == ENETDOWN || errno == ENETRESET' in serialqueue
    assert 'pollreactor_do_exit(sq->pr)' in serialqueue
    assert 'RESP_SESSION_RESET' in serialhdl
    assert 'CAN session reset acknowledged' in serialhdl
    assert 'bus.set_filters(filters)' in serialhdl
    assert 'handoff_unaccounted' in open(os.path.join(
        ROOT, 'klippy', 'extras', 'helix_can.py')).read()

    empty = helix_can.HelixCANBus(Config())
    try:
        empty._select_profile()
    except ConfigError as exc:
        assert 'no configured nodes' in str(exc)
    else:
        raise AssertionError('empty HELIX CAN bus selected a profile')

    config = Config()
    bus = helix_can.HelixCANBus(config)
    manager = bus.manager = Manager()
    log = []
    bus.register_connection(Connection('ebb36', log))
    bus.register_connection(Connection('fps', log))
    bus._bootstrap()
    assert manager.requests[-1]['profile'] == 'CLASSIC_1M'
    bus._activate()
    assert bus.active_profile == 'FD_8M_BRS' and bus.state == 'active'
    actions = [entry[0] for entry in log]
    assert actions == ['prepare', 'prepare', 'commit', 'commit',
                       'enable', 'enable']
    assert manager.requests[-1]['profile'] == 'FD_8M_BRS'
    assert config.printer.events[-1][0] == 'helix_can:profile_changed'
    assert ('HELIX_CAN_STATUS', 'BUS', 'helixcan0') in (
        config.printer.objects['gcode'].mux)
    bus.add_required_node('stm32:ebb36')
    bus.add_required_node('stm32:fps')
    bus.bridge_status.update({
        'rx_error': 7, 'tx_error': 2, 'tx_retries': 3,
        'bus_state': 0, 'rx_queue_drops': 0,
        'rx_queue_highwater': 236, 'rx_queue_depth': 0,
        'hw_rx_frames': 100, 'usb_forwarded_frames': 100,
        'handoff_unaccounted': 0})
    config.printer.objects['canbus_stats ebb36'] = CANStats({
        'bus_state': 'active', 'rx_error': 17, 'tx_error': 0,
        'tx_retries': 2, 'rx_fifo_overruns': 17,
        'rx_protocol_errors': 0, 'rx_fifo_highwater': 3})
    gcmd = GCmd()
    bus.cmd_HELIX_CAN_STATUS(gcmd)
    assert "HELIX CAN bus 'helixcan0': ACTIVE" in gcmd.response
    assert 'profile=FD_8M_BRS nominal=1000000 data=8000000' in gcmd.response
    assert 'required_nodes=stm32:ebb36, stm32:fps' in gcmd.response
    assert 'bridge(cumulative): bus=active rx_error=7' in gcmd.response
    assert 'delivery=OK accepted=100 forwarded=100 drops=0' in gcmd.response
    assert 'depth=0 highwater=236 unaccounted=0' in gcmd.response
    assert ('node ebb36: bus=active rx_error=17 tx_error=0 retries=2'
            in gcmd.response)
    assert 'fifo_overruns=17 protocol_errors=0 fifo_highwater=3' in (
        gcmd.response)

    # Current EBB36/FPS transceivers constrain this same protocol to
    # CAN FD without bit-rate switching at 1 Mbit.
    limited = helix_can.HelixCANBus(Config())
    limited.register_connection(Connection('ebb36', [], 0x01, 1000000))
    limited.register_connection(Connection('fps', [], 0x01, 1000000))
    assert limited._select_profile() == 'FD_1M_NOBRS'

    bridge_calls = []
    bus.bridge_mcu = 'canbridge'
    bus.time_epoch = 123
    config.printer.objects['mcu canbridge'] = Bridge(bridge_calls)
    bus.quiesce('test')
    assert bridge_calls == [(123, 0, 0, 0)]
    assert manager.requests[-1]['profile'] == 'CLASSIC_1M'
    assert bus.state == 'maintenance'
    assert [entry[0] for entry in log[-2:]] == ['abort', 'abort']
    bus.state = 'active'
    gcmd = GCmd()
    bus.cmd_HELIX_CAN_QUIESCE(gcmd)
    assert config.printer.objects['toolhead'].waited
    assert bus.state == 'maintenance'
    assert 'stop Klipper' in gcmd.response
    gcmd = GCmd({'PROFILE': 'classic_500k'})
    bus.cmd_HELIX_CAN_QUIESCE(gcmd)
    assert manager.requests[-1]['profile'] == 'CLASSIC_500K'
    assert bus.active_profile == 'CLASSIC_500K'
    assert bus.get_connection_profile()['data_bitrate'] == 500000
    assert bus.get_status(0.)['profile'] == 'CLASSIC_500K'
    assert 'CLASSIC_500K' in gcmd.response
    for name, bitrate in (('CLASSIC_125K', 125000),
                          ('CLASSIC_250K', 250000)):
        bus.active_profile = name
        assert bus.get_connection_profile()['data_bitrate'] == bitrate
    print('PASS: profile prepare/commit/netdevice/enable transaction')


if __name__ == '__main__':
    main()
