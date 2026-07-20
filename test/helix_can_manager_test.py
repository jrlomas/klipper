#!/usr/bin/env python3
"""Test the constrained CAN manager argv and rollback contract."""

import importlib.util
import json
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    'helix_can_manager', os.path.join(ROOT, 'scripts',
                                     'helix_can_manager.py'))
manager_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager_module)


class Result:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def main():
    bridge = open(os.path.join(ROOT, 'src', 'generic',
                              'usb_canbus.c')).read()
    fdcan = open(os.path.join(ROOT, 'src', 'stm32', 'fdcan.c')).read()
    assert 'canhw_set_nominal_bitrate(nominal_bitrate)' in bridge
    assert 'ActiveNominalBitrate = bitrate' in fdcan

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if '-json' in argv:
            return Result(stdout=json.dumps([{
                'ifname': 'helixcan0', 'mtu': 72,
                'linkinfo': {'info_data': {
                    'ctrlmode': ['FD'],
                    'bittiming': {'bitrate': 1000000},
                    'data_bittiming': {'bitrate': 8000000}}}}]))
        return Result()

    manager = manager_module.LinkManager(runner)
    result = manager.apply('helixcan0', 'FD_8M_BRS')
    assert result['ok'] and result['profile'] == 'FD_8M_BRS'
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'type', 'can',
            'bitrate', '1000000', 'dbitrate', '8000000', 'fd', 'on'] in calls
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'type', 'can',
            'restart-ms', '100'] in calls
    assert result['automatic_restart'] is True

    maintenance_calls = []
    def maintenance_runner(argv, **kwargs):
        maintenance_calls.append(argv)
        if '-json' in argv:
            return Result(stdout=json.dumps([{
                'ifname': 'helixcan0', 'mtu': 16,
                'linkinfo': {'info_data': {
                    'ctrlmode': [],
                    'bittiming': {'bitrate': 500000}}}}]))
        return Result()
    result = manager_module.LinkManager(maintenance_runner).apply(
        'helixcan0', 'CLASSIC_500K')
    assert result['ok'] and result['profile'] == 'CLASSIC_500K'
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'type', 'can',
            'bitrate', '500000', 'fd', 'off'] \
        in maintenance_calls
    assert manager_module.PROFILES['CLASSIC_125K']['nominal'] == 125000
    assert manager_module.PROFILES['CLASSIC_250K']['nominal'] == 250000
    try:
        manager.apply('../../bad', 'FD_8M_BRS')
    except manager_module.ManagerError:
        pass
    else:
        raise AssertionError('unsafe interface name accepted')

    failed = []

    def failing_runner(argv, **kwargs):
        failed.append(argv)
        if '8000000' in argv:
            return Result(1, stderr='unsupported')
        if '-json' in argv:
            return Result(stdout='[]')
        return Result()

    try:
        manager_module.LinkManager(failing_runner).apply(
            'helixcan0', 'FD_8M_BRS')
    except manager_module.ManagerError:
        pass
    else:
        raise AssertionError('failed profile was accepted')
    assert any(command[-2:] == ['fd', 'off'] for command in failed)

    no_restart_calls = []
    def no_restart_runner(argv, **kwargs):
        no_restart_calls.append(argv)
        if argv[-2:] == ['restart-ms', '100']:
            return Result(95, stderr='Operation not supported')
        if '-json' in argv:
            return Result(stdout=json.dumps([{
                'ifname': 'helixcan0', 'mtu': 16,
                'linkinfo': {'info_data': {
                    'ctrlmode': [],
                    'bittiming': {'bitrate': 1000000}}}}]))
        return Result()
    result = manager_module.LinkManager(no_restart_runner).apply(
        'helixcan0', 'CLASSIC_1M')
    assert result['ok'] and result['automatic_restart'] is False
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'type', 'can',
            'bitrate', '1000000', 'fd', 'off'] in no_restart_calls
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'up'] \
        in no_restart_calls
    print('PASS: CAN manager uses fixed argv and rolls FD failure back')


if __name__ == '__main__':
    main()
