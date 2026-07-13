// intentproto DatagramCarrier tests: a full HostSession ARQ loopback run
// entirely over the UDP datagram binding (datagram.hpp), proving the
// carrier that composes the two (datagram_carrier.hpp) - the "datagram
// transport bound to the session's framed byte stream" the README
// tracked as unimplemented.
//
// Layout mirrors test_host.cpp, but every frame crossing host<->device
// is wrapped in an authenticated datagram: the host side by the
// DatagramCarrier (HostSession::write = datagram_write_thunk), the
// device side by a plain DatagramTx/Rx pair standing in for
// udp_console.c.

#include "intentproto/datagram_carrier.hpp"
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

using namespace intentproto;

static const uint8_t PSK[16] = {'d', 'g', 'r', 'a', 'm', 'c', 'a', 'r',
                                'r', 'i', 'e', 'r', 'k', 'e', 'y', '0'};

// ---------------- device-side command ----------------
KLIPPER_RESPONSE(echo_status, (uint8_t, oid), (uint32_t, value));

static struct {
    int calls = 0;
    uint8_t oid = 0xff;
    uint32_t value = 0;
} g_dev_seen;

KLIPPER_METHOD(cmd_set_value, (uint8_t, oid), (uint32_t, value)) {
    g_dev_seen.calls++;
    g_dev_seen.oid = oid;
    g_dev_seen.value = value;
    reply(echo_status{oid, value});
}

// ---------------- datagram queues (one record per datagram) ----------
struct DgramQ {
    static constexpr int CAP = 64;
    uint8_t buf[CAP][DATAGRAM_MAX];
    size_t len[CAP];
    int n = 0;
    void push(const uint8_t* d, size_t l) {
        if (n < CAP && l <= DATAGRAM_MAX) {
            memcpy(buf[n], d, l);
            len[n] = l;
            n++;
        }
    }
    void clear() { n = 0; }
};

static DgramQ g_h2d;  // host -> device datagrams
static DgramQ g_d2h;  // device -> host datagrams

// ---------------- host side ----------------
static HostSession g_host;
static DatagramCarrier g_carrier;

static int carrier_send(const uint8_t* dgram, size_t len, void*) {
    g_h2d.push(dgram, len);
    return (int)len;
}

static struct {
    int count = 0;
    uint8_t last[MESSAGE_MAX];
    size_t last_len = 0;
} g_responses;

static void host_response(const uint8_t* payload, size_t len, void*) {
    g_responses.count++;
    if (len <= sizeof(g_responses.last)) {
        memcpy(g_responses.last, payload, len);
        g_responses.last_len = len;
    }
}

// ---------------- device side (stands in for udp_console.c) ----------
static DatagramTx g_devtx;
static DatagramRx g_devrx;

// The device's Config::write: wrap the frame in a datagram toward host.
static int device_write(const uint8_t* data, size_t len, void*) {
    uint8_t out[DATAGRAM_MAX];
    size_t n = datagram_encode(&g_devtx, out, data, len,
                               TrafficClass::Scheduled);
    if (n)
        g_d2h.push(out, n);
    return (int)len;
}

// Deliver queued host->device datagrams into the device registry.
static void pump_h2d() {
    for (int i = 0; i < g_h2d.n; i++) {
        uint8_t tmp[DATAGRAM_MAX];
        memcpy(tmp, g_h2d.buf[i], g_h2d.len[i]);
        const uint8_t* frames = nullptr;
        TrafficClass cls;
        int flen = datagram_decode(&g_devrx, tmp, g_h2d.len[i], &frames,
                                   &cls);
        if (flen > 0 && frames)
            rx(frames, (size_t)flen);
    }
    g_h2d.clear();
}

// Deliver queued device->host datagrams into the carrier/session.
static void pump_d2h() {
    for (int i = 0; i < g_d2h.n; i++)
        g_carrier.on_datagram(g_d2h.buf[i], g_d2h.len[i]);
    g_d2h.clear();
}

static const Command* cmd_by_name(const char* name) {
    for (const Command* c = first_command(); c; c = c->next)
        if (!strcmp(c->name, name))
            return c;
    return nullptr;
}
static const Response* res_by_name(const char* name) {
    for (const Response* r = first_response(); r; r = r->next)
        if (!strcmp(r->name, name))
            return r;
    return nullptr;
}

static bool send_set_value(uint8_t oid, uint32_t value) {
    uint8_t payload[16];
    uint8_t* p = payload;
    p = vlq_encode(p, cmd_by_name("cmd_set_value")->id);
    p = vlq_encode(p, oid);
    p = vlq_encode(p, value);
    return g_host.send_command(payload, (size_t)(p - payload));
}

// ---------------- tests ----------------

static void setup(const uint8_t* psk, size_t psk_len) {
    Config cfg;
    cfg.write = device_write;
    cfg.version = "carriertest";
    cfg.build_version = "test";
    init(cfg);
    datagram_tx_init(&g_devtx, psk, psk_len, 0);
    datagram_rx_init(&g_devrx, psk, psk_len);
    g_host.init(nullptr, nullptr, host_response, nullptr);
    g_carrier.init(&g_host, psk, psk_len, 0, carrier_send, nullptr);
    g_host.write = datagram_write_thunk;
    g_host.write_user = &g_carrier;
    g_h2d.clear();
    g_d2h.clear();
    g_dev_seen.calls = 0;
    g_responses.count = 0;
}

static void test_round_trip_authenticated() {
    setup(PSK, sizeof(PSK));
    CHECK(send_set_value(9, 7777));
    CHECK(g_host.inflight() == 1);
    pump_h2d();
    CHECK(g_dev_seen.calls == 1);
    CHECK(g_dev_seen.oid == 9 && g_dev_seen.value == 7777);

    pump_d2h();
    CHECK(g_host.inflight() == 0);        // response + ack drained window
    CHECK(g_responses.count == 1);
    const uint8_t* p = g_responses.last;
    const uint8_t* end = p + g_responses.last_len;
    uint32_t msgid = 0, oid = 0, value = 0;
    CHECK(vlq_decode(&p, end, &msgid) && msgid == res_by_name("echo_status")->id);
    CHECK(vlq_decode(&p, end, &oid) && oid == 9);
    CHECK(vlq_decode(&p, end, &value) && value == 7777);
    CHECK(p == end);
    CHECK(g_devrx.auth_failures == 0);
}

static void test_tamper_rejected_then_retransmit() {
    setup(PSK, sizeof(PSK));
    CHECK(send_set_value(3, 42));
    // Corrupt the single in-flight datagram's authenticated body.
    CHECK(g_h2d.n == 1);
    g_h2d.buf[0][DATAGRAM_HEADER] ^= 0x80;
    pump_h2d();
    CHECK(g_dev_seen.calls == 0);         // forged datagram dropped
    CHECK(g_devrx.auth_failures == 1);
    CHECK(g_host.inflight() == 1);        // still unacked

    // A retransmit (clean this time) gets through end to end. The
    // deadline arms lazily, so the first poll starts the clock and a
    // later poll fires the go-back-N retransmit through the carrier.
    CHECK(!g_host.need_retransmit(100, 50));
    CHECK(g_host.need_retransmit(150, 50));
    pump_h2d();
    CHECK(g_dev_seen.calls == 1 && g_dev_seen.value == 42);
    pump_d2h();
    CHECK(g_host.inflight() == 0 && g_responses.count == 1);
}

static void test_trust_network_unauthenticated() {
    // psk_len == 0 on both ends: the datagram layer's trust_network mode.
    setup(nullptr, 0);
    CHECK(send_set_value(5, 1234));
    pump_h2d();
    CHECK(g_dev_seen.calls == 1 && g_dev_seen.value == 1234);
    pump_d2h();
    CHECK(g_responses.count == 1);
}

int main() {
    test_round_trip_authenticated();
    test_tamper_rejected_then_retransmit();
    test_trust_network_unauthenticated();
    if (g_failures) {
        printf("test_datagram_carrier: %d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("test_datagram_carrier: all tests passed\n");
    return 0;
}
