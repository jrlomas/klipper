# End-to-end test of scripts/helix_flash.py against the desktop bootloader
# simulator (lib/intentproto/tools/bootsim.cpp), which links the REAL
# protocol core and the REAL bootcore update state machine. Proves the
# whole in-band update flow — identify/dictionary, flash_begin (erase),
# ack-windowed flash_data, CRC verify, boot — with no hardware.
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BOOTSIM = os.path.join(REPO, 'lib', 'intentproto', 'build', 'bootsim')
FLASHER = os.path.join(REPO, 'scripts', 'helix_flash.py')


def build_bootsim():
    subprocess.run(['make', 'bootsim'],
                   cwd=os.path.join(REPO, 'lib', 'intentproto'),
                   check=True, capture_output=True)


class TestHelixFlash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BOOTSIM):
            build_bootsim()

    def _run(self, image, extra=None):
        dump = tempfile.mktemp(suffix='.bin')
        cmd = [sys.executable, FLASHER,
               '--exec', '%s %s' % (BOOTSIM, dump), image] + (extra or [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r, dump

    def test_flash_round_trip(self):
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(8192))
        r, dump = self._run(img)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('verify ok', r.stdout)
        self.assertIn('boot ok', r.stdout)
        with open(img, 'rb') as a, open(dump, 'rb') as b:
            self.assertEqual(a.read(), b.read())

    def test_odd_size_image(self):
        # A size that is not a multiple of the chunk or page size.
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(5013))
        r, dump = self._run(img)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(img, 'rb') as a, open(dump, 'rb') as b:
            self.assertEqual(a.read(), b.read())

    def test_no_boot_flag(self):
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(2048))
        r, dump = self._run(img, extra=['--no-boot'])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('verify ok', r.stdout)
        self.assertNotIn('boot ok', r.stdout)
        self.assertFalse(os.path.exists(dump))  # no boot -> no "reset"


if __name__ == '__main__':
    unittest.main()
