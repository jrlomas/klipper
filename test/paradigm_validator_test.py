# Unit test for the FD-0001 doc-14 config-time validator: a coordination
# group (kinematic rail; all rails, for coupled kinematics) must be
# single-paradigm — all trajectory or all legacy.
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy',
                                'extras'))


class FakeStepper:
    def __init__(self, name):
        self._name = name
    def get_name(self):
        return self._name


class FakeRail:
    def __init__(self, names):
        self._steppers = [FakeStepper(n) for n in names]
    def get_steppers(self):
        return list(self._steppers)


class FakeKin:
    def __init__(self, rails, module='cartesian'):
        self.rails = rails
        type(self).__module__ = 'kinematics.' + module


class FakeToolhead:
    def __init__(self, kin):
        self._kin = kin
    def get_kinematics(self):
        return self._kin


class ConfigError(Exception):
    pass


class FakePrinter:
    def __init__(self, toolhead):
        self._toolhead = toolhead
        self.config_error = ConfigError
    def lookup_object(self, name, default="__s__"):
        if name == 'toolhead':
            return self._toolhead
        if default != "__s__":
            return default
        raise KeyError(name)


class FakeTS:
    def __init__(self, name):
        self.name = name


def make_tq(traj_names, kin):
    # Build a bare TrajectoryQueuing carrying only what the validator uses.
    import trajectory_queuing as tqmod
    tq = tqmod.TrajectoryQueuing.__new__(tqmod.TrajectoryQueuing)
    tq.printer = FakePrinter(FakeToolhead(kin))
    tq.steppers = [FakeTS(n) for n in traj_names]
    return tq


class TestParadigmValidator(unittest.TestCase):
    def test_all_trajectory_ok(self):
        kin = FakeKin([FakeRail(['stepper_x']), FakeRail(['stepper_y'])])
        make_tq(['stepper_x', 'stepper_y'], kin)._validate_paradigm_groups()

    def test_all_legacy_rails_with_traj_extruder_ok(self):
        # Regime 1: an independent (extruder) trajectory joint beside
        # legacy kinematic rails is the supported mixed-machine case.
        kin = FakeKin([FakeRail(['stepper_x']), FakeRail(['stepper_y'])])
        make_tq(['extruder'], kin)._validate_paradigm_groups()

    def test_mixed_rail_rejected(self):
        # Two lockstep steppers on one rail, only one converted -> error.
        kin = FakeKin([FakeRail(['stepper_z', 'stepper_z1'])])
        tq = make_tq(['stepper_z'], kin)
        with self.assertRaises(ConfigError):
            tq._validate_paradigm_groups()

    def test_coupled_kinematics_split_rejected(self):
        # corexy: rails move as one group; converting only X -> error.
        kin = FakeKin([FakeRail(['stepper_x']), FakeRail(['stepper_y'])],
                      module='corexy')
        tq = make_tq(['stepper_x'], kin)
        with self.assertRaises(ConfigError):
            tq._validate_paradigm_groups()

    def test_coupled_kinematics_all_converted_ok(self):
        kin = FakeKin([FakeRail(['stepper_x']), FakeRail(['stepper_y'])],
                      module='corexy')
        make_tq(['stepper_x', 'stepper_y'], kin)._validate_paradigm_groups()

    def test_cartesian_partial_conversion_ok(self):
        # cartesian: rails are independent groups; converting only X is
        # a legal (if unusual) topology.
        kin = FakeKin([FakeRail(['stepper_x']), FakeRail(['stepper_y'])])
        make_tq(['stepper_x'], kin)._validate_paradigm_groups()


if __name__ == '__main__':
    unittest.main()
