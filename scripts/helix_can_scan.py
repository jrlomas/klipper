#!/usr/bin/env python3
"""Discover HELIX CAN nodes without presenting legacy handles as UUIDs."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))

import canbus_identity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('interface', nargs='?', default='helixcan0')
    parser.add_argument('--timeout', type=float, default=1.0)
    args = parser.parse_args()
    nodes = canbus_identity.scan_bus(args.interface, args.timeout)
    for node in nodes:
        board_id = node['board_id'] or 'unsupported-by-firmware'
        print('board_id=%s legacy_handle=%s application=0x%02x bus=%s'
              % (board_id, node['legacy_uuid'], node['application'],
                 node['interface']))
    print('Total %d nodes found' % (len(nodes),))


if __name__ == '__main__':
    main()
