# Python <-> C++ round-trip of the secure session (session_sec.hpp via
# capi): handshake, per-board identity, session datagrams, replay and
# tamper rejection, epoch rotation, wrong-PSK rejection. Run from
# lib/intentproto: python3 python/test_session_py.py
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import intentproto as ip

PSK = b'0123456789abcdef'


def test_session():
    host = ip.SecureSession(True, PSK, b'host')
    board = ip.SecureSession(False, PSK, b'oams-board-01')
    reply = board.on_handshake(host.start())
    fin = host.on_handshake(reply)
    board.on_handshake(fin)
    assert host.established and board.established
    assert host.peer_id() == b'oams-board-01'
    assert board.peer_id() == b'host'

    frames = bytes(range(64))
    dg = host.encode(frames, cls=1)
    out, cls = board.decode(dg)
    assert (out, cls) == (frames, 1)

    try:
        board.decode(dg)
        raise AssertionError("replay accepted")
    except ValueError:
        pass

    bad = bytearray(host.encode(b'hello'))
    bad[8] ^= 1
    try:
        board.decode(bytes(bad))
        raise AssertionError("tamper accepted")
    except ValueError:
        pass

    host.rekey()
    out2, _ = board.decode(host.encode(b'after-rekey'))
    assert out2 == b'after-rekey'

    evil = ip.SecureSession(False, b'wrong-psk-000000', b'evil')
    h2 = ip.SecureSession(True, PSK, b'host2')
    r = evil.on_handshake(h2.start())
    if r is not None:
        h2.on_handshake(r)
        assert not h2.established
    print("session python<->C++: all checks passed")


if __name__ == '__main__':
    test_session()
