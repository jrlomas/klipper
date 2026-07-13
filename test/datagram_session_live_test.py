# LIVE test of the DTLS-class session over the UDP datagram transport,
# both ends: the host intentproto.SecureSession (initiator) against a real
# linuxprocess firmware built with WANT_DATAGRAM_SESSION (the responder),
# over an actual UDP socket. Proves handshake, per-board identity, an
# authenticated identify carried inside the session, and tamper rejection.
#
# Requires a linuxprocess build with CONFIG_WANT_DATAGRAM_SESSION at
# out/klipper.elf (skips otherwise; CI builds it).
import os
import socket
import subprocess
import sys
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ELF = os.path.join(REPO, 'out', 'klipper.elf')
PORT = 41599
PSK = b'0123456789abcdef'

sys.path.insert(0, os.path.join(REPO, 'lib', 'intentproto', 'python'))
sys.path.insert(0, os.path.join(REPO, 'klippy'))


def has_session_build():
    ac = os.path.join(REPO, 'out', 'autoconf.h')
    if not os.path.exists(ELF) or not os.path.exists(ac):
        return False
    with open(ac) as f:
        return 'CONFIG_WANT_DATAGRAM_SESSION 1' in f.read()


class TestDatagramSessionLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not has_session_build():
            raise unittest.SkipTest(
                "needs a linuxprocess build with WANT_DATAGRAM_SESSION")
        cls.psk_file = '/tmp/helix_sess_test.psk'
        with open(cls.psk_file, 'wb') as f:
            f.write(PSK)
        cls.proc = subprocess.Popen(
            [ELF, '-u', str(PORT), '-k', cls.psk_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'proc'):
            cls.proc.terminate()
            cls.proc.wait(timeout=5)

    def test_session_over_udp(self):
        import intentproto as ip
        import intentproto_transport as t
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.settimeout(3.0)
        board = ('127.0.0.1', PORT)
        try:
            host = ip.SecureSession(True, PSK, b'klippy-host')
            sk.sendto(host.start(), board)
            sh, _ = sk.recvfrom(2048)
            fin = host.on_handshake(sh)
            self.assertTrue(fin)
            sk.sendto(fin, board)
            time.sleep(0.2)
            self.assertTrue(host.established)
            self.assertEqual(host.peer_id(), b'helix-board')

            payload = (ip.vlq_encode(1) + ip.vlq_encode(0)
                       + ip.vlq_encode(40))  # identify offset=0 count=40
            frame = t.v1_build(payload, t.MESSAGE_DEST | 0)
            sk.sendto(host.encode(frame, cls=1), board)
            dg, _ = sk.recvfrom(2048)
            frames, cls = host.decode(dg)
            mp = frames[t.MESSAGE_HEADER_SIZE:
                        frames[0] - t.MESSAGE_TRAILER_SIZE]
            mid, pos = ip.vlq_decode(mp, 0)
            off, pos = ip.vlq_decode(mp, pos)
            self.assertEqual((mid, off), (0, 0))  # identify_response @0

            bad = bytearray(host.encode(frame, cls=1))
            bad[10] ^= 1
            sk.sendto(bytes(bad), board)
            with self.assertRaises(socket.timeout):
                sk.recvfrom(2048)  # tampered datagram dropped by the board

            # ---- DoS hardening: a hostile ClientHello must not reset
            # the LIVE session's keys. Send a fresh hello (attacker has
            # no PSK for the fin), then prove the original session still
            # authenticates end-to-end.
            time.sleep(0.3)  # clear the handshake rate gate
            attacker = ip.SecureSession(True, b'wrong-psk-entirely',
                                        b'mallory')
            sk2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk2.settimeout(1.0)
            try:
                sk2.sendto(attacker.start(), board)
                try:
                    sh2, _ = sk2.recvfrom(2048)
                    # A pending ServerHello may arrive; completing needs
                    # the PSK, which mallory lacks.
                    self.assertIsNone(attacker.on_handshake(sh2))
                except socket.timeout:
                    pass  # or the gate dropped it entirely - also fine
            finally:
                sk2.close()
            sk.sendto(host.encode(frame, cls=1), board)
            dg, _ = sk.recvfrom(2048)
            frames, _cls = host.decode(dg)  # live keys survived the hello
            self.assertTrue(frames)

            # ---- legitimate re-handshake (host restart): a NEW session
            # with the real PSK must be adopted and serve traffic.
            time.sleep(0.3)  # clear the handshake rate gate
            host2 = ip.SecureSession(True, PSK, b'klippy-host-2')
            sk3 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk3.settimeout(3.0)
            try:
                sk3.sendto(host2.start(), board)
                sh, _ = sk3.recvfrom(2048)
                fin = host2.on_handshake(sh)
                self.assertTrue(fin)
                sk3.sendto(fin, board)
                self.assertTrue(host2.established)
                self.assertEqual(host2.peer_id(), b'helix-board')
                sk3.sendto(host2.encode(frame, cls=1), board)
                dg, _ = sk3.recvfrom(2048)
                frames, _cls = host2.decode(dg)
                self.assertTrue(frames)  # the adopted session serves
            finally:
                sk3.close()
        finally:
            sk.close()


if __name__ == '__main__':
    unittest.main()
