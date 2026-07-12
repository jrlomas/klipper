// intentproto CAN carrier tests (RFC 0001 doc 07).
//
//  1. UUID admin handshake: an unassigned node answers QUERY_UNASSIGNED
//     with its UUID and latches the id the host assigns.
//  2. Frame chunking: a whole protocol frame is split into <=8-byte CAN
//     data frames on the device's tx id (assigned_id + 1), reassembling
//     to the original bytes.
//  3. Full round trip: a host session command, chunked into CAN frames
//     and delivered to the device carrier, dispatches to the handler;
//     the device reply, chunked back over CAN, decodes at the host.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
// This file may be distributed under the terms of the MIT license.

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>

#include "intentproto/can_transport.hpp"
#include "intentproto/host.hpp"
#include "intentproto/method.hpp"
#include "intentproto/proto.hpp"

static int g_failures = 0;
#define CHECK(c) do { if (!(c)) { \
    printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); g_failures++; } } while (0)

// ---- a device command + reply, as the working example ----
static struct { int calls; uint8_t oid; uint32_t value; } g_seen;

KLIPPER_RESPONSE(can_echo, (uint8_t, oid), (uint32_t, value));

KLIPPER_METHOD(can_set_value, (uint8_t, oid), (uint32_t, value)) {
    g_seen.calls++;
    g_seen.oid = oid;
    g_seen.value = value;
    intentproto::reply(can_echo{oid, value});
}

// ---- CAN "bus": two carriers/endpoints wired by a frame queue ----
// The device carrier is the unit under test. The "host side" is modeled
// minimally: it drives admin, forwards device->host data to a host
// session, and chunks host->device bytes into CAN frames.

static std::vector<intentproto::CanFrame> g_dev_tx; // frames device emits
static int dev_send(const intentproto::CanFrame& f, void*) {
    g_dev_tx.push_back(f);
    return 0;
}

static intentproto::CanCarrier g_dev;

// Host session plumbing: HostSession writes whole frames; we chunk them
// into CAN and hand each to the device carrier as bus traffic.
static intentproto::HostSession g_host;

static int host_write(const uint8_t* data, size_t len, void*) {
    // Split into <=8-byte CAN frames on the device's rx id and deliver.
    size_t off = 0;
    while (off < len) {
        size_t n = len - off; if (n > 8) n = 8;
        intentproto::CanFrame f;
        f.id = g_dev.rx_id();
        f.dlc = (uint8_t)n;
        memcpy(f.data, data + off, n);
        g_dev.on_can_frame(f);
        off += n;
    }
    return (int)len;
}

// Device replies arrive as CAN frames in g_dev_tx; reassemble and feed
// the host session's on_rx.
static struct { int count; uint8_t last[64]; size_t last_len; } g_resp;
static void host_response(const uint8_t* data, size_t len, void*) {
    g_resp.count++;
    g_resp.last_len = len < sizeof(g_resp.last) ? len : sizeof(g_resp.last);
    memcpy(g_resp.last, data, g_resp.last_len);
}
static void pump_device_replies() {
    for (auto& f : g_dev_tx) {
        CHECK(f.id == g_dev.rx_id() + 1);   // device -> host id
        g_host.on_rx(f.data, f.dlc);
    }
    g_dev_tx.clear();
}

static const intentproto::Command* cmd_by_name(const char* name) {
    for (const intentproto::Command* c = intentproto::first_command(); c;
         c = c->next)
        if (!strcmp(c->name, name)) return c;
    return nullptr;
}
static const intentproto::Response* res_by_name(const char* name) {
    for (const intentproto::Response* r = intentproto::first_response(); r;
         r = r->next)
        if (!strcmp(r->name, name)) return r;
    return nullptr;
}

// ---- tests ----

static const uint8_t UUID[6] = {0x11,0x22,0x33,0x44,0x55,0x66};

static void test_admin_assignment() {
    // Unassigned: a data frame on a stale id is ignored, write refused.
    CHECK(g_dev.node_id() == -1);
    CHECK(g_dev.write_frame(UUID, 6) == -1);

    // Host queries unassigned nodes; device answers with its UUID.
    intentproto::CanFrame q;
    q.id = intentproto::CAN_ID_ADMIN; q.dlc = 1;
    q.data[0] = intentproto::CAN_CMD_QUERY_UNASSIGNED;
    g_dev.on_can_frame(q);
    CHECK(g_dev_tx.size() == 1);
    CHECK(g_dev_tx[0].id == intentproto::CAN_ID_ADMIN_RESP);
    CHECK(g_dev_tx[0].data[0] == intentproto::CAN_RESP_NEED_NODEID);
    CHECK(memcmp(&g_dev_tx[0].data[1], UUID, 6) == 0);
    g_dev_tx.clear();

    // Host assigns node id 5 to this UUID.
    intentproto::CanFrame s;
    s.id = intentproto::CAN_ID_ADMIN; s.dlc = 8;
    s.data[0] = intentproto::CAN_CMD_SET_NODEID;
    memcpy(&s.data[1], UUID, 6);
    s.data[7] = 5;
    g_dev.on_can_frame(s);
    CHECK(g_dev.node_id() == 5);
    CHECK(g_dev.rx_id() == intentproto::can_nodeid_to_id(5));

    // A SET_NODEID for a DIFFERENT uuid must not steal our id.
    intentproto::CanFrame s2 = s;
    s2.data[1] = 0xff;
    g_dev.on_can_frame(s2);
    CHECK(g_dev.node_id() == 5);
    printf("PASS: admin UUID handshake assigns and latches a node id\n");
}

static void test_frame_chunking() {
    uint8_t buf[19];
    for (size_t i = 0; i < sizeof(buf); i++) buf[i] = (uint8_t)(i + 1);
    g_dev_tx.clear();
    CHECK(g_dev.write_frame(buf, sizeof(buf)) == (int)sizeof(buf));
    // 19 bytes -> 8 + 8 + 3, all on the device tx id.
    CHECK(g_dev_tx.size() == 3);
    CHECK(g_dev_tx[0].dlc == 8 && g_dev_tx[1].dlc == 8 && g_dev_tx[2].dlc == 3);
    std::vector<uint8_t> reassembled;
    for (auto& f : g_dev_tx) {
        CHECK(f.id == g_dev.rx_id() + 1);
        reassembled.insert(reassembled.end(), f.data, f.data + f.dlc);
    }
    CHECK(reassembled.size() == sizeof(buf));
    CHECK(memcmp(reassembled.data(), buf, sizeof(buf)) == 0);
    g_dev_tx.clear();
    printf("PASS: a whole frame chunks into <=8-byte CAN data frames\n");
}

static bool send_set_value(uint8_t oid, uint32_t value) {
    uint8_t payload[16];
    uint8_t* p = payload;
    p = intentproto::vlq_encode(p, cmd_by_name("can_set_value")->id);
    p = intentproto::vlq_encode(p, oid);
    p = intentproto::vlq_encode(p, value);
    return g_host.send_command(payload, (size_t)(p - payload));
}

static void test_round_trip_over_can() {
    g_resp.count = 0;
    int before = g_seen.calls;
    CHECK(send_set_value(9, 7777));   // host frame -> host_write -> CAN
    CHECK(g_seen.calls == before + 1); // dispatched through rx()
    CHECK(g_seen.oid == 9 && g_seen.value == 7777);

    pump_device_replies();            // device reply CAN frames -> host
    CHECK(g_host.inflight() == 0);    // ack drained the window
    CHECK(g_resp.count == 1);
    const uint8_t* p = g_resp.last;
    const uint8_t* end = p + g_resp.last_len;
    uint32_t msgid = 0, oid = 0, value = 0;
    CHECK(intentproto::vlq_decode(&p, end, &msgid));
    CHECK(msgid == res_by_name("can_echo")->id);
    CHECK(intentproto::vlq_decode(&p, end, &oid) && oid == 9);
    CHECK(intentproto::vlq_decode(&p, end, &value) && value == 7777);
    CHECK(p == end);
    printf("PASS: host command and device reply round-trip over CAN\n");
}

int main() {
    intentproto::Config cfg;
    g_dev.init(UUID, dev_send, nullptr);
    cfg.write = intentproto::can_write_thunk;
    cfg.user = &g_dev;
    cfg.version = "can-test-1.0";
    intentproto::init(cfg);
    g_host.init(host_write, nullptr, host_response, nullptr);

    test_admin_assignment();
    test_frame_chunking();
    test_round_trip_over_can();

    if (g_failures) { printf("%d FAILURE(S)\n", g_failures); return 1; }
    printf("all tests passed\n");
    return 0;
}
