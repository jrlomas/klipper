// intentproto host-profile C API — implementation (FD-0001 doc 10).
//
// A thin extern "C" shim over the freestanding C++ core. Every
// function forwards to include/intentproto/*.hpp; the only logic added
// here is host-profile convenience allocation (the C++ core keeps its
// caller-owned, heap-free contract untouched). See capi.h for the
// documented surface and the ABI-versioning contract.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
// MIT licensed (see LICENSE).

#include "intentproto/capi.h"

#include "intentproto/datagram.hpp"
#include "intentproto/host.hpp"
#include "intentproto/session_sec.hpp"
#include "intentproto/proto.hpp"
#include "intentproto/segment.hpp"

#include <stdlib.h>
#include <string.h>

using namespace intentproto;

namespace {

TrafficClass class_of_int(int cls) {
    switch (cls) {
    case IP_CLASS_PROMPT:
        return TrafficClass::Prompt;
    case IP_CLASS_TELEMETRY:
        return TrafficClass::Telemetry;
    default:
        return TrafficClass::Scheduled;
    }
}

const Command* command_at(int idx) {
    if (idx < 0)
        return nullptr;
    for (const Command* c = first_command(); c; c = c->next, idx--)
        if (idx == 0)
            return c;
    return nullptr;
}

const Response* response_at(int idx) {
    if (idx < 0)
        return nullptr;
    for (const Response* r = first_response(); r; r = r->next, idx--)
        if (idx == 0)
            return r;
    return nullptr;
}

} // namespace

extern "C" {

// ---- ABI versioning ----

uint32_t intentproto_abi_version(void) { return INTENTPROTO_ABI_VERSION; }

const char* intentproto_version_string(void) {
    return "intentproto " "1.0.0";
}

// ---- framing primitives ----

uint16_t ip_crc16_ccitt(const uint8_t* buf, size_t len) {
    return crc16_ccitt(buf, len);
}

size_t ip_vlq_encode(uint8_t* out, uint32_t v) {
    return (size_t)(vlq_encode(out, v) - out);
}

size_t ip_vlq_decode(const uint8_t* in, size_t len, uint32_t* out) {
    const uint8_t* p = in;
    if (!vlq_decode(&p, in + len, out))
        return 0;
    return (size_t)(p - in);
}

size_t ip_frame_v2_encode(uint8_t* out, const uint8_t* payload,
                          size_t payload_len, uint8_t seq) {
    return frame_v2_encode(out, payload, payload_len, seq);
}

int ip_frame_v2_decode(uint8_t* frame, size_t frame_len, size_t* payload_off,
                       uint8_t* seq, int* corrected) {
    const uint8_t* payload = nullptr;
    int n = frame_v2_decode(frame, frame_len, &payload, seq, corrected);
    if (n >= 0 && payload_off)
        *payload_off = (size_t)(payload - frame);
    return n;
}

// ---- trajectory segment codec (FD-0001 doc 02) ----

int32_t ip_segment_quantize(double true_value, unsigned order_k) {
    return segment_quantize(true_value, order_k);
}

int64_t ip_segment_end_delta(uint32_t duration, int32_t velocity,
                             int32_t accel, int32_t jerk, int32_t snap,
                             int32_t crackle) {
    return segment_end_delta(duration, velocity, accel, jerk, snap, crackle);
}

int64_t ip_segment_chain_advance(int64_t acc, uint32_t duration,
                                 int32_t velocity, int32_t accel,
                                 int32_t jerk, int32_t snap, int32_t crackle,
                                 int64_t* new_acc) {
    SegmentChain ch{acc};
    int64_t pos = segment_chain_advance(&ch, duration, velocity, accel, jerk,
                                        snap, crackle);
    if (new_acc)
        *new_acc = ch.acc;
    return pos;
}

size_t ip_segment_encode(uint8_t* out, size_t cap, uint8_t oid, uint8_t flags,
                         uint32_t duration, int32_t velocity, int32_t accel,
                         int32_t jerk, int32_t snap, int32_t crackle) {
    return segment_encode(out, cap, oid, flags, duration, velocity, accel,
                          jerk, snap, crackle);
}

size_t ip_segment_encode_hold(uint8_t* out, size_t cap, uint8_t oid,
                              uint32_t duration) {
    return segment_encode_hold(out, cap, oid, duration);
}

int ip_segment_decode(const uint8_t* in, size_t len, ip_segment* seg) {
    SegmentPayload sp;
    int kind = segment_decode(in, len, &sp);
    if (seg) {
        seg->kind = sp.kind;
        seg->oid = sp.oid;
        seg->flags = sp.flags;
        seg->duration = sp.duration;
        seg->velocity = sp.velocity;
        seg->accel = sp.accel;
        seg->jerk = sp.jerk;
        seg->snap = sp.snap;
        seg->crackle = sp.crackle;
    }
    return kind;
}

// ---- host session ----

struct ip_host_session {
    HostSession session;
};

ip_host_session* ip_host_session_create(ip_write_fn write_fn, void* wuser,
                                        ip_response_fn response_fn,
                                        void* ruser, int desired_framing) {
    ip_host_session* h =
        (ip_host_session*)malloc(sizeof(ip_host_session));
    if (!h)
        return nullptr;
    HostSession::Framing desired = desired_framing == IP_FRAMING_PROBING
                                       ? HostSession::Framing::Probing
                                       : HostSession::Framing::Legacy;
    h->session.init(write_fn, wuser, response_fn, ruser, desired);
    return h;
}

void ip_host_session_free(ip_host_session* h) { free(h); }

int ip_host_session_send_command(ip_host_session* h, const uint8_t* payload,
                                 size_t len, int cls) {
    return h->session.send_command(payload, len, class_of_int(cls)) ? 1 : 0;
}

void ip_host_session_on_rx(ip_host_session* h, const uint8_t* data,
                           size_t len) {
    h->session.on_rx(data, len);
}

int ip_host_session_need_retransmit(ip_host_session* h, uint64_t now_ticks,
                                    uint64_t rto_ticks) {
    return h->session.need_retransmit(now_ticks, rto_ticks) ? 1 : 0;
}

int ip_host_session_enable_v2(ip_host_session* h) {
    return h->session.session_enable_v2() ? 1 : 0;
}

size_t ip_host_session_inflight(const ip_host_session* h) {
    return h->session.inflight();
}

void ip_host_session_class_stats(const ip_host_session* h, int cls,
                                 ip_class_stats* out) {
    if (!out)
        return;
    int i = (int)class_of_int(cls);
    const ClassStats& s = h->session.class_stats[i];
    out->tx_msgs = s.tx_msgs;
    out->tx_bytes = s.tx_bytes;
    out->rx_msgs = s.rx_msgs;
    out->rx_bytes = s.rx_bytes;
    out->dropped = s.dropped;
}

void ip_host_session_diag(const ip_host_session* h, ip_host_diag* out) {
    if (!out)
        return;
    const HostSession& s = h->session;
    out->retransmits = s.retransmits;
    out->naks = s.naks;
    out->rx_crc_errors = s.rx_crc_errors;
    out->rx_bch_errors = s.rx_bch_errors;
    out->rx_framing_errors = s.rx_framing_errors;
    out->v2_frames_rx = s.v2_frames_rx;
    out->v2_rejected = s.v2_rejected ? 1 : 0;
    out->framing_v2 = s.framing == HostSession::Framing::V2 ? 1 : 0;
}

uint32_t ip_host_session_sequence_rebases(const ip_host_session* h) {
    return h ? h->session.sequence_rebases : 0;
}

// ---- datagram transport ----

struct ip_datagram_tx {
    DatagramTx tx;
    uint8_t psk[64];
};
struct ip_datagram_rx {
    DatagramRx rx;
    uint8_t psk[64];
};

ip_datagram_tx* ip_datagram_tx_create(const uint8_t* psk, size_t psk_len,
                                      uint8_t fec_k) {
    ip_datagram_tx* d = (ip_datagram_tx*)malloc(sizeof(ip_datagram_tx));
    if (!d)
        return nullptr;
    const uint8_t* key = nullptr;
    if (psk && psk_len) {
        if (psk_len > sizeof(d->psk))
            psk_len = sizeof(d->psk);
        memcpy(d->psk, psk, psk_len);
        key = d->psk;
    } else {
        psk_len = 0;
    }
    datagram_tx_init(&d->tx, key, psk_len, fec_k);
    return d;
}

void ip_datagram_tx_free(ip_datagram_tx* tx) { free(tx); }

ip_datagram_rx* ip_datagram_rx_create(const uint8_t* psk, size_t psk_len) {
    ip_datagram_rx* d = (ip_datagram_rx*)malloc(sizeof(ip_datagram_rx));
    if (!d)
        return nullptr;
    const uint8_t* key = nullptr;
    if (psk && psk_len) {
        if (psk_len > sizeof(d->psk))
            psk_len = sizeof(d->psk);
        memcpy(d->psk, psk, psk_len);
        key = d->psk;
    } else {
        psk_len = 0;
    }
    datagram_rx_init(&d->rx, key, psk_len);
    return d;
}

void ip_datagram_rx_free(ip_datagram_rx* rx) { free(rx); }

size_t ip_datagram_encode(ip_datagram_tx* tx, uint8_t* out,
                          const uint8_t* frames, size_t len, int cls) {
    return datagram_encode(&tx->tx, out, frames, len, class_of_int(cls));
}

size_t ip_datagram_parity_flush(ip_datagram_tx* tx, uint8_t* out) {
    return datagram_parity_flush(&tx->tx, out);
}

int ip_datagram_decode(ip_datagram_rx* rx, uint8_t* data, size_t len,
                       size_t* frames_off, int* cls) {
    const uint8_t* frames = nullptr;
    TrafficClass tc = TrafficClass::Scheduled;
    int n = datagram_decode(&rx->rx, data, len, &frames, &tc);
    if (n > 0 && frames_off)
        *frames_off = (size_t)(frames - data);
    if (cls)
        *cls = (int)tc;
    return n;
}

size_t ip_datagram_take_recovered(ip_datagram_rx* rx, uint8_t* out,
                                  size_t cap) {
    return datagram_take_recovered(&rx->rx, out, cap);
}

// ---- device registry + extension descriptors ----

// Persist the version strings (Config stores const char* by copy of
// the pointer, not the bytes — see proto.cpp current_config()).
static char g_version[64];
static char g_build_version[64];

void ip_device_init(ip_write_fn write_fn, void* user, const char* version,
                    const char* build_version) {
    Config cfg;
    cfg.write = write_fn;
    cfg.user = user;
    if (version) {
        strncpy(g_version, version, sizeof(g_version) - 1);
        g_version[sizeof(g_version) - 1] = '\0';
        cfg.version = g_version;
    }
    if (build_version) {
        strncpy(g_build_version, build_version, sizeof(g_build_version) - 1);
        g_build_version[sizeof(g_build_version) - 1] = '\0';
        cfg.build_version = g_build_version;
    }
    init(cfg);
}

void ip_device_rx(const uint8_t* data, size_t len) { rx(data, len); }

int ip_command_count(void) {
    int n = 0;
    for (const Command* c = first_command(); c; c = c->next)
        n++;
    return n;
}

int ip_response_count(void) {
    int n = 0;
    for (const Response* r = first_response(); r; r = r->next)
        n++;
    return n;
}

int ip_constant_count(void) {
    int n = 0;
    for (const Constant* k = first_constant(); k; k = k->next)
        n++;
    return n;
}

uint32_t ip_command_id(int idx) {
    const Command* c = command_at(idx);
    return c ? c->id : 0;
}

uint32_t ip_response_id(int idx) {
    const Response* r = response_at(idx);
    return r ? r->id : 0;
}

const char* ip_command_name(int idx) {
    const Command* c = command_at(idx);
    return c ? c->name : nullptr;
}

const char* ip_response_name(int idx) {
    const Response* r = response_at(idx);
    return r ? r->name : nullptr;
}

size_t ip_command_key(int idx, char* out, size_t cap) {
    const Command* c = command_at(idx);
    if (!c)
        return 0;
    return message_key(out, cap, c->name, c->param_names, c->param_types,
                       c->num_params);
}

size_t ip_response_key(int idx, char* out, size_t cap) {
    const Response* r = response_at(idx);
    if (!r)
        return 0;
    return message_key(out, cap, r->name, r->field_names, r->field_types,
                       r->num_fields);
}

int ip_command_index_by_name(const char* name) {
    if (!name)
        return -1;
    int idx = 0;
    for (const Command* c = first_command(); c; c = c->next, idx++)
        if (!strcmp(c->name, name))
            return idx;
    return -1;
}

// ---- secure session (session_sec.hpp) ----
// The wrapper owns a copy of the PSK (SecureSession stores a pointer).
struct ip_secure_session {
    SecureSession s;
    uint8_t psk[64];
};

ip_secure_session* ip_secure_session_create(int is_initiator,
                                            const uint8_t* psk,
                                            size_t psk_len,
                                            const uint8_t* board_id,
                                            size_t id_len,
                                            const uint8_t* my_random16,
                                            uint32_t rekey) {
    if (!psk || !psk_len || !my_random16)
        return nullptr;
    ip_secure_session* w =
        (ip_secure_session*)malloc(sizeof(ip_secure_session));
    if (!w)
        return nullptr;
    if (psk_len > sizeof(w->psk))
        psk_len = sizeof(w->psk);
    memcpy(w->psk, psk, psk_len);
    w->s.init(is_initiator ? SecRole::Initiator : SecRole::Responder,
              w->psk, psk_len, board_id, id_len, my_random16,
              rekey ? rekey : SEC_DEFAULT_REKEY);
    return w;
}

void ip_secure_session_free(ip_secure_session* s) {
    free(s);
}

size_t ip_secure_session_start(ip_secure_session* s, uint8_t* out,
                               size_t cap) {
    return s->s.start(out, cap);
}

size_t ip_secure_session_on_handshake(ip_secure_session* s,
                                      const uint8_t* msg, size_t len,
                                      uint8_t* out, size_t cap) {
    return s->s.on_handshake(msg, len, out, cap);
}

int ip_secure_session_established(const ip_secure_session* s) {
    return s->s.established();
}

int ip_secure_session_failed(const ip_secure_session* s) {
    return s->s.failed();
}

size_t ip_secure_session_peer_id(const ip_secure_session* s, uint8_t* out,
                                 size_t cap) {
    size_t n = s->s.peer_id_len();
    if (n > cap)
        n = cap;
    memcpy(out, s->s.peer_id(), n);
    return n;
}

size_t ip_secure_session_encode(ip_secure_session* s, uint8_t* out,
                                size_t cap, const uint8_t* frames,
                                size_t len, int cls) {
    return s->s.datagram_encode(out, cap, frames, len,
                                class_of_int(cls));
}

int ip_secure_session_decode(ip_secure_session* s, uint8_t* data,
                             size_t len, size_t* frames_off, int* cls) {
    const uint8_t* frames = nullptr;
    TrafficClass tc = TrafficClass::Scheduled;
    int r = s->s.datagram_decode(data, len, &frames, &tc);
    if (r > 0 && frames_off)
        *frames_off = (size_t)(frames - data);
    if (cls)
        *cls = (int)tc;
    return r;
}

void ip_secure_session_rekey(ip_secure_session* s) {
    s->s.rekey();
}

void ip_secure_session_get_diag(const ip_secure_session* s,
                                ip_secure_session_diag* d) {
    if (!d)
        return;
    d->auth_failures = s->s.auth_failures;
    d->replays_rejected = s->s.replays_rejected;
    d->old_epoch_rejected = s->s.old_epoch_rejected;
    d->tx_epoch = s->s.tx_epoch;
    d->rx_epoch = s->s.rx_epoch;
}

} // extern "C"
