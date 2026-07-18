// intentproto host session tests: loopback of HostSession against
// the device side in proto.cpp. Both directions are buffered so the
// test can pump, drop, or corrupt individual frames.

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

// Queue a valid legacy empty frame carrying the peer's next expected
// sequence.  The application-side protocol uses this same wire shape for
// both ack and nak, so bootstrap reconnect must disambiguate by behavior.
static void queue_empty_peer_frame(uint8_t seq) {
    uint8_t frame[intentproto::MESSAGE_MIN] = {
        intentproto::MESSAGE_MIN,
        (uint8_t)(intentproto::MESSAGE_DEST
                  | (seq & intentproto::MESSAGE_SEQ_MASK)),
        0, 0, intentproto::MESSAGE_SYNC
    };
    uint16_t crc = intentproto::crc16_ccitt(
        frame, intentproto::MESSAGE_MIN - intentproto::TRAILER_SIZE);
    frame[intentproto::MESSAGE_MIN - 3] = (uint8_t)(crc >> 8);
    frame[intentproto::MESSAGE_MIN - 2] = (uint8_t)crc;
    device_write(frame, sizeof(frame), nullptr);
}

// Deliver queued host bytes to the device (optionally not).
static void pump_h2d() {
    intentproto::rx(g_h2d, g_h2d_len);
    g_h2d_len = 0;
}

static void drop_h2d() { g_h2d_len = 0; }

// Deliver queued device bytes to the host session.
static void pump_d2h() {
    g_host.on_rx(g_d2h, g_d2h_len);
    g_d2h_len = 0;
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

// Encode and send one cmd_set_value command through the session.
static bool send_set_value(uint8_t oid, uint32_t value) {
    uint8_t payload[16];
    uint8_t* p = payload;
    p = intentproto::vlq_encode(p, cmd_by_name("cmd_set_value")->id);
    p = intentproto::vlq_encode(p, oid);
    p = intentproto::vlq_encode(p, value);
    return g_host.send_command(payload, (size_t)(p - payload));
}

// ---------------- tests ----------------

static void test_round_trip_and_ack() {
    CHECK(send_set_value(9, 7777));
    CHECK(g_host.inflight() == 1);
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == 1);
    CHECK(g_dev_seen.oid == 9);
    CHECK(g_dev_seen.value == 7777);

    pump_d2h();
    // The response frame and the ack both carry the advanced
    // sequence: the window drains and the payload is delivered.
    CHECK(g_host.inflight() == 0);
    CHECK(g_responses.count == 1);
    const uint8_t* p = g_responses.last;
    const uint8_t* end = p + g_responses.last_len;
    uint32_t msgid = 0, oid = 0, value = 0;
    CHECK(intentproto::vlq_decode(&p, end, &msgid));
    CHECK(msgid == res_by_name("echo_status")->id);
    CHECK(intentproto::vlq_decode(&p, end, &oid) && oid == 9);
    CHECK(intentproto::vlq_decode(&p, end, &value) && value == 7777);
    CHECK(p == end);

    // Nothing in flight: no retransmit however late the poll is.
    CHECK(!g_host.need_retransmit(1000000, 1));
    CHECK(g_host.retransmits == 0);
}

static void test_window_limit() {
    // Fill the window without delivering anything.
    for (size_t i = 0; i < intentproto::HOST_WINDOW; i++)
        CHECK(send_set_value(1, (uint32_t)i));
    CHECK(g_host.inflight() == intentproto::HOST_WINDOW);
    CHECK(!send_set_value(1, 999));   // window full: refused

    pump_h2d();
    pump_d2h();
    CHECK(g_host.inflight() == 0);    // acks drained the window
    CHECK(g_host.naks == 0);
    CHECK(send_set_value(1, 999));    // and sending works again
    pump_h2d();
    pump_d2h();
    CHECK(g_host.inflight() == 0);
}

static void test_dropped_frame_retransmit() {
    int calls_before = g_dev_seen.set_value_calls;
    CHECK(send_set_value(4, 1234));
    drop_h2d();                        // the link ate the frame
    CHECK(g_host.inflight() == 1);

    // First poll arms the RTO clock; before expiry, nothing happens.
    CHECK(!g_host.need_retransmit(100, 50));
    CHECK(!g_host.need_retransmit(149, 50));
    CHECK(g_h2d_len == 0);

    // Expiry: go-back-N resends the frame (after a resync byte).
    CHECK(g_host.need_retransmit(150, 50));
    CHECK(g_host.retransmits == 1);
    CHECK(g_h2d_len > 0);
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == calls_before + 1);
    CHECK(g_dev_seen.value == 1234);
    pump_d2h();
    CHECK(g_host.inflight() == 0);
}

static void test_corrupt_frame_nak_retransmit() {
    int calls_before = g_dev_seen.set_value_calls;
    uint32_t crc_before = intentproto::link_stats().crc_errors;
    CHECK(send_set_value(5, 42));
    // Corrupt the queued frame's CRC before delivering it.
    g_h2d[g_h2d_len - 2] ^= 0xff;
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == calls_before);
    CHECK(intentproto::link_stats().crc_errors == crc_before + 1);

    // The device nacked; the host retransmits without waiting for
    // the RTO (the deadline is far in the future here).
    pump_d2h();
    CHECK(g_host.naks == 1);
    CHECK(g_host.need_retransmit(0, 1000000));
    pump_h2d();
    CHECK(g_dev_seen.set_value_calls == calls_before + 1);
    CHECK(g_dev_seen.value == 42);
    pump_d2h();
    CHECK(g_host.inflight() == 0);
}

static void test_bootstrap_sequence_adoption() {
    g_h2d_len = g_d2h_len = 0;
    g_host.init(host_write, nullptr, host_response, nullptr);
    CHECK(send_set_value(6, 600));
    CHECK(g_h2d_len >= intentproto::MESSAGE_MIN);
    CHECK((g_h2d[1] & intentproto::MESSAGE_SEQ_MASK) == 0);
    drop_h2d();

    // First future nak could mean that sequence zero was corrupted.  The
    // host requests a normal retransmit and does not rebase yet.
    queue_empty_peer_frame(8);
    pump_d2h();
    CHECK(g_host.sequence_rebases == 0);
    CHECK(g_host.need_retransmit(0, 1000000));
    CHECK((g_h2d[2] & intentproto::MESSAGE_SEQ_MASK) == 0);
    drop_h2d();

    // A retained peer repeats the same expectation after the clean retry.
    // The host now moves its pending payload to sequence eight and sends it.
    queue_empty_peer_frame(8);
    pump_d2h();
    CHECK(g_host.sequence_rebases == 1);
    CHECK(g_host.receive_seq == 8 && g_host.send_seq == 9);
    CHECK(g_host.inflight() == 1);
    CHECK(g_h2d_len >= intentproto::MESSAGE_MIN);
    CHECK((g_h2d[1] & intentproto::MESSAGE_SEQ_MASK) == 8);
    drop_h2d();

    queue_empty_peer_frame(9);
    pump_d2h();
    CHECK(g_host.inflight() == 0);
}

static void test_no_mid_session_sequence_adoption() {
    g_h2d_len = g_d2h_len = 0;
    g_host.init(host_write, nullptr, host_response, nullptr);
    CHECK(send_set_value(7, 700));
    drop_h2d();
    queue_empty_peer_frame(1);       // accept sequence zero
    pump_d2h();
    CHECK(g_host.inflight() == 0);

    CHECK(send_set_value(7, 701));
    drop_h2d();
    queue_empty_peer_frame(8);
    pump_d2h();
    CHECK(g_host.need_retransmit(0, 1000000));
    drop_h2d();
    queue_empty_peer_frame(8);
    pump_d2h();
    CHECK(g_host.sequence_rebases == 0);
    CHECK(g_host.receive_seq == 1 && g_host.send_seq == 2);
}

int main() {
    intentproto::Config cfg;
    cfg.write = device_write;
    cfg.version = "test-1.0";
    intentproto::init(cfg);
    g_host.init(host_write, nullptr, host_response, nullptr);

    test_round_trip_and_ack();
    test_window_limit();
    test_dropped_frame_retransmit();
    test_corrupt_frame_nak_retransmit();
    test_bootstrap_sequence_adoption();
    test_no_mid_session_sequence_adoption();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
