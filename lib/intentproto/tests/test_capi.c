/* intentproto C API test — compiled as C (not C++).
 *
 * Purpose (FD-0001 doc 10, host profile):
 *   1. prove capi.h is valid C and the ABI links from a C translation
 *      unit with no C++ in sight;
 *   2. drive a host-session loopback against the device singleton's
 *      rx() entirely through the extern "C" surface — the same
 *      round-trip test_host.cpp does from C++, but from C;
 *   3. exercise the stateless framing codecs and the datagram binding
 *      through the shim.
 *
 * The device registry here holds only the library-owned meta-commands
 * (no KLIPPER_METHOD declarations link into this TU), so the loopback
 * enumerates the device's own constants (FRAMING_V2) over
 * list_constants — self-describing all the way down.
 */

#include "intentproto/capi.h"

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

/* ---------------- buffered loopback plumbing ---------------- */

static uint8_t g_h2d[4096];
static size_t g_h2d_len = 0;
static uint8_t g_d2h[4096];
static size_t g_d2h_len = 0;

static int host_write(const uint8_t *data, size_t len, void *user) {
    (void)user;
    if (g_h2d_len + len <= sizeof(g_h2d)) {
        memcpy(g_h2d + g_h2d_len, data, len);
        g_h2d_len += len;
    }
    return (int)len;
}

static int device_write(const uint8_t *data, size_t len, void *user) {
    (void)user;
    if (g_d2h_len + len <= sizeof(g_d2h)) {
        memcpy(g_d2h + g_d2h_len, data, len);
        g_d2h_len += len;
    }
    return (int)len;
}

#define MAX_RESP 16
static uint8_t g_resp[MAX_RESP][IP_MESSAGE_MAX];
static size_t g_resp_len[MAX_RESP];
static int g_resp_count = 0;

static void host_response(const uint8_t *payload, size_t len, void *user) {
    (void)user;
    if (g_resp_count < MAX_RESP && len <= IP_MESSAGE_MAX) {
        memcpy(g_resp[g_resp_count], payload, len);
        g_resp_len[g_resp_count] = len;
        g_resp_count++;
    }
}

static ip_host_session *g_host;

static void pump_h2d(void) {
    ip_device_rx(g_h2d, g_h2d_len);
    g_h2d_len = 0;
}
static void pump_d2h(void) {
    ip_host_session_on_rx(g_host, g_d2h, g_d2h_len);
    g_d2h_len = 0;
}

static int response_index_by_name(const char *name) {
    int i;
    for (i = 0; i < ip_response_count(); i++) {
        const char *n = ip_response_name(i);
        if (n && !strcmp(n, name))
            return i;
    }
    return -1;
}

/* ---------------- tests ---------------- */

static void test_abi_version(void) {
    CHECK(intentproto_abi_version() == INTENTPROTO_ABI_VERSION);
    CHECK(INTENTPROTO_ABI_VERSION_MAJOR == 1);
    CHECK(intentproto_version_string() != NULL);
}

static void test_framing_codecs(void) {
    /* CRC over a known buffer matches whatever the library computes for
     * both directions of the same bytes (stability, not a magic
     * number). */
    uint8_t buf[4] = {0x01, 0x02, 0x03, 0x04};
    uint16_t crc = ip_crc16_ccitt(buf, sizeof(buf));
    CHECK(crc == ip_crc16_ccitt(buf, sizeof(buf)));

    /* VLQ round-trips across the sign boundaries. */
    uint32_t samples[6] = {0u, 1u, 95u, 300u, 0x7fffffffu, 0xdeadbeefu};
    int i;
    for (i = 0; i < 6; i++) {
        uint8_t enc[8];
        size_t n = ip_vlq_encode(enc, samples[i]);
        CHECK(n >= 1 && n <= 5);
        uint32_t out = 0;
        size_t used = ip_vlq_decode(enc, n, &out);
        CHECK(used == n);
        CHECK(out == samples[i]);
    }
    /* Truncated input decodes to 0 consumed. */
    uint8_t trunc[1] = {0x80};
    uint32_t dummy = 0;
    CHECK(ip_vlq_decode(trunc, 1, &dummy) == 0);
}

static void test_frame_v2(void) {
    uint8_t payload[8] = {0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11};
    uint8_t frame[64];
    size_t flen = ip_frame_v2_encode(frame, payload, sizeof(payload), 5);
    CHECK(flen == sizeof(payload) + 7);

    size_t off = 0;
    uint8_t seq = 0xff;
    int corrected = -1;
    int plen = ip_frame_v2_decode(frame, flen, &off, &seq, &corrected);
    CHECK(plen == (int)sizeof(payload));
    CHECK(seq == 5);
    CHECK(corrected == 0);
    CHECK(!memcmp(frame + off, payload, sizeof(payload)));

    /* One flipped payload bit is corrected by the BCH trailer. */
    flen = ip_frame_v2_encode(frame, payload, sizeof(payload), 6);
    frame[2] ^= 0x01;
    plen = ip_frame_v2_decode(frame, flen, &off, &seq, &corrected);
    CHECK(plen == (int)sizeof(payload));
    CHECK(corrected >= 1);
    CHECK(!memcmp(frame + off, payload, sizeof(payload)));
}

static void test_datagram_round_trip(void) {
    static const uint8_t psk[16] = {1, 2, 3, 4, 5, 6, 7, 8,
                                    9, 10, 11, 12, 13, 14, 15, 16};
    ip_datagram_tx *tx = ip_datagram_tx_create(psk, sizeof(psk), 0);
    ip_datagram_rx *rx = ip_datagram_rx_create(psk, sizeof(psk));
    CHECK(tx != NULL && rx != NULL);

    uint8_t frames[8] = {0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27};
    uint8_t dg[256];
    size_t dn = ip_datagram_encode(tx, dg, frames, sizeof(frames),
                                   IP_CLASS_SCHEDULED);
    CHECK(dn > sizeof(frames));

    size_t off = 0;
    int cls = -1;
    int fn = ip_datagram_decode(rx, dg, dn, &off, &cls);
    CHECK(fn == (int)sizeof(frames));
    CHECK(cls == IP_CLASS_SCHEDULED);
    CHECK(!memcmp(dg + off, frames, sizeof(frames)));

    ip_datagram_tx_free(tx);
    ip_datagram_rx_free(rx);
}

static void test_host_loopback(void) {
    /* Drive list_constants through the host session; the device
     * answers with its own registry as data. */
    int cmd_idx = ip_command_index_by_name("list_constants");
    CHECK(cmd_idx >= 0);
    uint32_t msgid = ip_command_id(cmd_idx);
    CHECK(msgid >= 2);

    int const_desc = response_index_by_name("constant_desc");
    int done = response_index_by_name("extension_done");
    CHECK(const_desc >= 0 && done >= 0);
    uint32_t const_desc_id = ip_response_id(const_desc);
    uint32_t done_id = ip_response_id(done);

    uint8_t payload[16];
    size_t pl = 0;
    pl += ip_vlq_encode(payload + pl, msgid);
    pl += ip_vlq_encode(payload + pl, 0); /* start */
    pl += ip_vlq_encode(payload + pl, 8); /* count */

    g_resp_count = 0;
    CHECK(ip_host_session_send_command(g_host, payload, pl,
                                       IP_CLASS_SCHEDULED) == 1);
    CHECK(ip_host_session_inflight(g_host) == 1);

    pump_h2d();
    pump_d2h();

    /* The ack drained the window and the responses were delivered. */
    CHECK(ip_host_session_inflight(g_host) == 0);
    CHECK(g_resp_count >= 2);

    /* Find the FRAMING_V2 constant_desc and the terminating
     * extension_done in the delivered payloads. */
    int saw_framing_v2 = 0, saw_done = 0;
    int i;
    for (i = 0; i < g_resp_count; i++) {
        const uint8_t *p = g_resp[i];
        size_t rem = g_resp_len[i];
        uint32_t id = 0;
        size_t used = ip_vlq_decode(p, rem, &id);
        CHECK(used > 0);
        p += used;
        rem -= used;
        if (id == const_desc_id) {
            uint32_t kind = 0;
            used = ip_vlq_decode(p, rem, &kind);
            p += used;
            rem -= used;
            uint32_t dlen = 0;
            used = ip_vlq_decode(p, rem, &dlen);
            p += used;
            rem -= used;
            if (dlen == strlen("FRAMING_V2=1")
                && !memcmp(p, "FRAMING_V2=1", dlen))
                saw_framing_v2 = 1;
        } else if (id == done_id) {
            saw_done = 1;
        }
    }
    CHECK(saw_framing_v2);
    CHECK(saw_done);

    /* Nothing in flight: no retransmit however late the poll. */
    CHECK(ip_host_session_need_retransmit(g_host, 1000000, 1) == 0);
    ip_host_diag diag;
    ip_host_session_diag(g_host, &diag);
    CHECK(diag.retransmits == 0);
    CHECK(diag.naks == 0);
    CHECK(ip_host_session_sequence_rebases(g_host) == 0);

    ip_class_stats cs;
    ip_host_session_class_stats(g_host, IP_CLASS_SCHEDULED, &cs);
    CHECK(cs.tx_msgs >= 1);
}

int main(void) {
    ip_device_init(device_write, NULL, "capi-test", "capi-build");
    g_host = ip_host_session_create(host_write, NULL, host_response, NULL,
                                    IP_FRAMING_LEGACY);
    if (!g_host) {
        printf("FAIL: host session allocation\n");
        return 1;
    }

    test_abi_version();
    test_framing_codecs();
    test_frame_v2();
    test_datagram_round_trip();
    test_host_loopback();

    ip_host_session_free(g_host);

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
