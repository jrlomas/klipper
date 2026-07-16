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
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if '-json' in argv:
            return Result(stdout=json.dumps([{
                'ifname': 'helixcan0', 'linkinfo': {'info_data': {
                    'bitrate': 1000000, 'data_bitrate': 8000000}}}]))
        return Result()

    manager = manager_module.LinkManager(runner)
    result = manager.apply('helixcan0', 'FD_8M_BRS')
    assert result['ok'] and result['profile'] == 'FD_8M_BRS'
    assert ['ip', 'link', 'set', 'dev', 'helixcan0', 'type', 'can',
            'bitrate', '1000000', 'restart-ms', '100', 'dbitrate',
            '8000000', 'fd', 'on'] in calls
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
    print('PASS: CAN manager uses fixed argv and rolls FD failure back')


if __name__ == '__main__':
    main()
