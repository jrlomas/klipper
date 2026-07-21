#!/usr/bin/env python3

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

fake_mcu = types.ModuleType('mcu')
sys.modules['mcu'] = fake_mcu
from extras import helix_network


class Query:
    def __init__(self, kind):
        self.kind = kind
        self.sent = []
        self.fail = False
        self.raise_error = False

    def send(self, args=None):
        args = args or []
        self.sent.append(list(args))
        if self.raise_error:
            raise OSError('%s reply lost' % self.kind)
        state = {'prepare': 1, 'commit': 2}.get(self.kind, 0)
        epoch = args[0] if args else 17
        return {'result': int(self.fail), 'state': state, 'mode': 1,
                'ip': 0xc0a80164, 'netmask': 0xffffff00,
                'gateway': 0xc0a80101, 'port': 41415, 'epoch': epoch,
                'generation': 3, 'dhcp_state': 3, 'rejected': 0,
                'dhcp_malformed': 2, 'dhcp_naks': 1, 'dhcp_retries': 4}


class MCU:
    def __init__(self):
        self.callback = None
        self.queries = {}

    def register_config_callback(self, callback):
        self.callback = callback

    def alloc_command_queue(self):
        return object()

    def lookup_query_command(self, command, response, cq=None):
        kind = ('prepare' if 'prepare' in command else
                'commit' if 'commit' in command else
                'abort' if 'abort' in command else 'status')
        query = self.queries.setdefault(kind, Query(kind))
        return query


class GCode:
    def __init__(self):
        self.commands = {}

    def register_mux_command(self, command, key, value, callback, desc=None):
        self.commands[(command, key, value)] = callback


class Printer:
    command_error = RuntimeError

    def __init__(self):
        self.mcu = MCU()
        self.gcode = GCode()
        self.events = {}

    def lookup_object(self, name):
        return self.gcode

    def register_event_handler(self, name, callback):
        self.events[name] = callback


class Config:
    error = ValueError

    def __init__(self):
        self.printer = Printer()

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'helix_network gateway'

    def get(self, name, default=None):
        return {'ip': '192.168.1.100', 'netmask': '255.255.255.0',
                'gateway': '192.168.1.1'}.get(name, default)

    def getchoice(self, name, choices, default):
        return choices[default]

    def getint(self, name, default, **kwargs):
        return default

    def getboolean(self, name, default):
        return default


class GCmd:
    def __init__(self):
        self.response = None

    def respond_info(self, response):
        self.response = response

    def get_int(self, name, default, minval=None):
        return default


def main():
    assert helix_network._ipv4('192.168.1.100', 'ip') == 0xc0a80164
    assert helix_network._ipv4_text(0xc0a80164) == '192.168.1.100'
    try:
        helix_network._ipv4('192.168.1', 'ip')
    except ValueError:
        pass
    else:
        raise AssertionError('abbreviated IPv4 address was accepted')

    config = Config()
    fake_mcu.get_printer_mcu = lambda printer, name: printer.mcu
    network = helix_network.HelixNetwork(config)
    config.printer.mcu.callback()
    status = network.apply()
    assert status['schema_version'] == 1 and status['state'] == 'committed'
    assert status['mode'] == 'dhcp' and status['generation'] == 3
    assert status['dhcp_malformed'] == 2 and status['dhcp_retries'] == 4
    assert config.printer.mcu.queries['prepare'].sent[0][1:] == [
        1, 0xc0a80164, 0xffffff00, 0xc0a80101, 41415]
    gcmd = GCmd()
    network.cmd_HELIX_NETWORK_STATUS(gcmd)
    assert "mode=dhcp ip=192.168.1.100" in gcmd.response
    assert ('HELIX_NETWORK_APPLY', 'NETWORK', 'gateway') in (
        config.printer.gcode.commands)

    # A failed commit must issue an abort for the same epoch and leave the
    # previously active address as the module's reported state.
    before = dict(network.last_status)
    config.printer.mcu.queries['commit'].fail = True
    try:
        network.apply()
    except RuntimeError as exc:
        assert 'commit failed' in str(exc)
    else:
        raise AssertionError('failed network commit was accepted')
    abort = config.printer.mcu.queries['abort'].sent[-1]
    failed_epoch = config.printer.mcu.queries['commit'].sent[-1][0]
    assert abort == [failed_epoch]
    assert network.last_status == before

    config.printer.mcu.queries['commit'].fail = False
    config.printer.mcu.queries['commit'].raise_error = True
    try:
        network.apply()
    except RuntimeError as exc:
        assert 'reply lost' in str(exc)
    else:
        raise AssertionError('lost commit reply was accepted')
    lost_epoch = config.printer.mcu.queries['commit'].sent[-1][0]
    assert config.printer.mcu.queries['abort'].sent[-1] == [lost_epoch]
    print('helix_network_test: PASS')


if __name__ == '__main__':
    main()
