# Loopback test for the klippy datagram bridge in DTLS-class SESSION mode.
#
# Exercises the host-side wiring end to end WITHOUT firmware: a Python
# responder SecureSession stands in for the board on a real UDP socket, and
# the TransportBridge (initiator) runs its full handshake + session-datagram
# pump. Bytes written to the bridge's PTY are session-sealed on the wire,
# echoed by the responder, and must arrive back on the PTY intact. This
# proves open()'s handshake, _host_to_wire's session encode, and
# _wire_to_host's session decode. (datagram_session_live_test.py covers the
# same host session against real firmware; this one needs no build.)
import os
import select
import socket
import sys
import threading
import time
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(REPO, 'lib', 'intentproto', 'python'))
sys.path.insert(0, os.path.join(REPO, 'klippy'))

PSK = b'0123456789abcdef'


class _Responder(threading.Thread):
    """A board stand-in: completes the handshake, then echoes every session
    datagram's payload back inside the session."""
    def __init__(self):
        super().__init__(daemon=True)
        import intentproto as ip
        self.sess = ip.SecureSession(False, PSK, b'test-board')
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False

    def run(self):
        while not self._stop:
            r, _, _ = select.select([self.sock], [], [], 0.2)
            if not r:
                continue
            data, addr = self.sock.recvfrom(2048)
            if not self.sess.established:
                out = self.sess.on_handshake(data)
                if out:
                    self.sock.sendto(out, addr)
                continue
            try:
                payload, cls = self.sess.decode(data)
            except ValueError:
                continue
            self.sock.sendto(self.sess.encode(payload, cls), addr)

    def stop(self):
        self._stop = True
        self.sock.close()


class TestSessionBridge(unittest.TestCase):
    def test_handshake_and_echo(self):
        import intentproto_transport as ipt
        resp = _Responder()
        resp.start()
        pty_link = '/tmp/helix_sess_bridge_test'
        bridge = ipt.TransportBridge(
            'datagram', pty_link, psk=PSK, session=True,
            board_id=b'test-board',  # identity the responder must present
            udp_board=('127.0.0.1', resp.port), udp_listen=0)
        slave = None
        try:
            bridge.open()  # runs the 3-message handshake before the pump
            self.assertTrue(bridge.session_established)
            self.assertEqual(bridge.peer_id, b'test-board')
            st = bridge.stats()
            self.assertTrue(st['session'] and st['session_established'])
            self.assertEqual(st['peer_id'], 'test-board')
            self.assertEqual(st['auth_failures'], 0)
            self.assertIn('tx_epoch', st)

            slave = os.open(pty_link, os.O_RDWR | os.O_NOCTTY)
            os.set_blocking(slave, False)
            msg = b'\x0a\x11helix-intent-payload\x7e'
            os.write(slave, msg)

            got = b''
            deadline = time.time() + 3.0
            while len(got) < len(msg) and time.time() < deadline:
                r, _, _ = select.select([slave], [], [], 0.2)
                if slave in r:
                    got += os.read(slave, 4096)
            self.assertEqual(got, msg)
        finally:
            if slave is not None:
                os.close(slave)
            bridge.close()
            resp.stop()

    def test_identity_mismatch_rejected(self):
        # The responder presents 'test-board'; a bridge configured to
        # expect a different identity must reject the handshake.
        import intentproto_transport as ipt
        resp = _Responder()
        resp.start()
        bridge = ipt.TransportBridge(
            'datagram', '/tmp/helix_sess_bridge_mismatch', psk=PSK,
            session=True, board_id=b'some-other-board',
            udp_board=('127.0.0.1', resp.port), udp_listen=0)
        try:
            with self.assertRaisesRegex(ipt.FrameError,
                                        'identity mismatch'):
                bridge.open()
            self.assertFalse(bridge.session_established)
        finally:
            bridge.close()
            resp.stop()

    def test_missing_psk_rejected(self):
        import intentproto_transport as ipt
        with self.assertRaises(ipt.FrameError):
            ipt.TransportBridge('datagram', '/tmp/helix_sess_nopsk',
                                session=True, board_id=b'x')


if __name__ == '__main__':
    unittest.main()
