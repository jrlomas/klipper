import os
import sys
import unittest


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(REPO, 'klippy'))
from extras import tmc


class FakeToolhead:
    def __init__(self, print_time=10., est_print_time=10., empty=True):
        self.values = print_time, est_print_time, empty

    def check_busy(self, eventtime):
        return self.values


class FakePrinter:
    command_error = RuntimeError

    def __init__(self, toolhead):
        self.toolhead = toolhead

    def lookup_object(self, name, default=None):
        return self.toolhead if name == 'toolhead' else default


class TestTMCBackgroundMotionGate(unittest.TestCase):
    def make_check(self, toolhead):
        check = object.__new__(tmc.TMCErrorCheck)
        check.printer = FakePrinter(toolhead)
        check.toolhead = None
        check.drv_status_reg_info = object()
        check.gstat_reg_info = None
        check.adc_temp_reg = None
        check.queries = 0

        def query(reg_info):
            check.queries += 1

        check._query_register = query
        return check

    def test_periodic_read_waits_for_buffered_motion(self):
        check = self.make_check(FakeToolhead(12., 10., True))
        self.assertAlmostEqual(check._do_periodic_check(20.), 20.1)
        self.assertEqual(check.queries, 0)

    def test_periodic_read_waits_for_nonempty_lookahead(self):
        check = self.make_check(FakeToolhead(10., 10., False))
        self.assertAlmostEqual(check._do_periodic_check(20.), 20.1)
        self.assertEqual(check.queries, 0)

    def test_periodic_read_runs_after_motion_drains(self):
        check = self.make_check(FakeToolhead(10., 10., True))
        self.assertAlmostEqual(check._do_periodic_check(20.), 21.)
        self.assertEqual(check.queries, 1)


if __name__ == '__main__':
    unittest.main()
