#!/usr/bin/env python3
"""Standalone regression tests for Helix heater characterization models."""

import json, os, stat, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'klippy'))

from extras import heater_profiles


def run(target, kp, ki, kd, status='candidate', context=None):
    return {'target': target, 'context_temp': context, 'status': status,
            'gains': {'kp': kp, 'ki': ki, 'kd': kd},
            'method': 'test'}


def test_private_atomic_store():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'profiles.json')
        store = heater_profiles.HeaterProfileStore(
            path, wall_clock=lambda: 1234.)
        first = store.add_run('extruder', run(200, 20, 1, 80))
        assert first['status'] == 'candidate' and first['created'] == 1234.
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        # Content-addressed run IDs make a retry idempotent.
        store.add_run('extruder', dict(first))
        assert len(store.runs('extruder')) == 1
        store.set_status('extruder', first['id'], 'validated')
        reopened = heater_profiles.HeaterProfileStore(path)
        assert reopened.runs('extruder')[0]['status'] == 'validated'
        assert not [name for name in os.listdir(tmp) if name.endswith('.tmp')]
        assert reopened.clear('extruder')
        assert not reopened.runs('extruder')


def test_candidates_never_schedule():
    model = heater_profiles.HeaterGainModel(
        [run(200, 40, 2, 100)], (20, 1, 80))
    selected = model.select(200)
    assert selected['source'] == 'base'
    assert selected['gains']['kp'] == 20


def test_curve_exact_interpolation_and_no_extrapolation():
    runs = [run(100, 20, 1, 80, 'validated'),
            run(200, 40, 3, 120, 'validated')]
    model = heater_profiles.HeaterGainModel(runs, (25, 2, 100))
    assert model.kind == 'curve'
    exact = model.select(100)
    assert exact['source'] == 'exact' and exact['gains']['kp'] == 20
    middle = model.select(150)
    assert middle['source'] == 'linear'
    assert middle['gains'] == {'kp': 30., 'ki': 2., 'kd': 100.}
    assert model.select(250)['source'] == 'base'


def test_repeated_points_use_robust_median():
    runs = [run(100, 10, 1, 90, 'validated'),
            run(100, 20, 2, 100, 'validated'),
            run(100, 999, 999, 999, 'validated')]
    model = heater_profiles.HeaterGainModel(
        runs, (20, 2, 100), gain_ratio=(.01, 100.))
    selected = model.select(100)
    assert selected['gains'] == {'kp': 20., 'ki': 2., 'kd': 100.}


def test_surface_fit_and_hull_guards():
    # Each gain is an exact plane in target and context temperature.
    points = []
    for target, context in ((100, 20), (200, 20), (100, 40), (200, 40)):
        points.append(run(target,
                          5 + .1 * target + .2 * context,
                          1 + .01 * target + .02 * context,
                          10 + .5 * target + .3 * context,
                          'validated', context))
    model = heater_profiles.HeaterGainModel(
        points, (20, 2, 80), gain_ratio=(.01, 100.))
    assert model.kind == 'surface'
    selected = model.select(150, 30)
    assert selected['source'] == 'surface'
    assert abs(selected['gains']['kp'] - 26.) < 1.e-9
    assert abs(selected['gains']['ki'] - 3.1) < 1.e-9
    assert abs(selected['gains']['kd'] - 94.) < 1.e-9
    assert model.select(250, 30)['source'] == 'base'
    assert model.select(150, 50)['source'] == 'base'
    assert model.select(150, None)['source'] == 'base'

    triangle = [run(100, 20, 2, 80, 'validated', 20),
                run(200, 30, 3, 90, 'validated', 20),
                run(100, 25, 2.5, 85, 'validated', 40)]
    triangle_model = heater_profiles.HeaterGainModel(
        triangle, (20, 2, 80), gain_ratio=(.01, 100.))
    assert triangle_model.kind == 'surface'
    # Inside the target/context bounding box but outside the measured triangle.
    assert triangle_model.select(190, 38)['source'] == 'base'


def test_gain_bounds():
    model = heater_profiles.HeaterGainModel(
        [run(100, 1000, 1000, 1000, 'validated')],
        (20, 2, 100), gain_ratio=(.5, 2.))
    selected = model.select(100)
    assert selected['gains'] == {'kp': 40., 'ki': 4., 'kd': 200.}
    assert selected['raw_gains'] == {
        'kp': 1000., 'ki': 1000., 'kd': 1000.}
    assert selected['bounded']
    assert selected['clamped_gains'] == ['kp', 'ki', 'kd']


def test_underdetermined_context_never_becomes_target_curve():
    runs = [run(100, 20, 2, 80, 'validated', 20),
            run(200, 30, 3, 90, 'validated', 20)]
    model = heater_profiles.HeaterGainModel(runs, (25, 2.5, 85))
    assert model.kind == 'base'
    assert model.select(150, 20)['source'] == 'base'


def test_reject_corrupt_and_wrong_version():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'profiles.json')
        with open(path, 'w') as stream:
            stream.write('{not json')
        try:
            heater_profiles.HeaterProfileStore(path)
        except ValueError:
            pass
        else:
            raise AssertionError('corrupt profile store was accepted')
        with open(path, 'w') as stream:
            json.dump({'version': 99, 'heaters': {}}, stream)
        try:
            heater_profiles.HeaterProfileStore(path)
        except ValueError:
            pass
        else:
            raise AssertionError('unknown profile version was accepted')


def main():
    test_private_atomic_store()
    test_candidates_never_schedule()
    test_curve_exact_interpolation_and_no_extrapolation()
    test_repeated_points_use_robust_median()
    test_surface_fit_and_hull_guards()
    test_gain_bounds()
    test_underdetermined_context_never_becomes_target_curve()
    test_reject_corrupt_and_wrong_version()
    print('PASS: heater run store and bounded gain models')


if __name__ == '__main__':
    main()
