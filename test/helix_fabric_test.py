#!/usr/bin/env python3

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from helix_fabric import CanFabricEndpoint


def main():
    endpoint = CanFabricEndpoint('toolheads', 'helixcan0')
    endpoint.add_node('stm32:00112233445566778899aabb')
    endpoint.activate_profile('FD_8M_BRS', 99)
    endpoint.set_owner(77)
    endpoint.update_health({
        'hw_rx_frames': 100, 'host_forwarded_frames': 91,
        'rx_queue_drops': 2, 'rx_queue_depth': 7})
    status = endpoint.status()
    assert status['schema_version'] == 1
    assert status['backend'] == 'af_can'
    assert status['nodes'] == ['stm32:00112233445566778899aabb']
    assert status['conservation']['residual'] == 0
    status['health']['hw_rx_frames'] = 0
    assert endpoint.status()['health']['hw_rx_frames'] == 100
    try:
        endpoint.add_node('001122')
    except ValueError:
        pass
    else:
        raise AssertionError('non-canonical node identity accepted')
    print('helix_fabric_test: PASS')


if __name__ == '__main__':
    main()
