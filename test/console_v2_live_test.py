# LIVE test of the console framing-v2 (BCH) MCU path against a real
# linuxprocess firmware over its PTY console — the in-container proof for
# the C de-frame that was previously marked "UNPROVEN — needs hardware":
#
#   * dual-accept: a stock v1 identify gets a v1 reply (no latch);
#   * a v2 (BCH) identify latches the link and the reply comes re-framed
#     as v2 (FV2_FLAG set), carrying the same inner v1 frames;
#   * LIVE error correction: a v2 frame sent with 3 flipped bits is
#     corrected by the MCU's BCH decoder and still dispatched.
#
# Uses the same BchConsoleCodec klippy's bridge uses, so this doubles as
# an end-to-end host<->MCU compatibility check of the two implementations.
#
# Requires a linuxprocess build with CONFIG_WANT_CONSOLE_FRAMING_V2 at
# out/klipper.elf (skips otherwise; CI builds it).
import os
import select
import subprocess
import sys
import termios
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ELF = os.path.join(REPO, 'out', 'klipper.elf')
PTY = '/tmp/helix_cv2_test_pty'

sys.path.insert(0, os.path.join(REPO, 'lib', 'intentproto', 'python'))
sys.path.insert(0, os.path.join(REPO, 'klippy'))


def has_cv2_build():
    ac = os.path.join(REPO, 'out', 'autoconf.h')
    if not os.path.exists(ELF) or not os.path.exists(ac):
        return False
    with open(ac) as f:
        txt = f.read()
    return ('CONFIG_WANT_CONSOLE_FRAMING_V2 1' in txt
            and 'CONFIG_MACH_LINUX 1' in txt)


def identify_payload():
    import intentproto as ip
    # identify offset=0 count=40 (msgid 1)
    return ip.vlq_encode(1) + ip.vlq_encode(0) + ip.vlq_encode(40)


class TestConsoleV2Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not has_cv2_build():
            raise unittest.SkipTest(
                "needs a linuxprocess build with WANT_CONSOLE_FRAMING_V2")
        cls.proc = subprocess.Popen(
            [ELF, '-I', PTY],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5.0
        while not os.path.islink(PTY) and time.time() < deadline:
            time.sleep(0.05)
        cls.fd = os.open(PTY, os.O_RDWR | os.O_NOCTTY)
        # Raw mode: the pty must not echo or translate the binary stream.
        attrs = termios.tcgetattr(cls.fd)
        attrs[0] = attrs[1] = attrs[3] = 0  # iflag, oflag, lflag
        termios.tcsetattr(cls.fd, termios.TCSANOW, attrs)
        os.set_blocking(cls.fd, False)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'fd'):
            os.close(cls.fd)
        if hasattr(cls, 'proc'):
            cls.proc.terminate()
            cls.proc.wait(timeout=5)

    def _read_for(self, seconds):
        buf = b''
        deadline = time.time() + seconds
        while time.time() < deadline:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if self.fd in r:
                try:
                    buf += os.read(self.fd, 4096)
                except (BlockingIOError, OSError):
                    pass
        return buf

    def _find_identify_response(self, frames):
        import intentproto_transport as t
        import intentproto as ip
        for f in frames:
            payload = f[t.MESSAGE_HEADER_SIZE:
                        len(f) - t.MESSAGE_TRAILER_SIZE]
            if not payload:
                continue  # bare ack
            mid, pos = ip.vlq_decode(payload, 0)
            if mid == 0:  # identify_response
                off, pos = ip.vlq_decode(payload, pos)
                return off
        return None

    def test_latch_and_bch_correction(self):
        import intentproto_transport as t

        # ---- phase 1: stock v1 in, stock v1 out (no latch) ----
        v1 = t.v1_build(identify_payload(), t.MESSAGE_DEST | 0)
        os.write(self.fd, v1)
        raw = self._read_for(1.0)
        self.assertTrue(raw, "no v1 reply from the firmware")
        frames, _rest = t.v1_split(bytearray(raw))
        self.assertTrue(frames, "v1 reply did not parse")
        for f in frames:  # every reply frame is plain v1 (no FV2 flag)
            self.assertFalse(f[1] & t.FRAME_V2_FLAG)
        self.assertEqual(self._find_identify_response(frames), 0)

        # ---- phase 2: v2 in -> link latches, v2 out ----
        codec = t.BchConsoleCodec()
        v1 = t.v1_build(identify_payload(), t.MESSAGE_DEST | 1)
        os.write(self.fd, codec.to_wire(v1))
        raw = self._read_for(1.0)
        self.assertTrue(raw, "no v2 reply from the firmware")
        # The raw stream must now be v2 frames (seq byte carries FV2_FLAG).
        self.assertTrue(any(b & t.FRAME_V2_FLAG for b in raw[1:2]),
                        "reply did not latch to v2 framing")
        rebuilt = codec.from_wire(raw)
        frames, _rest = t.v1_split(bytearray(rebuilt))
        self.assertTrue(frames, "v2 reply did not decode")
        self.assertEqual(self._find_identify_response(frames), 0)
        self.assertEqual(codec.rx_uncorrectable, 0)

        # ---- phase 3: LIVE BCH correction of 3 bit errors ----
        v1 = t.v1_build(identify_payload(), t.MESSAGE_DEST | 2)
        wire = bytearray(codec.to_wire(v1))
        # Flip 3 bits across the frame body (t=3 code corrects them all).
        wire[3] ^= 0x10
        wire[7] ^= 0x02
        wire[len(wire) - 3] ^= 0x40  # in the BCH parity itself
        os.write(self.fd, bytes(wire))
        raw = self._read_for(1.0)
        self.assertTrue(raw, "no reply to the damaged v2 frame - BCH"
                             " correction failed on the MCU")
        rebuilt = codec.from_wire(raw)
        frames, _rest = t.v1_split(bytearray(rebuilt))
        self.assertEqual(self._find_identify_response(frames), 0,
                         "damaged v2 frame was not corrected+dispatched")


if __name__ == '__main__':
    unittest.main()
