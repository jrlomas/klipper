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
        self.objects = {}

    def register_event_handler(self, name, callback):
        self.handlers[name] = callback

    def send_event(self, name, payload):
        self.events.append((name, payload))

    def lookup_object(self, name):
        return self.objects[name]


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
    print('PASS: profile prepare/commit/netdevice/enable transaction')


if __name__ == '__main__':
    main()
