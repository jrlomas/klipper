// intentproto host session: sequence assignment, in-flight window,
// ack/nak handling, go-back-N retransmit. Pure state machine — the
// caller owns timers and I/O (see host.hpp).

#include "intentproto/host.hpp"

namespace intentproto {

void HostSession::init(WriteFn write_fn, void* wuser,
                       ResponseFn response_fn, void* ruser) {
    write = write_fn;
    write_user = wuser;
    on_response = response_fn;
    response_user = ruser;
    send_seq = 0;
    receive_seq = 0;
    last_ack_seq = 0;
    for (size_t i = 0; i < HOST_WINDOW; i++)
        frame_len[i] = 0;
    deadline_set = false;
    deadline = 0;
    nak_pending = false;
    rx_state = RxState::Length;
    rx_pos = 0;
    retransmits = 0;
    naks = 0;
    rx_crc_errors = 0;
    rx_framing_errors = 0;
}

bool HostSession::send_command(const uint8_t* payload, size_t len) {
    if (len > PAYLOAD_MAX || inflight() >= HOST_WINDOW)
        return false;
    uint8_t* f = frames[send_seq % HOST_WINDOW];
    size_t total = len + MESSAGE_MIN;
    f[0] = (uint8_t)total;
    f[1] = (uint8_t)(MESSAGE_DEST | (send_seq & MESSAGE_SEQ_MASK));
    memcpy(f + HEADER_SIZE, payload, len);
    uint16_t crc = crc16_ccitt(f, total - TRAILER_SIZE);
    f[total - 3] = (uint8_t)(crc >> 8);
    f[total - 2] = (uint8_t)(crc & 0xff);
    f[total - 1] = MESSAGE_SYNC;
    frame_len[send_seq % HOST_WINDOW] = (uint8_t)total;
    if (send_seq == receive_seq)
        deadline_set = false;   // window was empty: RTO re-arms fresh
    send_seq++;
    if (write)
        write(f, total, write_user);
    return true;
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
            if (rx_buf[total - 1] != MESSAGE_SYNC) {
                rx_framing_errors++;
                rx_state = RxState::Sync;
                rx_pos = 0;
                break;
            }
            uint16_t want = (uint16_t)((rx_buf[total - 3] << 8)
                                       | rx_buf[total - 2]);
            if (want != crc16_ccitt(rx_buf, total - TRAILER_SIZE)) {
                rx_crc_errors++;
            } else {
                uint64_t rseq = extend_seq(receive_seq,
                                           rx_buf[1] & MESSAGE_SEQ_MASK);
                if (rseq > send_seq) {
                    // Rewound ack: the device nacked a corrupt frame.
                    naks++;
                    if (inflight())
                        nak_pending = true;
                } else if (rseq > receive_seq) {
                    // Ack: frames [receive_seq, rseq) delivered.
                    receive_seq = rseq;
                    deadline_set = false;   // clock restarts on oldest
                    nak_pending = false;
                }
                if (total == MESSAGE_MIN) {
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
                    }
                } else if (on_response) {
                    on_response(rx_buf + HEADER_SIZE,
                                total - MESSAGE_MIN, response_user);
                }
            }
            rx_state = RxState::Length;
            rx_pos = 0;
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
    // Go-back-N: resend every unacked frame in order, preceded by a
    // sync byte so a receiver stuck mid-frame can resynchronize.
    if (write) {
        const uint8_t sync = MESSAGE_SYNC;
        write(&sync, 1, write_user);
        for (uint64_t s = receive_seq; s != send_seq; s++)
            write(frames[s % HOST_WINDOW], frame_len[s % HOST_WINDOW],
                  write_user);
    }
    retransmits++;
    nak_pending = false;
    deadline = now_ticks + rto_ticks;
    deadline_set = true;
    return true;
}

} // namespace intentproto
