// Host-side datagram<->HostSession binding. See datagram_carrier.hpp.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
// MIT licensed (see LICENSE).

#include "intentproto/datagram_carrier.hpp"
#include "intentproto/host.hpp"

namespace intentproto {

void DatagramCarrier::init(HostSession* s, const uint8_t* psk,
                           size_t psk_len, uint8_t fec_k,
                           int (*send_fn)(const uint8_t*, size_t, void*),
                           void* user_in) {
    session = s;
    send = send_fn;
    user = user_in;
    datagram_tx_init(&tx, psk, psk_len, fec_k);
    datagram_rx_init(&rx, psk, psk_len);
}

int DatagramCarrier::write_frame(const uint8_t* frames, size_t len) {
    size_t n = datagram_encode(&tx, txbuf, frames, len,
                               TrafficClass::Scheduled);
    if (!n)
        return -1;
    int rc = send(txbuf, n, user);
    if (rc < 0)
        return rc;
    // Emit the block's parity datagram when the FEC block just filled.
    size_t p = datagram_parity_flush(&tx, txbuf);
    if (p)
        send(txbuf, p, user);
    return (int)len;
}

void DatagramCarrier::on_datagram(const uint8_t* data, size_t len) {
    // datagram_decode may correct/consume in place, so work on a copy.
    uint8_t buf[DATAGRAM_MAX];
    if (len > sizeof(buf))
        return;
    for (size_t i = 0; i < len; i++)
        buf[i] = data[i];
    const uint8_t* frames = nullptr;
    TrafficClass cls;
    int flen = datagram_decode(&rx, buf, len, &frames, &cls);
    if (flen < 0)
        return;  // auth failure / malformed - the ARQ retransmits
    if (flen > 0 && frames)
        session->on_rx(frames, (size_t)flen);
    // A parity datagram (flen == 0) may have reconstructed a lost
    // datagram; replay it into the session in block order.
    uint8_t rec[DATAGRAM_MAX];
    size_t rn = datagram_take_recovered(&rx, rec, sizeof(rec));
    if (rn)
        session->on_rx(rec, rn);
}

int datagram_write_thunk(const uint8_t* data, size_t len, void* user) {
    return static_cast<DatagramCarrier*>(user)->write_frame(data, len);
}

} // namespace intentproto
