#!/usr/bin/env python3

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from helix_gateway_lab import run_udp_faults


def main():
    result = run_udp_faults(2000)
    assert result['schema_version'] == 1
    assert result['auth_failures'] == result['corrupted']
    # Static datagram replay protection rejects duplicates/reordering before
    # the service dispatcher; the inner runtime therefore cannot re-actuate.
    assert result['transport_reordered'] > 0
    assert result['runtime']['duplicates'] == 0
    assert result['accepted'] > 1000
    print('helix_gateway_loopback_test: PASS', result)


if __name__ == '__main__':
    main()
