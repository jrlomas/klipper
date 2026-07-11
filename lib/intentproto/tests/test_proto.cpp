// intentproto desktop tests.
//
// The declarations below are a slice of the OpenAMS firmware's real
// command set, ported to the static-registration layer — they double
// as the ergonomics demo: each command is ONE annotation plus its
// body; there is no table, no registration call, and no build step.

#include "intentproto/method.hpp"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

// ---------------- the OAMS slice ----------------

KLIPPER_CONSTANT(CLOCK_FREQ, 48000000);
KLIPPER_CONSTANT_STR(MCU, "oams-stm32f072rbt6");

KLIPPER_RESPONSE(oams_action_status,
                 (uint8_t, action), (uint8_t, code), (uint32_t, value));

KLIPPER_RESPONSE(oams_encoder_clicks, (uint32_t, clicks));

// Captured handler arguments for assertions.
static struct {
    int load_spool_calls = 0;
    uint8_t load_spool_arg = 0xff;
    int unload_calls = 0;
    int pid_calls = 0;
    uint32_t kp = 0, ki = 0, kd = 0;
    int follower_calls = 0;
    uint8_t follower_enable = 0;
    int16_t follower_trim = 0;
} g_seen;

static bool g_busy = false;

KLIPPER_METHOD(oams_cmd_load_spool, (uint8_t, spool)) {
    g_seen.load_spool_calls++;
    g_seen.load_spool_arg = spool;
    if (g_busy) {
        intentproto::reply(oams_action_status{/*action=*/0, /*code=*/2, 0});
        return;
    }
    intentproto::reply(oams_action_status{/*action=*/0, /*code=*/0, spool});
}

KLIPPER_METHOD0(oams_cmd_unload_spool) {
    g_seen.unload_calls++;
}

KLIPPER_METHOD(oams_cmd_pid_set,
               (uint32_t, kp), (uint32_t, ki), (uint32_t, kd)) {
    g_seen.pid_calls++;
    g_seen.kp = kp;
    g_seen.ki = ki;
    g_seen.kd = kd;
}

KLIPPER_METHOD(oams_cmd_follower, (uint8_t, enable), (int16_t, trim)) {
    g_seen.follower_calls++;
    g_seen.follower_enable = enable;
    g_seen.follower_trim = trim;
}

KLIPPER_METHOD0(oams_cmd_encoder_clicks) {
    intentproto::reply(oams_encoder_clicks{123456});
}

// ---------------- test transport ----------------

static uint8_t g_tx_buf[4096];
static size_t g_tx_len = 0;

static int test_write(const uint8_t* data, size_t len, void*) {
    if (g_tx_len + len <= sizeof(g_tx_buf)) {
        memcpy(g_tx_buf + g_tx_len, data, len);
        g_tx_len += len;
    }
    return (int)len;
}

static void tx_reset() { g_tx_len = 0; }

// Split the captured tx stream into frames; returns frame count.
static int tx_frames(const uint8_t* frames[], int max) {
    int n = 0;
    size_t pos = 0;
    while (pos < g_tx_len && n < max) {
        uint8_t len = g_tx_buf[pos];
        if (len < intentproto::MESSAGE_MIN || pos + len > g_tx_len)
            break;
        frames[n++] = g_tx_buf + pos;
        pos += len;
    }
    return n;
}

// Host-side frame builder for driving the MCU-side library.
struct HostFrame {
    uint8_t buf[intentproto::MESSAGE_MAX];
    uint8_t* p;
    uint8_t seq;
    explicit HostFrame(uint8_t seq_) : p(buf + 2), seq(seq_) {}
    HostFrame& put(uint32_t v) {
        p = intentproto::vlq_encode(p, v);
        return *this;
    }
    size_t finish() {
        size_t total = (size_t)(p - buf) + intentproto::TRAILER_SIZE;
        buf[0] = (uint8_t)total;
        buf[1] = (uint8_t)(intentproto::MESSAGE_DEST
                           | (seq & intentproto::MESSAGE_SEQ_MASK));
        uint16_t crc = intentproto::crc16_ccitt(buf, total - 3);
        buf[total - 3] = (uint8_t)(crc >> 8);
        buf[total - 2] = (uint8_t)(crc & 0xff);
        buf[total - 1] = intentproto::MESSAGE_SYNC;
        return total;
    }
};

// Decode a response frame payload: returns msgid, leaves *pp at args.
static uint32_t frame_msgid(const uint8_t* frame, const uint8_t** pp,
                            const uint8_t** end) {
    *pp = frame + 2;
    *end = frame + frame[0] - 3;
    uint32_t id = 0;
    intentproto::vlq_decode(pp, *end, &id);
    return id;
}

static uint32_t take_u32(const uint8_t** pp, const uint8_t* end) {
    uint32_t v = 0;
    CHECK(intentproto::vlq_decode(pp, end, &v));
    return v;
}

// ---------------- tests ----------------

static void test_crc16() {
    const uint8_t vec[] = "123456789";
    CHECK(intentproto::crc16_ccitt(vec, 9) == 0x29b1);
}

static void test_vlq_roundtrip() {
    const int32_t cases[] = {
        0, 1, -1, 31, 32, 95, 96, -32, -33, 127, 128, 300, 4095, -4096,
        12287, 12288, (1 << 20), -(1 << 20), (int32_t)0x7fffffff,
        (int32_t)0x80000000, (int32_t)0xdeadbeef,
    };
    for (int32_t v : cases) {
        uint8_t buf[8];
        uint8_t* wend = intentproto::vlq_encode(buf, (uint32_t)v);
        const uint8_t* rp = buf;
        uint32_t got = 0;
        CHECK(intentproto::vlq_decode(&rp, wend, &got));
        CHECK(rp == wend);
        CHECK((int32_t)got == v);
    }
    // One-byte range per the legacy protocol: -32..95.
    uint8_t buf[8];
    CHECK(intentproto::vlq_encode(buf, (uint32_t)95) - buf == 1);
    CHECK(intentproto::vlq_encode(buf, (uint32_t)-32) - buf == 1);
    CHECK(intentproto::vlq_encode(buf, (uint32_t)96) - buf == 2);
    CHECK(intentproto::vlq_encode(buf, (uint32_t)-33) - buf == 2);
    // Truncated input is rejected, not overrun.
    uint8_t trunc[] = {0x80};
    const uint8_t* rp = trunc;
    uint32_t out;
    CHECK(!intentproto::vlq_decode(&rp, trunc + 1, &out));
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

static void test_registry() {
    const intentproto::Command* c = cmd_by_name("oams_cmd_load_spool");
    CHECK(c != nullptr);
    CHECK(c->num_params == 1);
    CHECK(c->param_types[0] == intentproto::ParamType::U8);
    CHECK(!strcmp(c->param_names[0], "spool"));
    CHECK(c->id >= intentproto::MSGID_FIRST_FREE);

    const intentproto::Command* f = cmd_by_name("oams_cmd_follower");
    CHECK(f != nullptr);
    CHECK(f->num_params == 2);
    CHECK(f->param_types[1] == intentproto::ParamType::I16);

    // Definition order => id order.
    CHECK(cmd_by_name("oams_cmd_unload_spool")->id == c->id + 1);

    const intentproto::Response* r = res_by_name("oams_action_status");
    CHECK(r != nullptr);
    CHECK(r->num_fields == 3);
    CHECK(r->field_types[2] == intentproto::ParamType::U32);
}

static void test_dispatch_and_reply() {
    tx_reset();
    g_busy = false;

    const intentproto::Command* load = cmd_by_name("oams_cmd_load_spool");
    HostFrame f(0);
    f.put(load->id).put(3);
    size_t n = f.finish();
    intentproto::rx(f.buf, n);

    CHECK(g_seen.load_spool_calls == 1);
    CHECK(g_seen.load_spool_arg == 3);

    // Expect a response frame (oams_action_status) then the ack.
    const uint8_t* frames[8];
    int nf = tx_frames(frames, 8);
    CHECK(nf == 2);
    if (nf < 2)
        return;
    const uint8_t *p, *end;
    uint32_t msgid = frame_msgid(frames[0], &p, &end);
    CHECK(msgid == res_by_name("oams_action_status")->id);
    CHECK(take_u32(&p, end) == 0);   // action
    CHECK(take_u32(&p, end) == 0);   // code = ok
    CHECK(take_u32(&p, end) == 3);   // value = spool
    CHECK(p == end);
    // ack: empty payload, seq advanced, dest bit set
    CHECK(frames[1][0] == intentproto::MESSAGE_MIN);
    CHECK(frames[1][1] == (intentproto::MESSAGE_DEST | 1));
}

static void test_multi_message_block_and_signs() {
    tx_reset();
    const intentproto::Command* pid = cmd_by_name("oams_cmd_pid_set");
    const intentproto::Command* fol = cmd_by_name("oams_cmd_follower");
    // Two commands in one block; negative i16 exercises sign handling.
    HostFrame f(1);
    f.put(pid->id).put(400000).put(25).put(0);
    f.put(fol->id).put(1).put((uint32_t)(int32_t)-123);
    size_t n = f.finish();
    intentproto::rx(f.buf, n);

    CHECK(g_seen.pid_calls == 1);
    CHECK(g_seen.kp == 400000 && g_seen.ki == 25 && g_seen.kd == 0);
    CHECK(g_seen.follower_calls == 1);
    CHECK(g_seen.follower_enable == 1);
    CHECK(g_seen.follower_trim == -123);
}

static void test_byte_at_a_time_rx_and_crc_nack() {
    tx_reset();
    const intentproto::Command* unload = cmd_by_name("oams_cmd_unload_spool");
    HostFrame f(2);
    f.put(unload->id);
    size_t n = f.finish();
    // Feed one byte at a time — chunking must not matter.
    for (size_t i = 0; i < n; i++)
        intentproto::rx(f.buf + i, 1);
    CHECK(g_seen.unload_calls == 1);

    // Corrupt the CRC: expect a nack (seq - 1), handler NOT called.
    tx_reset();
    HostFrame g(3);
    g.put(unload->id);
    size_t gn = g.finish();
    g.buf[gn - 2] ^= 0xff;
    intentproto::rx(g.buf, gn);
    CHECK(g_seen.unload_calls == 1);
    CHECK(intentproto::link_stats().crc_errors == 1);
    const uint8_t* frames[4];
    int nf = tx_frames(frames, 4);
    CHECK(nf == 1);
    if (nf >= 1)
        CHECK(frames[0][1] == (intentproto::MESSAGE_DEST | 2));  // 3 - 1
}

static void test_identify() {
    // Serve a recognizable blob in two chunks.
    static const char blob[] = "0123456789abcdefghij";
    intentproto::Config cfg = intentproto::current_config();
    cfg.identify_blob = (const uint8_t*)blob;
    cfg.identify_blob_len = 20;
    intentproto::init(cfg);

    tx_reset();
    HostFrame f(0);
    f.put(intentproto::MSGID_IDENTIFY).put(12).put(40);
    intentproto::rx(f.buf, f.finish());

    const uint8_t* frames[4];
    int nf = tx_frames(frames, 4);
    CHECK(nf == 2);   // identify_response + ack
    if (nf < 2)
        return;
    const uint8_t *p, *end;
    CHECK(frame_msgid(frames[0], &p, &end)
          == intentproto::MSGID_IDENTIFY_RESPONSE);
    CHECK(take_u32(&p, end) == 12);          // offset echoed
    uint32_t dlen = take_u32(&p, end);       // buffer: length prefix
    CHECK(dlen == 8);                        // 20 - 12 remaining
    CHECK(!memcmp(p, "cdefghij", 8));
}

static void test_dictionary() {
    char json[4096];
    size_t n = intentproto::build_dictionary(json, sizeof(json));
    CHECK(n > 0);

    char key[128];
    const intentproto::Command* load = cmd_by_name("oams_cmd_load_spool");
    snprintf(key, sizeof(key), "\"oams_cmd_load_spool spool=%%c\":%u",
             load->id);
    CHECK(strstr(json, key) != nullptr);

    const intentproto::Response* st = res_by_name("oams_action_status");
    snprintf(key, sizeof(key),
             "\"oams_action_status action=%%c code=%%c value=%%u\":%u",
             st->id);
    CHECK(strstr(json, key) != nullptr);

    CHECK(strstr(json, "\"identify_response offset=%u data=%.*s\":0"));
    CHECK(strstr(json, "\"CLOCK_FREQ\":48000000"));
    CHECK(strstr(json, "\"MCU\":\"oams-stm32f072rbt6\""));
    CHECK(strstr(json, "\"version\":\"test-1.0\""));
}

int main() {
    intentproto::Config cfg;
    cfg.write = test_write;
    cfg.version = "test-1.0";
    cfg.build_version = "test-build";
    intentproto::init(cfg);

    test_crc16();
    test_vlq_roundtrip();
    test_registry();
    test_dispatch_and_reply();
    test_multi_message_block_and_signs();
    test_byte_at_a_time_rx_and_crc_nack();
    test_identify();
    test_dictionary();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
