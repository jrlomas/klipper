#!/usr/bin/env python3

import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from helix_gateway import (GatewayProtocolError, TimeExchange,
                           TIME_SYNC_REQUEST, TIME_SYNC_RESPONSE)
from helix_time import FourTimestampDiscipline, FourTimestampSample


def sample(epoch, host, offset, forward, reverse, service=400):
    t1 = host
    t2 = t1 + forward + offset
    t3 = t2 + service
    t4 = t3 - offset + reverse
    return FourTimestampSample(epoch, t1, t2, t3, t4, quality=1)


def test_codec():
    request = TimeExchange(TIME_SYNC_REQUEST, 7, 100)
    response = TimeExchange(TIME_SYNC_RESPONSE, 7, 100, 130, 140, 2)
    assert TimeExchange.decode(request.encode()) == request
    assert TimeExchange.decode(response.encode()) == response
    try:
        TimeExchange(TIME_SYNC_RESPONSE, 7, 100, 140, 130).encode()
    except GatewayProtocolError:
        pass
    else:
        raise AssertionError('backwards MCU timestamps accepted')


def test_convergence_outliers_and_holdover():
    rng = random.Random(0x505450)
    now = [0]
    discipline = FourTimestampDiscipline(
        min_samples=8, max_delay=500_000, holdover=2_000_000,
        clock=lambda: now[0])
    true_offset = 75_000
    for index in range(40):
        host = 1_000_000_000 + index * 10_000_000
        forward = 45_000 + rng.randrange(-500, 501)
        reverse = 47_000 + rng.randrange(-500, 501)
        now[0] += 100_000
        assert discipline.add(sample(9, host, true_offset,
                                     forward, reverse), now[0])
    status = discipline.status(now[0])
    assert status['state'] == 'converged'
    # Four-timestamp offset contains half the stable path asymmetry.
    assert abs(status['offset'] - (true_offset - 1000)) < 500
    accepted = discipline.accepted
    outlier = sample(9, 1_500_000_000, true_offset + 100_000,
                     45_000, 47_000)
    assert not discipline.add(outlier, now[0] + 1)
    assert discipline.accepted == accepted
    reordered = sample(9, 1_100_000_000, true_offset, 45_000, 47_000)
    assert not discipline.add(reordered, now[0] + 2)
    assert discipline.usable(now[0] + 1_000_000)
    assert not discipline.usable(now[0] + 3_000_000)
    assert discipline.status(now[0] + 3_000_000)['state'] == 'holdover_expired'


def test_drift_and_epoch_reset():
    now = 0
    discipline = FourTimestampDiscipline(min_samples=8, max_delay=200_000,
                                         holdover=5_000_000)
    drift = 25e-6
    base = 2_000_000_000
    for index in range(30):
        host = base + index * 20_000_000
        offset = 20_000 + int(drift * (host - base))
        now += 1000
        assert discipline.add(sample(3, host, offset, 20_000, 20_000), now)
    assert abs(discipline.status(now)['drift_ppm'] - 25.) < .2
    assert discipline.add(sample(4, base + 1_000_000_000, 30_000,
                                 20_000, 20_000), now + 1)
    assert discipline.epoch == 4 and len(discipline.samples) == 1
    assert not discipline.usable(now + 1)


if __name__ == '__main__':
    test_codec()
    test_convergence_outliers_and_holdover()
    test_drift_and_epoch_reset()
    print('helix_time_discipline_test: PASS')
