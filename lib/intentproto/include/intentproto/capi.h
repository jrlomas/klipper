#ifndef INTENTPROTO_CAPI_H
#define INTENTPROTO_CAPI_H
/* intentproto host-profile C API (FD-0001 doc 10, "host profile").
 *
 * This is the library's stable, C-linkage surface: the seam a host
 * (klippy's transmit machinery, a third-party C firmware, a test
 * harness) speaks the protocol through WITHOUT touching the C++ core.
 * It is a thin shim over the freestanding C++ implementation carried
 * in the intentproto headers.
 *
 * Two things this header buys the host profile that the embedded
 * profile does not need (each function here forwards to the matching
 * C++ entry point in the intentproto headers; no protocol logic lives
 * in the shim):
 *   * convenience allocation (ip_host_session_create / _free,
 *     ip_datagram_tx_create / _free) — the host may heap-allocate the
 *     otherwise caller-owned state structs;
 *   * a real, versioned, documented ABI (this file) plus a Python
 *     binding generated from it (python/intentproto), replacing the
 *     stringly-typed FFI the legacy host carries.
 *
 * The core stays pure: no I/O, no timers, no allocation inside the
 * protocol itself — the caller still owns the transport (the write
 * callbacks) and the clock (the now/rto arguments of
 * ip_host_session_need_retransmit).
 *
 * Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
 * MIT licensed, as the whole library (see LICENSE): a permissive C
 * ABI is the point — vendors bind to it without inheriting the GPL.
 */

#include <stddef.h>
#include <stdint.h>

/* ---- ABI versioning ----
 * Semantic ABI version, packed (major << 16) | (minor << 8) | patch.
 * Bump MAJOR for any breaking change to a signature or struct layout
 * below, MINOR for backward-compatible additions, PATCH for shim-only
 * fixes. A binding compares its compiled-in INTENTPROTO_ABI_VERSION
 * against the runtime intentproto_abi_version() and refuses a MAJOR
 * mismatch. */
#define INTENTPROTO_ABI_VERSION_MAJOR 1
#define INTENTPROTO_ABI_VERSION_MINOR 3
#define INTENTPROTO_ABI_VERSION_PATCH 0
#define INTENTPROTO_ABI_VERSION                                         \
    ((INTENTPROTO_ABI_VERSION_MAJOR << 16)                             \
     | (INTENTPROTO_ABI_VERSION_MINOR << 8)                            \
     | INTENTPROTO_ABI_VERSION_PATCH)

#ifdef __cplusplus
extern "C" {
#endif

/* The ABI version the shared object was actually built with. */
uint32_t intentproto_abi_version(void);
/* Human-readable library version string (build metadata). */
const char *intentproto_version_string(void);

/* ---- wire limits (mirror proto.hpp constants) ---- */
#define IP_MESSAGE_MAX 64
#define IP_PAYLOAD_MAX 59  /* MESSAGE_MAX - MESSAGE_MIN */

/* Traffic classes (FD-0001 doc 03), matching TrafficClass. */
#define IP_CLASS_SCHEDULED 0
#define IP_CLASS_PROMPT    1
#define IP_CLASS_TELEMETRY 2

/* Per-class tx/rx accounting, layout-compatible with ClassStats. */
typedef struct ip_class_stats {
    uint32_t tx_msgs, tx_bytes;
    uint32_t rx_msgs, rx_bytes;
    uint32_t dropped;
} ip_class_stats;

/* ================================================================
 * Framing primitives (stateless codecs)
 * ================================================================ */

/* CRC-16/CCITT-FALSE over len bytes (legacy frame trailer). */
uint16_t ip_crc16_ccitt(const uint8_t *buf, size_t len);

/* VLQ encode v into out (out must hold >= 5 bytes); returns the
 * number of bytes written. */
size_t ip_vlq_encode(uint8_t *out, uint32_t v);
/* VLQ decode from in[0..len); on success writes *out and returns the
 * number of bytes consumed (1..5), or 0 on a truncated input. */
size_t ip_vlq_decode(const uint8_t *in, size_t len, uint32_t *out);

/* Framing v2 (BCH t=3 trailer, FD-0001 doc 07). Encode payload
 * (<= 57 bytes) into out (>= payload_len + 7); returns frame length. */
size_t ip_frame_v2_encode(uint8_t *out, const uint8_t *payload,
                          size_t payload_len, uint8_t seq);
/* Decode (and error-correct in place) a v2 frame. On success returns
 * the payload length, sets *payload_off to the payload's byte offset
 * within frame, sets *seq, and (when corrected != NULL) *corrected to
 * the number of repaired bit errors. Returns -1 if uncorrectable. */
int ip_frame_v2_decode(uint8_t *frame, size_t frame_len,
                       size_t *payload_off, uint8_t *seq, int *corrected);

/* ================================================================
 * Trajectory segment codec (FD-0001 doc 02)
 * ================================================================
 * Helpers a third-party trajectory peer needs to emit queue_traj_segment
 * payloads and track chained position identically to the MCU. The
 * quantizer and end-delta arithmetic are bit-for-bit identical to
 * src/trajq.c and klippy/chelper/segfit.c (guarded by the test suite). */

/* Segment polynomial-order flags (mirror src/trajq.h TSEG_*). */
#define IP_SEG_HOLD_AT_END    (1 << 0)
#define IP_SEG_POLY_MASK      (3 << 6)
#define IP_SEG_POLY_QUADRATIC (0 << 6)
#define IP_SEG_POLY_CUBIC     (1 << 6)
#define IP_SEG_POLY_QUINTIC   (2 << 6)

/* Decoded segment kind (ip_segment.kind / ip_segment_decode return). */
#define IP_SEG_KIND_NONE    0
#define IP_SEG_KIND_SEGMENT 1
#define IP_SEG_KIND_HOLD    2

/* Decoded queue_traj_segment / traj_hold fields. */
typedef struct ip_segment {
    int kind;
    uint8_t oid;
    uint8_t flags;
    uint32_t duration;
    int32_t velocity, accel, jerk, snap, crackle;
} ip_segment;

/* Quantize a true per-tick derivative to its wire int32 (scale 2^(16*k),
 * round half away from zero, saturated to int32). order_k: 1=velocity ..
 * 5=crackle. */
int32_t ip_segment_quantize(double true_value, unsigned order_k);

/* Exact Q32.32 sub-unit end-of-segment position delta for the quantized
 * coefficients over duration ticks. Unused higher orders pass 0. */
int64_t ip_segment_end_delta(uint32_t duration, int32_t velocity,
                             int32_t accel, int32_t jerk, int32_t snap,
                             int32_t crackle);

/* Advance a Q32.32 chained accumulator by one segment. Returns the new
 * integer sub-unit position (new_acc >> 32); if new_acc != NULL, writes
 * the full Q32.32 accumulator there. */
int64_t ip_segment_chain_advance(int64_t acc, uint32_t duration,
                                 int32_t velocity, int32_t accel,
                                 int32_t jerk, int32_t snap, int32_t crackle,
                                 int64_t *new_acc);

/* Encode a queue_traj_segment payload (msgid + oid + flags + duration +
 * coefficients; coefficient count follows the flags' polynomial order)
 * into out. Returns bytes written, or 0 on a reserved order / short cap. */
size_t ip_segment_encode(uint8_t *out, size_t cap, uint8_t oid, uint8_t flags,
                         uint32_t duration, int32_t velocity, int32_t accel,
                         int32_t jerk, int32_t snap, int32_t crackle);
/* Encode a traj_hold payload (msgid + oid + duration). */
size_t ip_segment_encode_hold(uint8_t *out, size_t cap, uint8_t oid,
                              uint32_t duration);
/* Decode a queue_traj_segment / traj_hold payload into *seg; returns the
 * kind (IP_SEG_KIND_*) or IP_SEG_KIND_NONE on a foreign/short payload. */
int ip_segment_decode(const uint8_t *in, size_t len, ip_segment *seg);

/* ================================================================
 * Host session — the retransmit-window state machine (host.hpp)
 * ================================================================ */

/* Transport transmit hook: write len bytes (a whole frame). */
typedef int (*ip_write_fn)(const uint8_t *data, size_t len, void *user);
/* Delivered once per received message frame (payload = msgid + args,
 * VLQ encoded); ack-only frames are consumed internally. */
typedef void (*ip_response_fn)(const uint8_t *payload, size_t len,
                               void *user);

/* Initial tx framing knob (matches HostSession::Framing). */
#define IP_FRAMING_LEGACY  0
#define IP_FRAMING_PROBING 1

typedef struct ip_host_session ip_host_session;

/* Allocate and initialize a host session. Either callback may be NULL
 * for one-way tests. desired_framing is IP_FRAMING_LEGACY (default,
 * compatible bootstrap) or IP_FRAMING_PROBING (probe v2 immediately).
 * Returns NULL on allocation failure. */
ip_host_session *ip_host_session_create(ip_write_fn write_fn, void *wuser,
                                        ip_response_fn response_fn,
                                        void *ruser, int desired_framing);
void ip_host_session_free(ip_host_session *h);

/* Frame payload (msgid + VLQ args), assign the next sequence, transmit
 * through the write hook, and hold it for retransmit until acked.
 * cls is one of IP_CLASS_*. Returns 1 on success, 0 when the window is
 * full or the payload is oversized (retry after acks arrive). */
int ip_host_session_send_command(ip_host_session *h, const uint8_t *payload,
                                 size_t len, int cls);

/* Feed raw link bytes (any chunking). Complete frames update the
 * window; message payloads are delivered through the response hook
 * from inside this call. */
void ip_host_session_on_rx(ip_host_session *h, const uint8_t *data,
                           size_t len);

/* Poll from the caller's timer loop. Retransmits the whole in-flight
 * window (go-back-N) and returns 1 when a nak is pending or the oldest
 * frame has waited longer than rto_ticks; else returns 0. now_ticks
 * and rto_ticks are in the caller's own time unit. */
int ip_host_session_need_retransmit(ip_host_session *h, uint64_t now_ticks,
                                    uint64_t rto_ticks);

/* Promote the link to framing v2 (enter Probing). Returns 1 on
 * success, 0 if an in-flight legacy payload cannot be re-framed with
 * the v2 overhead (retry once the window drains). */
int ip_host_session_enable_v2(ip_host_session *h);

/* Frames sent and not yet acked. */
size_t ip_host_session_inflight(const ip_host_session *h);

/* Copy the per-class tx/rx accounting for class cls (IP_CLASS_*). */
void ip_host_session_class_stats(const ip_host_session *h, int cls,
                                 ip_class_stats *out);

/* Link diagnostics latched by the session. */
typedef struct ip_host_diag {
    uint32_t retransmits;
    uint32_t naks;
    uint32_t rx_crc_errors;
    uint32_t rx_bch_errors;
    uint32_t rx_framing_errors;
    uint32_t v2_frames_rx;
    int v2_rejected;   /* probe fell back to legacy */
    int framing_v2;    /* tx framing has latched to v2 */
} ip_host_diag;
void ip_host_session_diag(const ip_host_session *h, ip_host_diag *out);
/* Added in ABI 1.3 without extending ip_host_diag's caller-owned layout. */
uint32_t ip_host_session_sequence_rebases(const ip_host_session *h);

/* ================================================================
 * Datagram transport binding (datagram.hpp, FD-0001 doc 07)
 * ================================================================ */

typedef struct ip_datagram_tx ip_datagram_tx;
typedef struct ip_datagram_rx ip_datagram_rx;

/* psk NULL / psk_len 0 selects trust_network (unauthenticated) mode.
 * fec_k 2 emits one XOR parity datagram per protected pair; 0 disables
 * FEC. Other values are invalid and make encode return 0. */
ip_datagram_tx *ip_datagram_tx_create(const uint8_t *psk, size_t psk_len,
                                      uint8_t fec_k);
void ip_datagram_tx_free(ip_datagram_tx *tx);
ip_datagram_rx *ip_datagram_rx_create(const uint8_t *psk, size_t psk_len);
void ip_datagram_rx_free(ip_datagram_rx *rx);

/* Wrap already-framed bytes into a datagram in out (>= len + 11).
 * Returns the datagram size. */
size_t ip_datagram_encode(ip_datagram_tx *tx, uint8_t *out,
                          const uint8_t *frames, size_t len, int cls);
/* If FEC is on and due, emit a parity datagram into out and return its
 * size; else 0. Call after each ip_datagram_encode. */
size_t ip_datagram_parity_flush(ip_datagram_tx *tx, uint8_t *out);

/* Authenticate + sequence-check a received datagram. On success
 * returns the frames' length and sets *frames_off (the frames' byte
 * offset within data) and *cls. Returns -1 on auth failure, -2 on
 * malformed input, 0 when consumed internally (e.g. a parity
 * datagram). */
int ip_datagram_decode(ip_datagram_rx *rx, uint8_t *data, size_t len,
                       size_t *frames_off, int *cls);
/* After a decode that recovered a lost datagram via parity, copy the next
 * ready datagram into out; call until 0 because first-packet loss yields
 * recovered-first followed by the deferred survivor. */
size_t ip_datagram_take_recovered(ip_datagram_rx *rx, uint8_t *out,
                                  size_t cap);

/* ================================================================
 * Secure session (session_sec.hpp): the DTLS-class authenticated
 * session over the datagram transport — HKDF session keys, epoch
 * rotation, per-board identity, replay window. Auth-only.
 * ================================================================ */
typedef struct ip_secure_session ip_secure_session;
/* role: 1 = initiator (host), 0 = responder (board). my_random is
 * SEC_RANDOM_SIZE (16) fresh bytes from the caller's RNG. rekey = 0
 * selects the default auto-rekey threshold. */
ip_secure_session *ip_secure_session_create(int is_initiator,
                                            const uint8_t *psk,
                                            size_t psk_len,
                                            const uint8_t *board_id,
                                            size_t id_len,
                                            const uint8_t *my_random16,
                                            uint32_t rekey);
void ip_secure_session_free(ip_secure_session *s);
/* Initiator: write the ClientHello into out; returns its length. */
size_t ip_secure_session_start(ip_secure_session *s, uint8_t *out,
                               size_t cap);
/* Feed one received handshake message; any reply is written to out
 * and its length returned (0 if none). */
size_t ip_secure_session_on_handshake(ip_secure_session *s,
                                      const uint8_t *msg, size_t len,
                                      uint8_t *out, size_t cap);
int ip_secure_session_established(const ip_secure_session *s);
int ip_secure_session_failed(const ip_secure_session *s);
/* Peer identity (valid once hellos exchanged); returns its length. */
size_t ip_secure_session_peer_id(const ip_secure_session *s,
                                 uint8_t *out, size_t cap);
/* Seal frames into a session datagram; returns its size, or 0. */
size_t ip_secure_session_encode(ip_secure_session *s, uint8_t *out,
                                size_t cap, const uint8_t *frames,
                                size_t len, int cls);
/* Unwrap a session datagram: returns the frames' length (with
 * *frames_off the offset within data) and *cls; -1 auth failure, -2
 * malformed/not-a-session-datagram, -3 replay/stale epoch. */
int ip_secure_session_decode(ip_secure_session *s, uint8_t *data,
                             size_t len, size_t *frames_off, int *cls);
/* Explicit key rotation (the peer follows on the epoch byte). */
void ip_secure_session_rekey(ip_secure_session *s);

/* Session health counters (layout-stable; extend only at the end). */
typedef struct ip_secure_session_diag {
    uint32_t auth_failures;      /* bad tag / truncated datagrams */
    uint32_t replays_rejected;   /* replay-window hits */
    uint32_t old_epoch_rejected; /* stale-epoch datagrams */
    uint32_t tx_epoch, rx_epoch; /* current key epochs */
} ip_secure_session_diag;
void ip_secure_session_get_diag(const ip_secure_session *s,
                                ip_secure_session_diag *d);

/* ================================================================
 * Device registry + extension-descriptor accessors
 * ================================================================
 * Enough for a host to stand a device up in loopback and enumerate
 * it. The device side is the library's global singleton (proto.hpp
 * init()/rx()); after ip_device_init() the registry always holds at
 * least the library-owned meta-commands (list_extensions,
 * list_constants, extension_desc, constant_desc, extension_done) and
 * the FRAMING_V2 capability constant. */

/* Initialize the device singleton with a transmit hook and the
 * version strings served in its dictionary. identify_blob is not set
 * (the extension self-description path needs none). */
void ip_device_init(ip_write_fn write_fn, void *user, const char *version,
                    const char *build_version);
/* Feed raw link bytes to the device; valid frames are acked and their
 * messages dispatched (including the library-owned meta-commands),
 * damaged ones nacked, all from inside this call. */
void ip_device_rx(const uint8_t *data, size_t len);

/* Registry enumeration (local introspection; the same data the device
 * serves over list_extensions / list_constants on the wire). */
int ip_command_count(void);
int ip_response_count(void);
int ip_constant_count(void);
/* Assigned wire id of the idx-th command / response (definition
 * order), or 0 if idx is out of range. */
uint32_t ip_command_id(int idx);
uint32_t ip_response_id(int idx);
/* Name of the idx-th command / response, or NULL if out of range. */
const char *ip_command_name(int idx);
const char *ip_response_name(int idx);
/* Write the idx-th command's / response's dictionary key string
 * ("name p=%c ...", NUL-terminated) into out; returns the length, or
 * 0 if idx is out of range or cap is too small. */
size_t ip_command_key(int idx, char *out, size_t cap);
size_t ip_response_key(int idx, char *out, size_t cap);
/* Index of the command with this name, or -1 if none. */
int ip_command_index_by_name(const char *name);

#ifdef __cplusplus
}
#endif

#endif /* INTENTPROTO_CAPI_H */
