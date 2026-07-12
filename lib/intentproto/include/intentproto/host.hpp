#ifndef INTENTPROTO_HOST_HPP
#define INTENTPROTO_HOST_HPP
// intentproto host session — the retransmit-window state machine for
// the host side of the legacy protocol (FD-0001 doc 10). This is
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
//
// Framing negotiation (FD-0001 doc 07): rx accepts both framings at
// all times; tx framing follows a three-state machine —
//
//   Legacy ---session_enable_v2()---> Probing ---valid v2 rx---> V2
//     ^                                  |
//     +--- HOST_V2_PROBE_LIMIT consecutive rejections (naks or RTO
//          retransmits) with zero v2 replies; v2_rejected latched --+
//
// * Legacy: the default, the bootstrap format, and the permanent
//   fallback. The session never parses dictionaries — after reading
//   FRAMING_V2 from the identify blob the CALLER promotes the link
//   with session_enable_v2().
// * Probing: tx frames carry the BCH trailer and the FRAME_V2_FLAG
//   seq bit that legacy peers provably reject-and-nak, so probing is
//   safe: a legacy peer keeps the payload flowing via retransmit. A
//   peer that answers with any valid v2 frame confirms the upgrade;
//   one that only naks (or stays silent past the RTO) for
//   HOST_V2_PROBE_LIMIT consecutive rounds is a legacy peer — the
//   session falls back to Legacy automatically, latches v2_rejected
//   for the caller to inspect, and retransmits in legacy framing.
// * V2: sticky — once the peer has spoken v2 the session never
//   auto-downgrades (only init() resets it).
//
// In-flight frames are stored as payloads and (re)framed per the
// current tx framing at every (re)transmit, so a probe fallback
// re-sends the same window in legacy framing.

#include "proto.hpp"
#include "datagram.hpp"

namespace intentproto {

// Maximum unacked frames in flight (< 16, see above).
constexpr size_t HOST_WINDOW = 12;
// Consecutive rejections of unanswered v2 probes before fallback.
constexpr uint32_t HOST_V2_PROBE_LIMIT = 4;

struct HostSession {
    // Transport transmit hook: must write len bytes (a whole frame).
    using WriteFn = int (*)(const uint8_t* data, size_t len, void* user);
    // Called from on_rx() for every received message frame with the
    // frame's payload (msgid + args, VLQ encoded). Ack-only frames
    // are consumed internally.
    using ResponseFn = void (*)(const uint8_t* payload, size_t len,
                                void* user);

    // Tx framing state (see the state machine above).
    enum class Framing : uint8_t { Legacy, Probing, V2 };

    // ---- state (read-only outside; mutate via the methods) ----
    WriteFn write;
    void* write_user;
    ResponseFn on_response;
    void* response_user;

    // Sequence numbers, 64-bit; the low 4 bits go on the wire.
    uint64_t send_seq;      // sequence of the next frame to send
    uint64_t receive_seq;   // lowest unacked (everything below acked)
    uint64_t last_ack_seq;  // highest empty-frame ack seen (nak detect)

    // In-flight payloads, a ring indexed by sequence % HOST_WINDOW;
    // framed at transmit time per the current framing.
    uint8_t payloads[HOST_WINDOW][MESSAGE_MAX];
    uint8_t payload_len[HOST_WINDOW];
    TrafficClass classes[HOST_WINDOW];

    // Retransmit bookkeeping — caller-supplied time only.
    bool deadline_set;
    uint64_t deadline;      // now + rto when the oldest frame was seen
    bool nak_pending;       // device asked for an immediate retransmit

    // Frame receive state machine (device -> host bytes).
    enum class RxState : uint8_t { Sync, Length, Body };
    RxState rx_state;
    uint8_t rx_buf[MESSAGE_MAX];
    size_t rx_pos;

    // Framing negotiation.
    Framing framing;
    bool v2_rejected;       // probe fell back to legacy (stats latch)
    uint32_t probe_naks;    // consecutive rejections while Probing
    uint32_t v2_frames_rx;  // valid v2 frames received

    // Diagnostics.
    uint32_t retransmits;
    uint32_t naks;
    uint32_t rx_crc_errors;
    uint32_t rx_bch_errors; // uncorrectable v2 frames (dropped)
    uint32_t rx_framing_errors;
    // Per-class tx accounting, indexed by TrafficClass.
    ClassStats class_stats[3];

    // ---- API ----
    // Reset all state and install the callbacks (either response_fn
    // or the write hook may be nullptr for one-way tests). desired
    // is the initial tx framing knob: Legacy (the default) starts a
    // compatible session; anything else starts Probing immediately —
    // for transports known to be v2 without a dictionary round-trip.
    void init(WriteFn write_fn, void* wuser,
              ResponseFn response_fn, void* ruser,
              Framing desired = Framing::Legacy);

    // Frame a command payload (msgid + args, VLQ encoded, at most
    // PAYLOAD_MAX bytes — two fewer under v2 framing), assign the
    // next sequence number, transmit, and hold it for retransmission
    // until acked. The traffic class is recorded per in-flight frame
    // (see class_of) and accounted in class_stats. Returns false —
    // sending nothing — when the window is full or the payload is
    // oversized; the caller retries after acks arrive.
    bool send_command(const uint8_t* payload, size_t len,
                      TrafficClass cls = TrafficClass::Scheduled);

    // Promote the link to framing v2 (the caller saw FRAMING_V2 in
    // the dictionary — or wants to probe blind, which is safe, see
    // above). Enters Probing and clears v2_rejected; idempotent when
    // already Probing/V2. Returns false only when an in-flight
    // legacy payload is too large to re-frame with the v2 overhead
    // (retry once the window drains).
    bool session_enable_v2();

    // Feed raw link bytes in any chunking. Complete valid frames —
    // legacy CRC or v2 BCH, both accepted at all times — update the
    // window (every device frame carries the next expected
    // sequence); message payloads are delivered through on_response
    // from inside this call; naks arm nak_pending.
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

    // Traffic class recorded for an in-flight sequence number — a
    // datagram transport binding maps frames to datagram classes
    // with this (valid for receive_seq <= seq < send_seq).
    TrafficClass class_of(uint64_t seq) const {
        return classes[seq % HOST_WINDOW];
    }

    // ---- internal helpers (public struct, library use) ----
    void xmit(uint64_t seq);
    void note_probe_reject();
};

} // namespace intentproto

#endif // INTENTPROTO_HOST_HPP
