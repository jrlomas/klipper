// intentproto framing v2 negotiation tests: loopback of HostSession
// against the device side in proto.cpp, plus a simulated legacy-only
// peer. Covers the FD-0001 doc 07 negotiation path: legacy default,
// dictionary-driven upgrade, BCH correction avoiding retransmits,
// uncorrectable damage falling back to ARQ, and the automatic
// legacy-peer fallback (v2_rejected).

#include "intentproto/host.hpp"
#include "intentproto/method.hpp"

#include <stdio.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

// ---------------- device-side declarations ----------------

KLIPPER_RESPONSE(echo_status, (uint8_t, oid), (uint32_t, value));

static struct {
    int set_value_calls = 0;
    uint8_t oid = 0xff;
    uint32_t value = 0;
} g_dev_seen;

KLIPPER_METHOD(cmd_set_value, (uint8_t, oid), (uint32_t, value)) {
    g_dev_seen.set_value_calls++;
    g_dev_seen.oid = oid;
    g_dev_seen.value = value;
    intentproto::reply(echo_status{oid, value});
}

// ---------------- buffered loopback plumbing ----------------

// host -> device bytes (HostSession write hook fills, pump drains)
static uint8_t g_h2d[4096];
static size_t g_h2d_len = 0;
// device -> host bytes (device Config::write fills, pump drains)
static uint8_t g_d2h[4096];
static size_t g_d2h_len = 0;

static int host_write(const uint8_t* data, size_t len, void*) {
    if (g_h2d_len + len <= sizeof(g_h2d)) {
        memcpy(g_h2d + g_h2d_len, data, len);
        g_h2d_len += len;
    }
    return (int)len;
}

static int device_write(const uint8_t* data, size_t len, void*) {
    if (g_d2h_len + len <= sizeof(g_d2h)) {
        memcpy(g_d2h + g_d2h_len, data, len);
        g_d2h_len += len;
    }
    return (int)len;
}

// Responses delivered by HostSession::on_rx.
static struct {
    int count = 0;
    uint8_t last[intentproto::MESSAGE_MAX];
    size_t last_len = 0;
} g_responses;

static void host_response(const uint8_t* payload, size_t len, void*) {
    g_responses.count++;
    if (len <= sizeof(g_responses.last)) {
        memcpy(g_responses.last, payload, len);
        g_responses.last_len = len;
    }
}

static intentproto::HostSession g_host;

static void pump_h2d() {
    intentproto::rx(g_h2d, g_h2d_len);
    g_h2d_len = 0;
}

static void pump_d2h() {
    g_host.on_rx(g_d2h, g_d2h_len);
    g_d2h_len = 0;
}

// Split a captured byte stream into frames (both framings share the
// leading length byte; idle sync bytes are skipped).
static int split_frames(const uint8_t* buf, size_t len,
                        const uint8_t* frames[], int max) {
    int n = 0;
    size_t pos = 0;
    while (pos < len && n < max) {
        if (buf[pos] == intentproto::MESSAGE_SYNC) {
            pos++;                      // idle sync between frames
            continue;
        }
        uint8_t flen = buf[pos];
        if (flen < intentproto::MESSAGE_MIN || pos + flen > len)
            break;
        frames[n++] = buf + pos;
        pos += flen;
    }
    return n;
}

static const intentproto::Command* cmd_by_name(const char* name) {
    for (const intentproto::Command* c = intentproto::first_command(); c;
         c = c->next)
        if (!strcmp(c->name, name))
            return c;
    return nullptr;
}

static const intentproto::Response* res_by_name(const char* name) {
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next)
        if (!strcmp(r->name, name))
            return r;
    return nullptr;
}

// Encode and send one cmd_set_value command through a session.
static bool send_set_value(intentproto::HostSession& host, uint8_t oid,
                           uint32_t value) {
    uint8_t payload[16];
    uint8_t* p = payload;
    p = intentproto::vlq_encode(p, cmd_by_name("cmd_set_value")->id);
    p = intentproto::vlq_encode(p, oid);
    p = intentproto::vlq_encode(p, value);
    return host.send_command(payload, (size_t)(p - payload));
}

// Decode an echo_status payload delivered to the host.
static void check_echo(uint8_t oid, uint32_t value) {
    const uint8_t* p = g_responses.last;
    const uint8_t* end = p + g_responses.last_len;
    uint32_t msgid = 0, roid = 0, rvalue = 0;
    CHECK(intentproto::vlq_decode(&p, end, &msgid));
    CHECK(msgid == res_by_name("echo_status")->id);
    CHECK(intentproto::vlq_decode(&p, end, &roid) && roid == oid);
    CHECK(intentproto::vlq_decode(&p, end, &rvalue) && rvalue == value);
    CHECK(p == end);
}

// ---------------- tests ----------------

using Framing = intentproto::HostSession::Framing;

// (1) The default session is pure legacy and behaves as before; the
// device advertises FRAMING_V2 in its dictionary.
static void test_legacy_default() {
    char json[4096];
    CHECK(intentproto::build_dictionary(json, sizeof(json)) > 0);
    CHECK(strstr(json, "\"FRAMING_V2\":1") != nullptr);

    CHECK(g_host.framing == Framing::Legacy);
    CHECK(send_set_value(g_host, 9, 7777));
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == 1);
    CHECK(!intentproto::link_framing_v2());

    // Device replies are legacy: no FRAME_V2_FLAG on any frame.
    const uint8_t* frames[8];
    int nf = split_frames(g_d2h, g_d2h_len, frames, 8);
    CHECK(nf == 2);   // echo_status + ack
    for (int i = 0; i < nf; i++)
        CHECK(!(frames[i][1] & intentproto::FRAME_V2_FLAG));
    pump_d2h();
    CHECK(g_host.inflight() == 0);
    CHECK(g_responses.count == 1);
    check_echo(9, 7777);
    CHECK(g_host.framing == Framing::Legacy);
    CHECK(g_host.v2_frames_rx == 0);
}

// (2) Upgrade: the caller saw FRAMING_V2 and enables v2. The first
// probe latches the device; replies carry FRAME_V2_FLAG and the host
// confirms the upgrade.
static void test_upgrade_round_trip() {
    CHECK(g_host.session_enable_v2());
    CHECK(g_host.framing == Framing::Probing);

    CHECK(send_set_value(g_host, 3, 123456));
    // The probe frame itself carries the v2 seq bit.
    CHECK(g_h2d_len > 1);
    CHECK(g_h2d[1] & intentproto::FRAME_V2_FLAG);
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == 2);
    CHECK(g_dev_seen.value == 123456);
    CHECK(intentproto::link_framing_v2());

    // Every device reply (response + ack) is v2-framed now.
    const uint8_t* frames[8];
    int nf = split_frames(g_d2h, g_d2h_len, frames, 8);
    CHECK(nf == 2);
    for (int i = 0; i < nf; i++)
        CHECK(frames[i][1] & intentproto::FRAME_V2_FLAG);
    pump_d2h();
    CHECK(g_host.framing == Framing::V2);
    CHECK(!g_host.v2_rejected);
    CHECK(g_host.v2_frames_rx == 2);
    CHECK(g_host.inflight() == 0);
    CHECK(g_responses.count == 2);
    check_echo(3, 123456);
    // Per-class accounting: both commands so far were Scheduled.
    CHECK(g_host.class_stats[0].tx_msgs == 2);
    CHECK(g_host.class_stats[1].tx_msgs == 0);
}

// (3) Up to three bit errors are corrected in place: the command
// still dispatches, the reply is correct, and NO retransmit or nak
// happens anywhere.
static void test_bch_corrects_without_retransmit() {
    uint32_t retransmits = g_host.retransmits;
    uint32_t naks = g_host.naks;
    uint32_t bch_errors = intentproto::link_stats().bch_errors;

    CHECK(send_set_value(g_host, 5, 42));
    // Damage three bits of the queued frame (payload/parity region;
    // byte 0 is the length the rx state machine frames on, and the
    // final byte is the uncovered sync).
    CHECK(g_h2d_len >= intentproto::FRAME_V2_OVERHEAD + 3);
    g_h2d[2] ^= 0x04;
    g_h2d[4] ^= 0x80;
    g_h2d[g_h2d_len - 3] ^= 0x01;   // a parity byte
    pump_h2d();

    CHECK(g_dev_seen.set_value_calls == 3);
    CHECK(g_dev_seen.oid == 5);
    CHECK(g_dev_seen.value == 42);
    CHECK(intentproto::link_stats().bch_corrected >= 3);
    CHECK(intentproto::link_stats().bch_errors == bch_errors);
    pump_d2h();
    CHECK(g_host.inflight() == 0);
    check_echo(5, 42);
    // FEC did its job: the ARQ machinery never moved.
    CHECK(g_host.naks == naks);
    CHECK(!g_host.nak_pending);
    CHECK(!g_host.need_retransmit(1000000, 1));
    CHECK(g_host.retransmits == retransmits);
}

// (4) Damage beyond t=3 is uncorrectable: the device naks (in v2
// framing), the host retransmits, and the exchange recovers.
static void test_uncorrectable_nak_recovers() {
    int calls = g_dev_seen.set_value_calls;
    uint32_t naks = g_host.naks;
    uint32_t bch_errors = intentproto::link_stats().bch_errors;

    CHECK(send_set_value(g_host, 6, 999));
    // Scatter eight bit errors across payload and parity.
    CHECK(g_h2d_len >= 10);
    g_h2d[2] ^= 0x11;
    g_h2d[3] ^= 0x42;
    g_h2d[5] ^= 0x08;
    g_h2d[g_h2d_len - 4] ^= 0x21;
    g_h2d[g_h2d_len - 3] ^= 0x80;
    pump_h2d();

    CHECK(g_dev_seen.set_value_calls == calls);      // not dispatched
    CHECK(intentproto::link_stats().bch_errors == bch_errors + 1);
    // The nak itself is v2-framed: the link stays latched.
    const uint8_t* frames[4];
    int nf = split_frames(g_d2h, g_d2h_len, frames, 4);
    CHECK(nf == 1);
    if (nf >= 1)
        CHECK(frames[0][1] & intentproto::FRAME_V2_FLAG);
    pump_d2h();
    CHECK(g_host.naks == naks + 1);
    CHECK(g_host.need_retransmit(0, 1000000));       // nak-driven
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == calls + 1);
    CHECK(g_dev_seen.value == 999);
    pump_d2h();
    CHECK(g_host.inflight() == 0);
    check_echo(6, 999);
    // Still v2 on both sides — a nak never downgrades the link.
    CHECK(g_host.framing == Framing::V2);
    CHECK(intentproto::link_framing_v2());
}

// (6) identify works after the upgrade, served in v2 framing.
static void test_identify_post_upgrade() {
    uint8_t payload[16];
    uint8_t* p = payload;
    p = intentproto::vlq_encode(p, intentproto::MSGID_IDENTIFY);
    p = intentproto::vlq_encode(p, 12);   // offset
    p = intentproto::vlq_encode(p, 40);   // count
    CHECK(g_host.send_command(payload, (size_t)(p - payload)));
    pump_h2d();

    const uint8_t* frames[4];
    int nf = split_frames(g_d2h, g_d2h_len, frames, 4);
    CHECK(nf == 2);   // identify_response + ack, both v2
    for (int i = 0; i < nf; i++)
        CHECK(frames[i][1] & intentproto::FRAME_V2_FLAG);
    int responses = g_responses.count;
    pump_d2h();
    CHECK(g_host.inflight() == 0);
    CHECK(g_responses.count == responses + 1);
    const uint8_t* rp = g_responses.last;
    const uint8_t* rend = rp + g_responses.last_len;
    uint32_t msgid = 1, offset = 0, dlen = 0;
    CHECK(intentproto::vlq_decode(&rp, rend, &msgid));
    CHECK(msgid == intentproto::MSGID_IDENTIFY_RESPONSE);
    CHECK(intentproto::vlq_decode(&rp, rend, &offset) && offset == 12);
    CHECK(intentproto::vlq_decode(&rp, rend, &dlen) && dlen == 8);
    CHECK((size_t)(rend - rp) == 8);
    CHECK(!memcmp(rp, "cdefghij", 8));
}

// ---------------- simulated legacy-only peer ----------------

// A legacy peer enforces (seq & ~MESSAGE_SEQ_MASK) == MESSAGE_DEST
// and naks anything else — that is the compatibility hook v2 probes
// rely on. This stub implements exactly that: v2 frames are nacked
// (duplicate empty ack), legacy frames are consumed and acked.
static struct {
    uint8_t buf[1024];
    size_t len = 0;
    uint8_t expect = 0;             // next expected seq nibble
    int commands = 0;
    uint8_t last_payload[intentproto::MESSAGE_MAX];
    size_t last_payload_len = 0;
    int naks_sent = 0;
} g_stub;

static intentproto::HostSession g_host2;

static int stub_write(const uint8_t* data, size_t len, void*) {
    if (g_stub.len + len <= sizeof(g_stub.buf)) {
        memcpy(g_stub.buf + g_stub.len, data, len);
        g_stub.len += len;
    }
    return (int)len;
}

static void stub_reply(uint8_t seq_nibble) {
    uint8_t f[intentproto::MESSAGE_MIN];
    f[0] = intentproto::MESSAGE_MIN;
    f[1] = (uint8_t)(intentproto::MESSAGE_DEST
                     | (seq_nibble & intentproto::MESSAGE_SEQ_MASK));
    uint16_t crc = intentproto::crc16_ccitt(f, 2);
    f[2] = (uint8_t)(crc >> 8);
    f[3] = (uint8_t)(crc & 0xff);
    f[4] = intentproto::MESSAGE_SYNC;
    g_host2.on_rx(f, sizeof(f));
}

// Drain host2's queued bytes through the legacy peer.
static void pump_stub() {
    const uint8_t* frames[16];
    int nf = split_frames(g_stub.buf, g_stub.len, frames, 16);
    for (int i = 0; i < nf; i++) {
        const uint8_t* f = frames[i];
        if ((f[1] & ~intentproto::MESSAGE_SEQ_MASK)
            != intentproto::MESSAGE_DEST) {
            // Reserved bits set: reject and request retransmission
            // (an empty frame that does not advance the window).
            g_stub.naks_sent++;
            stub_reply(g_stub.expect);
            continue;
        }
        if ((f[1] & intentproto::MESSAGE_SEQ_MASK) == g_stub.expect) {
            g_stub.expect = (uint8_t)((g_stub.expect + 1)
                                      & intentproto::MESSAGE_SEQ_MASK);
            g_stub.commands++;
            g_stub.last_payload_len = f[0] - intentproto::MESSAGE_MIN;
            memcpy(g_stub.last_payload, f + intentproto::HEADER_SIZE,
                   g_stub.last_payload_len);
        }
        stub_reply(g_stub.expect);      // ack (or duplicate-ack nak)
    }
    g_stub.len = 0;
}

// (5) Probing a legacy peer: every v2 probe is nacked, the host
// falls back to legacy automatically, latches v2_rejected, and the
// command still gets through.
static void test_legacy_peer_fallback() {
    g_host2.init(stub_write, nullptr, nullptr, nullptr);
    CHECK(g_host2.session_enable_v2());
    CHECK(g_host2.framing == Framing::Probing);

    uint8_t payload[16];
    uint8_t* p = payload;
    p = intentproto::vlq_encode(p, 77);      // opaque to the stub
    p = intentproto::vlq_encode(p, 4321);
    CHECK(g_host2.send_command(payload, (size_t)(p - payload),
                               intentproto::TrafficClass::Prompt));
    CHECK(g_host2.class_of(0) == intentproto::TrafficClass::Prompt);
    CHECK(g_host2.class_stats[1].tx_msgs == 1);

    // Nak -> retransmit rounds until the probe limit falls back.
    int rounds = 0;
    while (g_host2.framing == Framing::Probing && rounds < 20) {
        pump_stub();
        g_host2.need_retransmit(0, 1000000);
        rounds++;
    }
    CHECK(g_host2.framing == Framing::Legacy);
    CHECK(g_host2.v2_rejected);
    CHECK(g_host2.v2_frames_rx == 0);
    CHECK((uint32_t)g_stub.naks_sent >= intentproto::HOST_V2_PROBE_LIMIT);
    CHECK(g_stub.commands == 0);             // nothing misinterpreted

    // The pending retransmit went out in legacy framing: traffic
    // continues and the window drains.
    pump_stub();
    CHECK(g_stub.commands == 1);
    CHECK(g_stub.last_payload_len == (size_t)(p - payload));
    CHECK(!memcmp(g_stub.last_payload, payload, g_stub.last_payload_len));
    CHECK(g_host2.inflight() == 0);

    // Traffic keeps flowing in legacy.
    CHECK(g_host2.send_command(payload, (size_t)(p - payload)));
    pump_stub();
    CHECK(g_stub.commands == 2);
    CHECK(g_host2.inflight() == 0);
    CHECK(g_host2.framing == Framing::Legacy);
}

int main() {
    static const char blob[] = "0123456789abcdefghij";
    intentproto::Config cfg;
    cfg.write = device_write;
    cfg.version = "test-1.0";
    cfg.identify_blob = (const uint8_t*)blob;
    cfg.identify_blob_len = 20;
    intentproto::init(cfg);
    g_host.init(host_write, nullptr, host_response, nullptr);

    test_legacy_default();
    test_upgrade_round_trip();
    test_bch_corrects_without_retransmit();
    test_uncorrectable_nak_recovers();
    test_identify_post_upgrade();
    test_legacy_peer_fallback();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
