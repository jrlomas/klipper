# LIVE test of the built-in self-test protocol (src/self_test.c) against a
# real firmware binary: the linuxprocess build runs as a process, serves
# its dictionary over a PTY, and executes every advertised self test —
# including the trajectory fixed-point kernel checked against host golden
# vectors. This is the in-container form of the "test mode" that hardware
# verification uses on real boards (docs/Helix_Test_Plan.md).
#
# Requires a linuxprocess build with WANT_SELF_TEST at out/klipper.elf
# (skips with a message otherwise; CI builds it).
import os
import subprocess
import sys
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ELF = os.path.join(REPO, 'out', 'klipper.elf')
PTY = '/tmp/klipper_selftest_unittest'

sys.path.insert(0, os.path.join(REPO, 'scripts'))


def is_linux_build():
    if not os.path.exists(ELF):
        return False
    with open(os.path.join(REPO, 'out', 'autoconf.h')) as f:
        return 'CONFIG_MACH_LINUX 1' in f.read()


class TestSelfTestLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not is_linux_build():
            raise unittest.SkipTest(
                "needs a linuxprocess build with WANT_SELF_TEST at"
                " out/klipper.elf (make with CONFIG_MACH_LINUX=y"
                " CONFIG_WANT_SELF_TEST=y)")
        cls.proc = subprocess.Popen([ELF, '-I', PTY],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not os.path.exists(PTY) and time.monotonic() < deadline:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'proc'):
            cls.proc.terminate()
            cls.proc.wait(timeout=5)

    def test_all_self_tests_pass_live(self):
        from helix_flash import Link, Proto
        link = Link(device=PTY)
        try:
            proto = Proto(link)
            d = proto.fetch_dictionary()
            count = d.get('config', {}).get('SELF_TEST_COUNT')
            self.assertTrue(count and count >= 5,
                            "SELF_TEST_COUNT missing from dictionary")
            enums = d.get('enumerations', {}).get('self_test', {})
            self.assertIn('crc_wire', enums)
            self.assertIn('traj_kernel', enums)
            for tid in range(count):
                proto.send('run_self_test', tid)
                rid, status, value = proto.wait_response(
                    'self_test_result', 5.0)
                self.assertEqual(rid, tid)
                self.assertNotEqual(status, 1,
                                    "self test %d FAILED (value=0x%x)"
                                    % (tid, value))
            # The two signature results: the wire CRC check value and the
            # golden-vector count are exact, not just "no failure".
            proto.send('run_self_test', enums['crc_wire'])
            _, status, value = proto.wait_response('self_test_result', 5.0)
            self.assertEqual((status, value), (0, 0x6f91))
            proto.send('run_self_test', enums['traj_kernel'])
            _, status, value = proto.wait_response('self_test_result', 5.0)
            self.assertEqual(status, 0)
            self.assertEqual(value, 4)  # all four golden vectors matched
        finally:
            link.close()


if __name__ == '__main__':
    unittest.main()
