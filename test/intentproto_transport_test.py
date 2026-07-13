# Loopback test for the host v2 transport transform
# (klippy/intentproto_transport.py).
#
# Proves the v1<->v2 framing transform is lossless in both directions and
# preserves the exact v1 frame (so v1's ARQ rides through v2 untouched),
# including 5-byte ack frames, resync bytes, and arbitrarily chunked reads.
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'klippy'))
import intentproto_transport as t


def v1_frame(payload, seq_nibble):
    return t.v1_build(payload, t.MESSAGE_DEST | (seq_nibble & 0x0f))


SAMPLE = [
    v1_frame(b"", 0),                       # a bare ack (total == 5)
    v1_frame(bytes([2, 5]), 1),             # tiny command
    v1_frame(bytes(range(20)), 2),          # medium
    v1_frame(bytes((i * 3 + 1) & 0xff for i in range(55)), 15),  # near-max
]


class TestBchTransform(unittest.TestCase):
    def _round_trip(self, frames, tx_chunks=None, rx_chunks=None):
        host = t.BchConsoleCodec()   # klippy side
        mcu = t.BchConsoleCodec()    # far side
        stream = b"".join(frames)
        # host: v1 stream -> v2 wire (optionally fed in chunks)
        wire = b""
        for chunk in (tx_chunks if tx_chunks else [stream]):
            wire += host.to_wire(chunk)
        # mcu: v2 wire -> reconstructed v1 (optionally chunked)
        rebuilt = b""
        for chunk in (rx_chunks if rx_chunks
                      else [wire[i:i + 7] for i in range(0, len(wire), 7)]):
            rebuilt += mcu.from_wire(chunk)
        return stream, wire, rebuilt

    def test_exact_reconstruction(self):
        stream, wire, rebuilt = self._round_trip(SAMPLE)
        self.assertEqual(rebuilt, stream)
        self.assertNotEqual(wire, stream)  # it really got re-framed

    def test_chunked_v1_input(self):
        # Feed the v1 byte stream one byte at a time (partial-frame tails).
        stream = b"".join(SAMPLE)
        stream, wire, rebuilt = self._round_trip(
            SAMPLE, tx_chunks=[stream[i:i + 1] for i in range(len(stream))])
        self.assertEqual(rebuilt, stream)

    def test_leading_resync_byte(self):
        # serialqueue prepends a lone 0x7e to a retransmit block.
        host = t.BchConsoleCodec()
        mcu = t.BchConsoleCodec()
        stream = bytes([t.MESSAGE_SYNC]) + b"".join(SAMPLE)
        wire = host.to_wire(stream)
        rebuilt = mcu.from_wire(wire)
        self.assertEqual(rebuilt, b"".join(SAMPLE))

    def test_reverse_direction(self):
        # MCU replies: v1 -> v2 on the far side, v1 back on the host side.
        replies = [v1_frame(bytes([9, 9, 9]), 7), v1_frame(b"", 8)]
        mcu = t.BchConsoleCodec()
        host = t.BchConsoleCodec()
        wire = mcu.to_wire(b"".join(replies))
        rebuilt = host.from_wire(wire)
        self.assertEqual(rebuilt, b"".join(replies))


class TestDatagramTransform(unittest.TestCase):
    def test_datagram_round_trip(self):
        DatagramCodec = t.load_datagram_codec()
        psk = b"0123456789abcdef"
        tx = DatagramCodec(psk)
        rx = DatagramCodec(psk)
        frame = b"".join(SAMPLE)
        dg = tx.encode(frame)
        out = rx.decode(dg)
        self.assertEqual(out, [frame])

    def test_datagram_auth_rejects_tamper(self):
        DatagramCodec = t.load_datagram_codec()
        psk = b"0123456789abcdef"
        tx, rx = DatagramCodec(psk), DatagramCodec(psk)
        dg = bytearray(tx.encode(b"\x05\x10\x00\x00\x7e"))
        dg[4] ^= 0x01  # flip a payload bit
        self.assertEqual(rx.decode(bytes(dg)), [])  # dropped
        self.assertEqual(rx.auth_failures, 1)


class TestBridge(unittest.TestCase):
    def test_bch_bridge_passthrough_then_upgrade(self):
        # A bch bridge must start in v1 pass-through (identify happens in
        # plain v1 against a possibly-stock board) and transform only after
        # enable_v2() — the capability-driven negotiation.
        import socket
        import time
        host_end, mcu_end = socket.socketpair()
        host_end.setblocking(False)
        link = "/tmp/ip-bridge-passthrough"
        br = t.TransportBridge('bch', link,
                               stream_wire_fd=host_end.fileno())
        br.open()
        try:
            self.assertFalse(br.v2_active)
            klippy = os.open(link, os.O_RDWR)
            try:
                frame = v1_frame(bytes(range(10)), 4)
                os.write(klippy, frame)
                time.sleep(0.2)
                # Pass-through: the wire sees the raw v1 frame verbatim.
                self.assertEqual(mcu_end.recv(4096), frame)
                br.enable_v2()
                os.write(klippy, frame)
                time.sleep(0.2)
                wire = mcu_end.recv(4096)
                self.assertNotEqual(wire, frame)  # now transformed
                self.assertEqual(t.BchConsoleCodec().from_wire(wire), frame)
                self.assertEqual(br.stats()['mode'], 'bch')
                self.assertTrue(br.stats()['v2_active'])
            finally:
                os.close(klippy)
        finally:
            br.close()
            host_end.close()
            mcu_end.close()

    def test_bch_bridge_over_pty(self):
        import socket
        import time
        host_end, mcu_end = socket.socketpair()
        host_end.setblocking(False)
        link = "/tmp/ip-bridge-unittest"
        br = t.TransportBridge('bch', link,
                               stream_wire_fd=host_end.fileno())
        br.open()
        br.enable_v2()  # bch starts in v1 pass-through until negotiated
        try:
            klippy = os.open(link, os.O_RDWR)
            try:
                frames = [
                    v1_frame(b"", 0),
                    v1_frame(bytes(range(20)), 3),
                    v1_frame(bytes((i * 5) & 0xff for i in range(40)), 15),
                ]
                # host -> wire
                os.write(klippy, b"".join(frames))
                time.sleep(0.2)
                wire = mcu_end.recv(8192)
                mcu = t.BchConsoleCodec()
                self.assertEqual(mcu.from_wire(wire), b"".join(frames))
                self.assertNotEqual(wire, b"".join(frames))
                # wire -> host (a reply)
                reply = v1_frame(bytes([7, 7, 7]), 9)
                mcu_end.sendall(mcu.to_wire(reply))
                time.sleep(0.2)
                self.assertEqual(os.read(klippy, 4096), reply)
            finally:
                os.close(klippy)
        finally:
            br.close()
            host_end.close()
            mcu_end.close()


if __name__ == '__main__':
    unittest.main()
