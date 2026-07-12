// intentproto v2 link layer: BCH framing, authenticated datagrams,
// XOR erasure parity. See datagram.hpp and FD-0001 doc 07.

#include "intentproto/datagram.hpp"
#include "intentproto/bch.hpp"
#include "intentproto/hmac.hpp"
#include "intentproto/proto.hpp"

namespace intentproto {

// ---------------- framing v2 ----------------

size_t
frame_v2_encode(uint8_t* out, const uint8_t* payload, size_t payload_len,
                uint8_t seq)
{
    size_t total = payload_len + FRAME_V2_OVERHEAD;
    out[0] = (uint8_t)total;
    out[1] = (uint8_t)(seq | FRAME_V2_FLAG);
    memcpy(out + 2, payload, payload_len);
    // The codeword covers len, seq, and payload
    bch_encode(out, payload_len + 2, out + 2 + payload_len);
    out[total - 1] = MESSAGE_SYNC;
    return total;
}

int
frame_v2_decode(uint8_t* frame, size_t frame_len, const uint8_t** payload,
                uint8_t* seq, int* corrected)
{
    if (frame_len < FRAME_V2_OVERHEAD || frame_len > MESSAGE_MAX + 2)
        return -1;
    if (frame[frame_len - 1] != MESSAGE_SYNC)
        return -1;
    size_t data_len = frame_len - 5; // len+seq+payload
    int fixed = bch_decode(frame, data_len, frame + data_len);
    if (fixed < 0)
        return -1;
    if (frame[0] != frame_len || !(frame[1] & FRAME_V2_FLAG))
        return -1;
    if (corrected)
        *corrected = fixed;
    *seq = frame[1] & MESSAGE_SEQ_MASK;
    *payload = frame + 2;
    return (int)(data_len - 2);
}

// ---------------- datagrams ----------------

void
datagram_tx_init(DatagramTx* tx, const uint8_t* psk, size_t psk_len,
                 uint8_t fec_k)
{
    memset(tx, 0, sizeof(*tx));
    tx->psk = psk;
    tx->psk_len = psk_len;
    tx->k = fec_k;
}

void
datagram_rx_init(DatagramRx* rx, const uint8_t* psk, size_t psk_len)
{
    memset(rx, 0, sizeof(*rx));
    rx->psk = psk;
    rx->psk_len = psk_len;
}

static void
xor_into(uint8_t* dst, size_t* dst_len, const uint8_t* src, size_t len)
{
    size_t i;
    for (i = 0; i < len; i++)
        dst[i] ^= src[i];
    if (len > *dst_len)
        *dst_len = len;
}

static size_t
seal(const uint8_t* psk, size_t psk_len, uint8_t* dgram, size_t len)
{
    if (!psk_len)
        return len; // trust_network: explicitly unauthenticated
    dgram[2] |= DGF_AUTH;
    hmac_sha256_tag(psk, psk_len, dgram, len, dgram + len);
    return len + DATAGRAM_TAG;
}

size_t
datagram_encode(DatagramTx* tx, uint8_t* out, const uint8_t* frames,
                size_t len, TrafficClass cls)
{
    if (len + DATAGRAM_HEADER + DATAGRAM_TAG > DATAGRAM_MAX)
        return 0;
    uint16_t seq = tx->next_seq++;
    out[0] = (uint8_t)(seq >> 8);
    out[1] = (uint8_t)seq;
    out[2] = (uint8_t)cls & DGF_CLASS_MASK;
    memcpy(out + DATAGRAM_HEADER, frames, len);
    size_t body = DATAGRAM_HEADER + len;
    ClassStats* st = &tx->stats[(int)cls];
    st->tx_msgs++;
    st->tx_bytes += (uint32_t)len;
    if (tx->k) {
        // Fold this datagram (header+frames, pre-auth) into the
        // running parity block
        if (!tx->sent_since_parity) {
            memset(tx->parity, 0, sizeof(tx->parity));
            tx->parity_len = 0;
        }
        xor_into(tx->parity, &tx->parity_len, out, body);
        tx->sent_since_parity++;
    }
    return seal(tx->psk, tx->psk_len, out, body);
}

size_t
datagram_parity_flush(DatagramTx* tx, uint8_t* out)
{
    if (!tx->k || tx->sent_since_parity < tx->k)
        return 0;
    uint16_t seq = tx->next_seq++;
    out[0] = (uint8_t)(seq >> 8);
    out[1] = (uint8_t)seq;
    out[2] = DGF_PARITY | (uint8_t)(tx->sent_since_parity & DGF_CLASS_MASK);
    // Parity body covers the protected datagrams' header+frames
    size_t plen = tx->parity_len;
    if (DATAGRAM_HEADER + plen + DATAGRAM_TAG > DATAGRAM_MAX)
        plen = DATAGRAM_MAX - DATAGRAM_HEADER - DATAGRAM_TAG;
    memcpy(out + DATAGRAM_HEADER, tx->parity, plen);
    tx->sent_since_parity = 0;
    return seal(tx->psk, tx->psk_len, out, DATAGRAM_HEADER + plen);
}

int
datagram_decode(DatagramRx* rx, uint8_t* data, size_t len,
                const uint8_t** frames, TrafficClass* cls)
{
    if (len < DATAGRAM_HEADER)
        return -2;
    uint8_t flags = data[2];
    if (rx->psk_len) {
        // Authentication is mandatory outside trust_network mode
        if (!(flags & DGF_AUTH) || len < DATAGRAM_HEADER + DATAGRAM_TAG) {
            rx->auth_failures++;
            return -1;
        }
        size_t body = len - DATAGRAM_TAG;
        uint8_t tag[DATAGRAM_TAG];
        hmac_sha256_tag(rx->psk, rx->psk_len, data, body, tag);
        if (!hmac_tag_equal(tag, data + body)) {
            rx->auth_failures++;
            return -1;
        }
        len = body;
    }
    uint16_t seq = (uint16_t)((data[0] << 8) | data[1]);
    if (!rx->synced) {
        rx->synced = true;
        rx->expect_seq = seq;
    }
    int16_t delta = (int16_t)(seq - rx->expect_seq);
    if (delta < 0) {
        rx->reordered++;
        return 0; // stale duplicate/reorder: already accounted for
    }
    if (delta > 0)
        rx->lost += delta;
    rx->expect_seq = seq + 1;

    if (flags & DGF_PARITY) {
        // Single-loss recovery: if exactly one datagram of the block
        // was lost, XOR of the survivors with the parity rebuilds it.
        // The caller keeps the survivors folded in rx->held.
        if (rx->holding && delta == 1) {
            size_t plen = len - DATAGRAM_HEADER;
            size_t n = plen > rx->held_len ? plen : rx->held_len;
            size_t i;
            for (i = 0; i < n; i++)
                rx->held[i] ^= data[DATAGRAM_HEADER + i];
            rx->held_len = n;
            // rx->held now contains the missing datagram (hdr+frames)
            return 0;
        }
        rx->holding = false;
        rx->held_len = 0;
        return 0;
    }

    // Fold into the survivors buffer for potential parity recovery
    if (!rx->holding) {
        memset(rx->held, 0, sizeof(rx->held));
        rx->held_len = 0;
        rx->holding = true;
    }
    xor_into(rx->held, &rx->held_len, data, len);

    TrafficClass c = (TrafficClass)(flags & DGF_CLASS_MASK);
    ClassStats* st = &rx->stats[(int)c <= 2 ? (int)c : 2];
    st->rx_msgs++;
    st->rx_bytes += (uint32_t)(len - DATAGRAM_HEADER);
    *frames = data + DATAGRAM_HEADER;
    *cls = c;
    return (int)(len - DATAGRAM_HEADER);
}

size_t
datagram_take_recovered(DatagramRx* rx, uint8_t* out, size_t cap)
{
    if (!rx->holding || !rx->held_len || rx->held_len > cap)
        return 0;
    size_t n = rx->held_len;
    memcpy(out, rx->held, n);
    rx->holding = false;
    rx->held_len = 0;
    return n;
}

} // namespace intentproto
