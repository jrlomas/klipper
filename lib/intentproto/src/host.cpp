// intentproto host session: sequence assignment, in-flight window,
// ack/nak handling, go-back-N retransmit, framing v2 negotiation.
// Pure state machine — the caller owns timers and I/O (see host.hpp).

#include "intentproto/host.hpp"

namespace intentproto {

namespace {

// Largest payload the given tx framing fits in a MESSAGE_MAX frame.
size_t payload_cap(HostSession::Framing f) {
    return f == HostSession::Framing::Legacy
               ? PAYLOAD_MAX
               : MESSAGE_MAX - FRAME_V2_OVERHEAD;
}

} // namespace

void HostSession::init(WriteFn write_fn, void* wuser,
                       ResponseFn response_fn, void* ruser,
                       Framing desired) {
    write = write_fn;
    write_user = wuser;
    on_response = response_fn;
    response_user = ruser;
    send_seq = 0;
    receive_seq = 0;
    last_ack_seq = 0;
    for (size_t i = 0; i < HOST_WINDOW; i++) {
        payload_len[i] = 0;
        classes[i] = TrafficClass::Scheduled;
    }
    deadline_set = false;
    deadline = 0;
    nak_pending = false;
    bootstrap_sync = true;
    bootstrap_nak_seq = 0;
    bootstrap_nak_count = 0;
    rx_state = RxState::Length;
    rx_pos = 0;
    framing = desired == Framing::Legacy ? Framing::Legacy
                                         : Framing::Probing;
    v2_rejected = false;
    probe_naks = 0;
    v2_frames_rx = 0;
    retransmits = 0;
    naks = 0;
    sequence_rebases = 0;
    rx_crc_errors = 0;
    rx_bch_errors = 0;
    rx_framing_errors = 0;
    for (size_t i = 0; i < 3; i++)
        class_stats[i] = ClassStats{};
}

// (Re)frame one in-flight payload per the current framing and write
// it. Framing at transmit time is what lets a probe fallback resend
// the same window in legacy framing.
void HostSession::xmit(uint64_t seq) {
    size_t w = seq % HOST_WINDOW;
    uint8_t seq_byte = (uint8_t)(MESSAGE_DEST | (seq & MESSAGE_SEQ_MASK));
    uint8_t f[MESSAGE_MAX];
    size_t total;
    if (framing == Framing::Legacy) {
        total = (size_t)payload_len[w] + MESSAGE_MIN;
        f[0] = (uint8_t)total;
        f[1] = seq_byte;
        memcpy(f + HEADER_SIZE, payloads[w], payload_len[w]);
        uint16_t crc = crc16_ccitt(f, total - TRAILER_SIZE);
        f[total - 3] = (uint8_t)(crc >> 8);
        f[total - 2] = (uint8_t)(crc & 0xff);
        f[total - 1] = MESSAGE_SYNC;
    } else {
        total = frame_v2_encode(f, payloads[w], payload_len[w], seq_byte);
    }
    if (write)
        write(f, total, write_user);
}

// Move the unacknowledged payload window to start at the sequence a retained
// peer expects.  Payloads are copied because the old and new modulo windows
// may overlap.  This is only called during bootstrap, before any frame from
// this HostSession has been accepted by the peer.
void HostSession::rebase_window(uint64_t seq) {
    const size_t count = inflight();
    uint8_t saved_payloads[HOST_WINDOW][MESSAGE_MAX];
    uint8_t saved_len[HOST_WINDOW];
    TrafficClass saved_classes[HOST_WINDOW];
    for (size_t i = 0; i < count; i++) {
        const size_t old = (receive_seq + i) % HOST_WINDOW;
        saved_len[i] = payload_len[old];
        saved_classes[i] = classes[old];
        memcpy(saved_payloads[i], payloads[old], saved_len[i]);
    }
    receive_seq = seq;
    send_seq = seq + count;
    last_ack_seq = seq;
    for (size_t i = 0; i < count; i++) {
        const size_t next = (seq + i) % HOST_WINDOW;
        payload_len[next] = saved_len[i];
        classes[next] = saved_classes[i];
        memcpy(payloads[next], saved_payloads[i], saved_len[i]);
    }
    deadline_set = false;
    nak_pending = false;
    bootstrap_sync = false;
    bootstrap_nak_count = 0;
    sequence_rebases++;
    for (uint64_t s = receive_seq; s != send_seq; s++)
        xmit(s);
}

bool HostSession::send_command(const uint8_t* payload, size_t len,
                               TrafficClass cls) {
    if (len > payload_cap(framing) || inflight() >= HOST_WINDOW)
        return false;
    size_t w = send_seq % HOST_WINDOW;
    memcpy(payloads[w], payload, len);
    payload_len[w] = (uint8_t)len;
    classes[w] = cls;
    ClassStats* st = &class_stats[(int)cls <= 2 ? (int)cls : 2];
    st->tx_msgs++;
    st->tx_bytes += (uint32_t)len;
    if (send_seq == receive_seq)
        deadline_set = false;   // window was empty: RTO re-arms fresh
    uint64_t seq = send_seq++;
    xmit(seq);
    return true;
}

bool HostSession::session_enable_v2() {
    if (framing != Framing::Legacy)
        return true;            // already probing or confirmed
    // In-flight payloads are re-framed on retransmit; refuse the
    // upgrade while one is too large for the bigger v2 overhead.
    for (uint64_t s = receive_seq; s != send_seq; s++)
        if (payload_len[s % HOST_WINDOW]
            > MESSAGE_MAX - FRAME_V2_OVERHEAD)
            return false;
    framing = Framing::Probing;
    v2_rejected = false;
    probe_naks = 0;
    return true;
}

// One round of probe rejection (a nak, or an RTO expiry with no v2
// reply). A legacy peer naks every v2 frame and can never answer in
// v2, so enough consecutive rejections mean fall back — the pending
// retransmit then goes out in legacy framing and traffic resumes.
void HostSession::note_probe_reject() {
    if (framing != Framing::Probing)
        return;
    if (++probe_naks >= HOST_V2_PROBE_LIMIT) {
        framing = Framing::Legacy;
        v2_rejected = true;     // latched for the caller to inspect
        probe_naks = 0;
    }
}

namespace {

// A device frame's seq byte holds the low 4 bits of the next
// sequence it expects; extend it against the window. Values beyond
// send_seq cannot be real acks — the device side signals a nak by
// rewinding its ack sequence, which lands here (the window being
// < 16 keeps the two cases unambiguous).
uint64_t extend_seq(uint64_t receive_seq, uint8_t nibble) {
    uint8_t base = (uint8_t)(receive_seq & MESSAGE_SEQ_MASK);
    return receive_seq + (uint8_t)((nibble - base) & MESSAGE_SEQ_MASK);
}

} // namespace

void HostSession::on_rx(const uint8_t* data, size_t len) {
    while (len--) {
        uint8_t byte = *data++;
        switch (rx_state) {
        case RxState::Sync:
            if (byte == MESSAGE_SYNC)
                rx_state = RxState::Length;
            break;
        case RxState::Length:
            if (byte == MESSAGE_SYNC)
                break;                      // idle syncs between frames
            if (byte < MESSAGE_MIN || byte > MESSAGE_MAX) {
                rx_framing_errors++;
                rx_state = RxState::Sync;
                break;
            }
            rx_buf[0] = byte;
            rx_pos = 1;
            rx_state = RxState::Body;
            break;
        case RxState::Body: {
            rx_buf[rx_pos++] = byte;
            size_t total = rx_buf[0];
            if (rx_pos < total)
                break;
            rx_state = RxState::Length;
            rx_pos = 0;
            if (rx_buf[total - 1] != MESSAGE_SYNC) {
                rx_framing_errors++;
                rx_state = RxState::Sync;
                break;
            }
            // Both framings accepted at all times; the seq byte's
            // FRAME_V2_FLAG says which trailer to check.
            uint8_t seq_nibble;
            const uint8_t* payload;
            size_t plen;
            if (rx_buf[1] & FRAME_V2_FLAG) {
                uint8_t sq;
                int n = frame_v2_decode(rx_buf, total, &payload, &sq);
                if (n < 0) {
                    rx_bch_errors++;    // uncorrectable: ARQ recovers
                    break;
                }
                v2_frames_rx++;
                if (framing == Framing::Probing) {
                    // The peer answered in v2: upgrade confirmed,
                    // and it is sticky from here on.
                    framing = Framing::V2;
                    probe_naks = 0;
                }
                seq_nibble = sq;
                plen = (size_t)n;
            } else {
                uint16_t want = (uint16_t)((rx_buf[total - 3] << 8)
                                           | rx_buf[total - 2]);
                if (want != crc16_ccitt(rx_buf, total - TRAILER_SIZE)) {
                    rx_crc_errors++;
                    break;
                }
                seq_nibble = rx_buf[1] & MESSAGE_SEQ_MASK;
                payload = rx_buf + HEADER_SIZE;
                plen = total - MESSAGE_MIN;
            }
            uint64_t rseq = extend_seq(receive_seq, seq_nibble);
            if (rseq > send_seq) {
                // Rewound ack: the device nacked a corrupt frame.
                naks++;
                if (inflight()) {
                    // There is no distinct wire opcode for a corrupt-frame
                    // nak and a retained peer saying "I expect sequence N".
                    // One repeated future nak disambiguates them: a clean
                    // retransmit resolves corruption, while a stale sequence
                    // peer repeats N.  Adoption is bootstrap-only.
                    uint8_t wire_seq = seq_nibble & MESSAGE_SEQ_MASK;
                    if (plen == 0 && bootstrap_sync
                        && bootstrap_nak_count
                        && bootstrap_nak_seq == wire_seq) {
                        rebase_window(rseq);
                        break;
                    }
                    if (plen == 0 && bootstrap_sync) {
                        bootstrap_nak_seq = wire_seq;
                        bootstrap_nak_count = 1;
                    }
                    nak_pending = true;
                    note_probe_reject();
                }
            } else if (rseq > receive_seq) {
                // Ack: frames [receive_seq, rseq) delivered.
                receive_seq = rseq;
                deadline_set = false;   // clock restarts on oldest
                nak_pending = false;
                bootstrap_sync = false;
                bootstrap_nak_count = 0;
            } else if (plen != 0) {
                // Any valid response proves that this host's sequence space
                // is already accepted, even if an earlier frame drained the
                // window before this response arrived.
                bootstrap_sync = false;
                bootstrap_nak_count = 0;
            }
            if (plen == 0) {
                // Empty frame: pure ack — a duplicate of one we
                // already saw is the other nak form. Rewound
                // frames were counted above and are not acks.
                if (rseq > send_seq)
                    ;
                else if (rseq > last_ack_seq)
                    last_ack_seq = rseq;
                else if (inflight()) {
                    naks++;
                    nak_pending = true;
                    note_probe_reject();
                }
            } else if (on_response) {
                on_response(payload, plen, response_user);
            }
            break;
        }
        }
    }
}

bool HostSession::need_retransmit(uint64_t now_ticks, uint64_t rto_ticks) {
    if (!inflight()) {
        deadline_set = false;
        nak_pending = false;
        return false;
    }
    if (!deadline_set) {
        deadline = now_ticks + rto_ticks;
        deadline_set = true;
    }
    if (!nak_pending && now_ticks < deadline)
        return false;
    // A silent peer stuck on a v2 probe counts as a rejection too
    // (nak-driven rounds were already counted when the nak arrived).
    if (!nak_pending)
        note_probe_reject();
    // Go-back-N: resend every unacked frame in order, preceded by a
    // sync byte so a receiver stuck mid-frame can resynchronize.
    if (write) {
        const uint8_t sync = MESSAGE_SYNC;
        write(&sync, 1, write_user);
    }
    for (uint64_t s = receive_seq; s != send_seq; s++)
        xmit(s);
    retransmits++;
    nak_pending = false;
    deadline = now_ticks + rto_ticks;
    deadline_set = true;
    return true;
}

} // namespace intentproto
