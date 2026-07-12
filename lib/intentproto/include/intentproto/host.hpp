#ifndef INTENTPROTO_HOST_HPP
#define INTENTPROTO_HOST_HPP
// intentproto host session — the retransmit-window state machine for
// the host side of the legacy protocol (RFC 0001 doc 10). This is
// the library's counterpart to the device side in proto.cpp: it
// assigns sequence numbers, frames commands, tracks the in-flight
// window, and turns acks/naks/timeouts into go-back-N retransmits.
//
// Pure state machine, freestanding profile: no heap, no exceptions,
// no RTTI, no I/O, and no time reads — the caller owns timers and
// the transport. Time enters only as the now_ticks argument of
// need_retransmit(), in whatever unit the caller likes (the same
// unit as rto_ticks).
//
// Legacy semantics implemented here: frame sequence numbers are
// 4-bit on the wire (extended to 64-bit internally); the device acks
// with the next sequence it expects, so one ack covers everything
// before it; an empty frame that does not advance the window — or
// one whose sequence rewinds past it, which is how the device side
// naks a corrupt frame — requests an immediate go-back-N retransmit.
// The window must stay smaller than the 16-value sequence space to
// keep acks unambiguous; 12 matches the reference host.

#include "proto.hpp"

namespace intentproto {

// Maximum unacked frames in flight (< 16, see above).
constexpr size_t HOST_WINDOW = 12;

struct HostSession {
    // Transport transmit hook: must write len bytes (a whole frame).
    using WriteFn = int (*)(const uint8_t* data, size_t len, void* user);
    // Called from on_rx() for every received message frame with the
    // frame's payload (msgid + args, VLQ encoded). Ack-only frames
    // are consumed internally.
    using ResponseFn = void (*)(const uint8_t* payload, size_t len,
                                void* user);

    // ---- state (read-only outside; mutate via the methods) ----
    WriteFn write;
    void* write_user;
    ResponseFn on_response;
    void* response_user;

    // Sequence numbers, 64-bit; the low 4 bits go on the wire.
    uint64_t send_seq;      // sequence of the next frame to send
    uint64_t receive_seq;   // lowest unacked (everything below acked)
    uint64_t last_ack_seq;  // highest empty-frame ack seen (nak detect)

    // In-flight frames, a ring indexed by sequence % HOST_WINDOW.
    uint8_t frames[HOST_WINDOW][MESSAGE_MAX];
    uint8_t frame_len[HOST_WINDOW];

    // Retransmit bookkeeping — caller-supplied time only.
    bool deadline_set;
    uint64_t deadline;      // now + rto when the oldest frame was seen
    bool nak_pending;       // device asked for an immediate retransmit

    // Frame receive state machine (device -> host bytes).
    enum class RxState : uint8_t { Sync, Length, Body };
    RxState rx_state;
    uint8_t rx_buf[MESSAGE_MAX];
    size_t rx_pos;

    // Diagnostics.
    uint32_t retransmits;
    uint32_t naks;
    uint32_t rx_crc_errors;
    uint32_t rx_framing_errors;

    // ---- API ----
    // Reset all state and install the callbacks (either response_fn
    // or the write hook may be nullptr for one-way tests).
    void init(WriteFn write_fn, void* wuser,
              ResponseFn response_fn, void* ruser);

    // Frame a command payload (msgid + args, VLQ encoded, at most
    // PAYLOAD_MAX bytes), assign the next sequence number, transmit,
    // and hold it for retransmission until acked. Returns false —
    // sending nothing — when the window is full or the payload is
    // oversized; the caller retries after acks arrive.
    bool send_command(const uint8_t* payload, size_t len);

    // Feed raw link bytes in any chunking. Complete CRC-valid frames
    // update the window (every device frame carries the next
    // expected sequence); message payloads are delivered through
    // on_response from inside this call; naks arm nak_pending.
    void on_rx(const uint8_t* data, size_t len);

    // Caller polls this from its timer loop. If a nak is pending or
    // the oldest unacked frame has been waiting longer than
    // rto_ticks, every in-flight frame is retransmitted in order
    // (go-back-N) through the write hook and true is returned. The
    // deadline re-arms lazily: the first poll after the window
    // becomes (or advances while) non-empty starts the clock.
    bool need_retransmit(uint64_t now_ticks, uint64_t rto_ticks);

    // Frames sent and not yet acked.
    size_t inflight() const { return (size_t)(send_seq - receive_seq); }
};

} // namespace intentproto

#endif // INTENTPROTO_HOST_HPP
