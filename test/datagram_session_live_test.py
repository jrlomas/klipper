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
PSK = b'0123456789abcdef'

sys.path.insert(0, os.path.join(REPO, 'lib', 'intentproto', 'python'))
sys.path.insert(0, os.path.join(REPO, 'klippy'))


def has_session_build():
    ac = os.path.join(REPO, 'out', 'autoconf.h')
    if not os.path.exists(ELF) or not os.path.exists(ac):
        return False
    with open(ac) as f:
        return 'CONFIG_WANT_DATAGRAM_SESSION 1' in f.read()


def recv_identify_response(ip, transport, session, sock, timeout=3.0):
    """Receive until the requested identify_response arrives.

    The firmware can emit authenticated load stats asynchronously, so callers
    cannot assume that the first session datagram after a request is its reply.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("identify_response not received")
        sock.settimeout(remaining)
        datagram, _peer = sock.recvfrom(2048)
        encoded_frames, _cls = session.decode(datagram)
        frames, _tail = transport.v1_split(bytearray(encoded_frames))
        for frame in frames:
            payload, _seq = transport.v1_payload_seq(frame)
            if not payload:
                continue
            msgid, pos = ip.vlq_decode(payload, 0)
            if msgid != 0:
                continue
            offset, _pos = ip.vlq_decode(payload, pos)
            if offset == 0:
                return frame


class TestDatagramSessionLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not has_session_build():
            if os.environ.get('HELIX_REQUIRE_LIVE'):
                raise RuntimeError(
                    "CI expected a linuxprocess WANT_DATAGRAM_SESSION build")
            raise unittest.SkipTest(
                "needs a linuxprocess build with WANT_DATAGRAM_SESSION")
        cls.psk_file = '/tmp/helix_sess_test.psk'
        with open(cls.psk_file, 'wb') as f:
            f.write(PSK)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(('127.0.0.1', 0))
        cls.port = probe.getsockname()[1]
        probe.close()
        cls.proc = subprocess.Popen(
            [ELF, '-u', str(cls.port), '-k', cls.psk_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        if cls.proc.poll() is not None:
            raise RuntimeError("linuxprocess session firmware exited early")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'proc'):
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        if hasattr(cls, 'psk_file') and os.path.exists(cls.psk_file):
            os.unlink(cls.psk_file)

    def test_session_over_udp(self):
        import intentproto as ip
        import intentproto_transport as t
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.settimeout(3.0)
        board = ('127.0.0.1', self.port)
        try:
            # Session-capable firmware must preserve the authenticated
            # static fallback before a session is established, including
            # sequence high bytes that equal handshake type tags.
            static = t.load_datagram_codec()(PSK, 0)
            payload = (ip.vlq_encode(1) + ip.vlq_encode(0)
                       + ip.vlq_encode(40))  # identify offset=0 count=40
            for index, seq in enumerate((0x5100, 0x5101, 0x51ff,
                                         0x5300, 0x53ff)):
                static.tx_seq = seq
                static_frame = t.v1_build(
                    payload, t.MESSAGE_DEST | (index & t.MESSAGE_SEQ_MASK))
                sk.sendto(static.encode(static_frame, cls=1), board)
                static_reply, _ = sk.recvfrom(2048)
                self.assertTrue(static.decode(static_reply), hex(seq))

            host = ip.SecureSession(True, PSK, b'klippy-host')
            sk.sendto(host.start(), board)
            sh, _ = sk.recvfrom(2048)
            fin = host.on_handshake(sh)
            self.assertTrue(fin)

            # A different unauthenticated hello arriving between the real
            # hello and fin may not replace the active handshake.
            time.sleep(0.3)
            pre_attacker = ip.SecureSession(
                True, b'wrong-psk-entirely', b'pre-mallory')
            pre_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pre_sock.settimeout(0.3)
            try:
                pre_sock.sendto(pre_attacker.start(), board)
                with self.assertRaises(socket.timeout):
                    pre_sock.recvfrom(2048)
            finally:
                pre_sock.close()
            sk.sendto(fin, board)
            time.sleep(0.2)
            self.assertTrue(host.established)
            self.assertEqual(host.peer_id(), b'helix-board')

            # The static probes consumed v1 sequence numbers 0 through 4.
            # Session negotiation changes the envelope, not Klipper's
            # end-to-end v1 sequence space.
            frame = t.v1_build(payload, t.MESSAGE_DEST | 5)
            sk.sendto(host.encode(frame, cls=1), board)
            self.assertTrue(recv_identify_response(ip, t, host, sk))

            # Session pinning: a valid static-PSK datagram cannot bypass
            # the live session's identity/replay/key-rotation guarantees.
            # Use a clean sequence whose high byte has no handshake or
            # DGF_SESSION collision. Old, unpinned routing accepted this.
            static.tx_seq = 0x0100
            static_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            static_sock.settimeout(0.3)
            try:
                static_sock.sendto(static.encode(frame, cls=1), board)
                with self.assertRaises(socket.timeout):
                    static_sock.recvfrom(2048)
            finally:
                static_sock.close()

            bad = bytearray(host.encode(frame, cls=1))
            bad[10] ^= 1
            sk.sendto(bytes(bad), board)
            with self.assertRaises(socket.timeout):
                sk.recvfrom(2048)  # tampered datagram dropped by the board

            # ---- DoS hardening: a hostile ClientHello must not reset
            # the LIVE session's keys or elicit a reflected ServerHello.
            # ClientHello itself proves PSK possession before responder
            # state or reply routing changes.
            attacker = ip.SecureSession(True, b'wrong-psk-entirely',
                                        b'mallory')
            # Discard any already-buffered periodic packet so the one
            # observed below is known to have been emitted after the hello.
            sk.settimeout(0.01)
            try:
                while True:
                    old_dg, _ = sk.recvfrom(2048)
                    host.decode(old_dg)
            except socket.timeout:
                pass
            sk2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk2.settimeout(1.0)
            try:
                attacker_hello = attacker.start()
                sk2.sendto(attacker_hello, board)
                sk2.settimeout(0.2)
                with self.assertRaises(socket.timeout):
                    sk2.recvfrom(2048)

                # Repeated unauthenticated hellos likewise receive nothing;
                # they cannot occupy the half-open candidate slot.
                sk2.sendto(attacker_hello, board)
                with self.assertRaises(socket.timeout):
                    sk2.recvfrom(2048)

                # The board emits load stats asynchronously every five
                # seconds. They must remain routed to the authenticated
                # host after the rejected handshake, never to the most
                # recent ClientHello source. This is the precise regression
                # for unauthenticated reply-peer redirection.
                sk.settimeout(6.5)
                async_dg, _ = sk.recvfrom(2048)
                async_frames, _ = host.decode(async_dg)
                self.assertTrue(async_frames)
                sk2.settimeout(0.2)
                with self.assertRaises(socket.timeout):
                    sk2.recvfrom(2048)
            finally:
                sk2.close()
            frame = t.v1_build(payload, t.MESSAGE_DEST | 6)
            sk.sendto(host.encode(frame, cls=1), board)
            self.assertTrue(recv_identify_response(ip, t, host, sk))

            # ---- legitimate re-handshake (host restart): a NEW session
            # with the real PSK must be adopted and serve traffic after the
            # hostile hello was rejected before creating half-open state.
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
                frame = t.v1_build(payload, t.MESSAGE_DEST | 7)
                sk3.sendto(host2.encode(frame, cls=1), board)
                self.assertTrue(recv_identify_response(ip, t, host2, sk3))
            finally:
                sk3.close()
        finally:
            sk.close()

    def test_trust_network_sequence_collision(self):
        import intentproto as ip
        import intentproto_transport as t
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        proc = subprocess.Popen(
            [ELF, '-u', str(port), '-t'], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3.0)
        try:
            time.sleep(0.3)
            self.assertIsNone(proc.poll())
            codec = t.load_datagram_codec()(b'', 0)
            codec.tx_seq = 0x5300
            payload = (ip.vlq_encode(1) + ip.vlq_encode(0)
                       + ip.vlq_encode(40))
            frame = t.v1_build(payload, t.MESSAGE_DEST | 0)
            # Six resync bytes make the whole datagram exactly 17 bytes,
            # the old ClientFin classifier's collision shape.
            datagram = codec.encode(frame + b'\x7e' * 6, cls=1)
            self.assertEqual(len(datagram), 17)
            sock.sendto(datagram, ('127.0.0.1', port))
            reply, _peer = sock.recvfrom(2048)
            self.assertTrue(codec.decode(reply))
        finally:
            sock.close()
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
