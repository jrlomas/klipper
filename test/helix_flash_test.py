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

    def _run(self, image, extra=None, pubkey=None):
        dump = tempfile.mktemp(suffix='.bin')
        sim = '%s %s' % (BOOTSIM, dump)
        if pubkey:
            sim += ' %s' % (pubkey,)  # signing-enabled bootloader
        cmd = [sys.executable, FLASHER,
               '--exec', sim, image] + (extra or [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r, dump

    def _sign(self, image):
        # Detached Ed25519 signature with the DEV/TEST key (never a
        # production secret), exactly as a release pipeline would.
        sig = tempfile.mktemp(suffix='.sig')
        subprocess.run(
            [sys.executable, os.path.join(REPO, 'scripts', 'sign_image.py'),
             'blob', image, '--key',
             os.path.join(REPO, 'keys', 'helix_dev_signing.key'),
             '-o', sig], check=True, capture_output=True)
        return sig

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

    def test_signed_flash(self):
        # Full signed flow against a signing-enabled bootloader: sign the
        # image with the dev key, ship the signature via --sign-file, and
        # verify/boot must pass with the byte-identical image landing.
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(6000))
        sig = self._sign(img)
        pub = os.path.join(REPO, 'keys', 'helix_dev_signing.pub')
        r, dump = self._run(img, extra=['--sign-file', sig], pubkey=pub)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('signature accepted', r.stdout)
        self.assertIn('verify ok', r.stdout)
        self.assertIn('boot ok', r.stdout)
        with open(img, 'rb') as a, open(dump, 'rb') as b:
            self.assertEqual(a.read(), b.read())

    def test_signed_flash_bad_signature_rejected(self):
        # A tampered signature must fail flash_verify with ERR_SIG and
        # never reach boot.
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(4096))
        sig = self._sign(img)
        with open(sig, 'rb') as f:
            raw = bytearray(f.read())
        raw[10] ^= 0xff
        with open(sig, 'wb') as f:
            f.write(raw)
        pub = os.path.join(REPO, 'keys', 'helix_dev_signing.pub')
        r, dump = self._run(img, extra=['--sign-file', sig], pubkey=pub)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('ERR_SIG', r.stdout + r.stderr)
        self.assertNotIn('boot ok', r.stdout)
        self.assertFalse(os.path.exists(dump))

    def test_signing_bootloader_requires_signature(self):
        # A signing-enabled bootloader must refuse an UNSIGNED flash: the
        # CRC alone cannot mark the image valid.
        img = tempfile.mktemp(suffix='.bin')
        with open(img, 'wb') as f:
            f.write(os.urandom(3000))
        pub = os.path.join(REPO, 'keys', 'helix_dev_signing.pub')
        r, dump = self._run(img, pubkey=pub)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('ERR_SIG', r.stdout + r.stderr)
        self.assertFalse(os.path.exists(dump))


if __name__ == '__main__':
    unittest.main()
