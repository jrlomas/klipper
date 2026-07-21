#!/usr/bin/env python3
"""Validate the frozen live USB-gateway compatibility fixture."""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    path = os.path.join(ROOT, 'test', 'fixtures',
                        'helix_gateway_usb.json')
    with open(path, encoding='utf-8') as stream:
        fixture = json.load(stream)
    assert fixture['schema_version'] == 1
    device = fixture['device']
    assert (device['vid'], device['pid']) == (0x1d50, 0x606f)
    assert (device['manufacturer'], device['product']) == (
        'OpenAMS', 'Helix CAN-FD Bridge')
    assert device['interfaces'] == 3
    assert device['interface_layout'] == [
        {'number': 0, 'class': 255, 'endpoints': [2, 129]},
        {'number': 1, 'class': 2, 'endpoints': [131]},
        {'number': 2, 'class': 10, 'endpoints': [4, 133]}]
    can = fixture['socketcan']
    assert can['mtu'] == 72 and can['fd']
    assert can['nominal_bitrate'] == 1_000_000
    assert can['data_bitrate'] == 8_000_000
    assert can['controller_clock'] == 80_000_000
    assert not any(can['error_counters'].values())
    status = fixture['gateway_status']
    assert status['schema_version'] == 1
    assert not any(value for key, value in status.items()
                   if key != 'schema_version')
    print('helix_gateway_golden_test: PASS')


if __name__ == '__main__':
    main()
