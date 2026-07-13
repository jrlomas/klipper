// intentproto optional session-security tests: HKDF-SHA256 (RFC 5869
// vectors), the PSK handshake loopback, session datagram round-trip,
// replay rejection, epoch key rotation, per-board identity, static-PSK
// downgrade, and forgery rejection.

#include "intentproto/session_sec.hpp"
#include "intentproto/hmac.hpp"
#include "intentproto/datagram.hpp"

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

// Parse a lowercase hex string into bytes; n is the byte count.
static void from_hex(const char* hex, uint8_t* out, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned v = 0;
        sscanf(hex + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

// ---- HKDF-SHA256, RFC 5869 Appendix A ----

static void test_hkdf_rfc5869() {
    uint8_t prk[32], okm[82], want[82];

    // A.1 Test Case 1 (basic).
    uint8_t ikm1[22];
    memset(ikm1, 0x0b, sizeof(ikm1));
    uint8_t salt1[13];
    from_hex("000102030405060708090a0b0c", salt1, 13);
    uint8_t info1[10];
    from_hex("f0f1f2f3f4f5f6f7f8f9", info1, 10);
    hkdf_extract(salt1, 13, ikm1, 22, prk);
    from_hex("077709362c2e32df0ddc3f0dc47bba63"
             "90b6c73bb50f9c3122ec844ad7c2b3e5", want, 32);
    CHECK(memcmp(prk, want, 32) == 0);
    hkdf_expand(prk, info1, 10, okm, 42);
    from_hex("3cb25f25faacd57a90434f64d0362f2a"
             "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
             "34007208d5b887185865", want, 42);
    CHECK(memcmp(okm, want, 42) == 0);
    // One-shot path must agree.
    hkdf_sha256(salt1, 13, ikm1, 22, info1, 10, okm, 42);
    CHECK(memcmp(okm, want, 42) == 0);

    // A.2 Test Case 2 (longer inputs and output).
    uint8_t ikm2[80], salt2[80], info2[80];
    for (int i = 0; i < 80; i++) {
        ikm2[i] = (uint8_t)i;
        salt2[i] = (uint8_t)(0x60 + i);
        info2[i] = (uint8_t)(0xb0 + i);
    }
    hkdf_extract(salt2, 80, ikm2, 80, prk);
    from_hex("06a6b88c5853361a06104c9ceb35b45c"
             "ef760014904671014a193f40c15fc244", want, 32);
    CHECK(memcmp(prk, want, 32) == 0);
    hkdf_expand(prk, info2, 80, okm, 82);
    from_hex("b11e398dc80327a1c8e7f78c596a4934"
             "4f012eda2d4efad8a050cc4c19afa97c"
             "59045a99cac7827271cb41c65e590e09"
             "da3275600c2f09b8367793a9aca3db71"
             "cc30c58179ec3e87c14c01d5c1f3434f"
             "1d87", want, 82);
    CHECK(memcmp(okm, want, 82) == 0);

    // A.3 Test Case 3 (zero-length salt and info).
    hkdf_extract(nullptr, 0, ikm1, 22, prk);
    from_hex("19ef24a32c717b167f33a91d6f648bdf"
             "96596776afdb6377ac434c1c293ccb04", want, 32);
    CHECK(memcmp(prk, want, 32) == 0);
    hkdf_expand(prk, nullptr, 0, okm, 42);
    from_hex("8da4e775a563c18f715f802a063c5a31"
             "b8a11f5c5ee1879ec3454e5f3c738d2d"
             "9d201395faa4b61a96c8", want, 42);
    CHECK(memcmp(okm, want, 42) == 0);
}

// ---- handshake fixtures ----

static const uint8_t PSK[16] =
    {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16};
static const uint8_t CLIENT_RAND[16] =
    {0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,
     0x18,0x19,0x1a,0x1b,0x1c,0x1d,0x1e,0x1f};
static const uint8_t SERVER_RAND[16] =
    {0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x27,
     0x28,0x29,0x2a,0x2b,0x2c,0x2d,0x2e,0x2f};
static const char CLIENT_ID[] = "host-a";
static const char SERVER_ID[] = "toolboard-3";

// Run the three-message handshake to Established on both peers.
static void do_handshake(SecureSession* cli, SecureSession* srv,
                         uint32_t rekey = SEC_DEFAULT_REKEY) {
    cli->init(SecRole::Initiator, PSK, sizeof(PSK),
              (const uint8_t*)CLIENT_ID, strlen(CLIENT_ID),
              CLIENT_RAND, rekey);
    srv->init(SecRole::Responder, PSK, sizeof(PSK),
              (const uint8_t*)SERVER_ID, strlen(SERVER_ID),
              SERVER_RAND, rekey);

    uint8_t m[SEC_MSG_MAX];
    size_t n = cli->start(m, sizeof(m));
    CHECK(n > 0);

    uint8_t r[SEC_MSG_MAX];
    size_t rn = srv->on_handshake(m, n, r, sizeof(r));
    CHECK(rn > 0);                       // ServerHello

    uint8_t f[SEC_MSG_MAX];
    size_t fn = cli->on_handshake(r, rn, f, sizeof(f));
    CHECK(fn > 0);                       // ClientFinished
    CHECK(cli->established());

    size_t z = srv->on_handshake(f, fn, m, sizeof(m));
    CHECK(z == 0);
    CHECK(srv->established());
}

static void test_handshake_loopback() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv);

    // Derived keys must cross-match: each peer's tx key equals the
    // other's rx key at the same epoch.
    CHECK(memcmp(cli.tx_key, srv.rx_key, SEC_KEY_SIZE) == 0);
    CHECK(memcmp(cli.rx_key, srv.tx_key, SEC_KEY_SIZE) == 0);
    // And neither traffic key is the raw PSK.
    CHECK(memcmp(cli.tx_key, PSK, sizeof(PSK)) != 0);
}

static void test_identity() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv);
    CHECK(cli.peer_id_len() == strlen(SERVER_ID));
    CHECK(!memcmp(cli.peer_id(), SERVER_ID, strlen(SERVER_ID)));
    CHECK(srv.peer_id_len() == strlen(CLIENT_ID));
    CHECK(!memcmp(srv.peer_id(), CLIENT_ID, strlen(CLIENT_ID)));
}

static void server_finished_for_ids(const char* client_id,
                                    const char* server_id,
                                    uint8_t mac[SEC_FINISHED_SIZE]) {
    SecureSession cli, srv;
    cli.init(SecRole::Initiator, PSK, sizeof(PSK),
             (const uint8_t*)client_id, strlen(client_id), CLIENT_RAND);
    srv.init(SecRole::Responder, PSK, sizeof(PSK),
             (const uint8_t*)server_id, strlen(server_id), SERVER_RAND);
    uint8_t hello[SEC_MSG_MAX], reply[SEC_MSG_MAX];
    size_t hn = cli.start(hello, sizeof(hello));
    size_t rn = srv.on_handshake(hello, hn, reply, sizeof(reply));
    CHECK(rn >= SEC_FINISHED_SIZE);
    memcpy(mac, reply + rn - SEC_FINISHED_SIZE, SEC_FINISHED_SIZE);
}

static void test_identity_lengths_bound() {
    uint8_t split1[SEC_FINISHED_SIZE], split2[SEC_FINISHED_SIZE];
    server_finished_for_ids("A", "BC", split1);
    server_finished_for_ids("AB", "C", split2);
    CHECK(memcmp(split1, split2, SEC_FINISHED_SIZE) != 0);
}

static void test_datagram_roundtrip() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv);

    uint8_t frames[40];
    memset(frames, 0xcd, sizeof(frames));
    uint8_t dg[DATAGRAM_MAX];
    size_t n = cli.datagram_encode(dg, sizeof(dg), frames,
                                   sizeof(frames), TrafficClass::Prompt);
    CHECK(n == SEC_DG_HEADER + 40 + SEC_DG_TAG);
    CHECK(dg[0] & DGF_SESSION);
    CHECK(dg[0] & DGF_AUTH);

    const uint8_t* out;
    TrafficClass cls;
    int r = srv.datagram_decode(dg, n, &out, &cls);
    CHECK(r == 40);
    CHECK(cls == TrafficClass::Prompt);
    CHECK(!memcmp(out, frames, 40));

    // Reply the other direction under the mirror key.
    uint8_t reply[8] = {1,2,3,4,5,6,7,8};
    size_t rn = srv.datagram_encode(dg, sizeof(dg), reply, 8,
                                    TrafficClass::Telemetry);
    r = cli.datagram_decode(dg, rn, &out, &cls);
    CHECK(r == 8);
    CHECK(!memcmp(out, reply, 8));
}

static void test_forgery() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv);

    uint8_t frames[16];
    memset(frames, 0x5a, sizeof(frames));
    uint8_t dg[DATAGRAM_MAX];
    size_t n = cli.datagram_encode(dg, sizeof(dg), frames, 16,
                                   TrafficClass::Scheduled);
    // Flip one payload byte: the session HMAC must reject it.
    dg[SEC_DG_HEADER + 3] ^= 0x01;
    const uint8_t* out;
    TrafficClass cls;
    int r = srv.datagram_decode(dg, n, &out, &cls);
    CHECK(r == -1);
    CHECK(srv.auth_failures == 1);
}

static void test_replay() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv);

    uint8_t f[8] = {7,7,7,7,7,7,7,7};
    uint8_t d0[DATAGRAM_MAX], d1[DATAGRAM_MAX];
    size_t n0 = cli.datagram_encode(d0, sizeof(d0), f, 8,
                                    TrafficClass::Scheduled);
    size_t n1 = cli.datagram_encode(d1, sizeof(d1), f, 8,
                                    TrafficClass::Scheduled);

    const uint8_t* out;
    TrafficClass cls;
    CHECK(srv.datagram_decode(d0, n0, &out, &cls) == 8);
    CHECK(srv.datagram_decode(d1, n1, &out, &cls) == 8);
    // Replaying an in-window sequence is rejected.
    CHECK(srv.datagram_decode(d0, n0, &out, &cls) == -3);
    CHECK(srv.datagram_decode(d1, n1, &out, &cls) == -3);
    CHECK(srv.replays_rejected == 2);

    // A sequence far below the window (simulated by decoding many new
    // ones, then the stale one) is also dropped.
    for (int i = 0; i < 70; i++) {
        uint8_t dn[DATAGRAM_MAX];
        size_t nn = cli.datagram_encode(dn, sizeof(dn), f, 8,
                                        TrafficClass::Scheduled);
        CHECK(srv.datagram_decode(dn, nn, &out, &cls) == 8);
    }
    CHECK(srv.datagram_decode(d0, n0, &out, &cls) == -3);
}

static void test_rotation() {
    SecureSession cli, srv;
    do_handshake(&cli, &srv, 2); // auto-rekey after 2 datagrams/epoch

    uint8_t f[8] = {3,1,4,1,5,9,2,6};
    uint8_t da[DATAGRAM_MAX], db[DATAGRAM_MAX], dc[DATAGRAM_MAX];
    size_t na = cli.datagram_encode(da, sizeof(da), f, 8,
                                    TrafficClass::Scheduled);
    size_t nb = cli.datagram_encode(db, sizeof(db), f, 8,
                                    TrafficClass::Scheduled);
    // The second encode crossed the threshold -> epoch bumped.
    CHECK(cli.tx_epoch == 1);
    size_t nc = cli.datagram_encode(dc, sizeof(dc), f, 8,
                                    TrafficClass::Scheduled);
    CHECK(da[1] == 0 && db[1] == 0 && dc[1] == 1); // epoch bytes

    const uint8_t* out;
    TrafficClass cls;
    CHECK(srv.datagram_decode(da, na, &out, &cls) == 8);
    // Adopt the new epoch on dc; keys re-derived and window reset.
    CHECK(srv.datagram_decode(dc, nc, &out, &cls) == 8);
    CHECK(srv.rx_epoch == 1);
    // The old-epoch datagram db is now stale and must be rejected.
    CHECK(srv.datagram_decode(db, nb, &out, &cls) == -3);
    CHECK(srv.old_epoch_rejected == 1);

    // Explicit rekey path.
    uint32_t before = cli.tx_epoch;
    cli.rekey();
    CHECK(cli.tx_epoch == before + 1);
    uint8_t dd[DATAGRAM_MAX];
    size_t nd = cli.datagram_encode(dd, sizeof(dd), f, 8,
                                    TrafficClass::Scheduled);
    CHECK(srv.datagram_decode(dd, nd, &out, &cls) == 8);
    CHECK(srv.rx_epoch == cli.tx_epoch);
}

static void test_downgrade() {
    // The initiator offers the session; a peer with no session support
    // (here a plain static-PSK receiver) cannot parse the ClientHello,
    // so no ServerHello returns. The initiator downgrades and both
    // fall back to the untouched static-PSK datagram path.
    SecureSession cli;
    cli.init(SecRole::Initiator, PSK, sizeof(PSK),
             (const uint8_t*)CLIENT_ID, strlen(CLIENT_ID), CLIENT_RAND);
    uint8_t hello[SEC_MSG_MAX];
    size_t hn = cli.start(hello, sizeof(hello));
    CHECK(hn > 0);

    DatagramRx legacy_rx;
    datagram_rx_init(&legacy_rx, PSK, sizeof(PSK));
    const uint8_t* out;
    TrafficClass cls;
    // A static-PSK receiver rejects the hello (no valid HMAC tag).
    int r = datagram_decode(&legacy_rx, hello, hn, &out, &cls);
    CHECK(r < 0);

    // No ServerHello arrives -> caller downgrades.
    cli.downgrade();
    CHECK(cli.failed());
    CHECK(!cli.established());

    // Static-PSK path still works end to end.
    DatagramTx tx;
    DatagramRx rx;
    datagram_tx_init(&tx, PSK, sizeof(PSK), 0);
    datagram_rx_init(&rx, PSK, sizeof(PSK));
    uint8_t frames[12];
    memset(frames, 0x33, sizeof(frames));
    uint8_t dg[DATAGRAM_MAX];
    size_t n = datagram_encode(&tx, dg, frames, sizeof(frames),
                               TrafficClass::Scheduled);
    CHECK(datagram_decode(&rx, dg, n, &out, &cls) == 12);
    CHECK(!memcmp(out, frames, 12));
}

static void test_bad_psk_rejected() {
    // A responder holding the wrong PSK rejects ClientHello before
    // allocating handshake state or emitting a ServerHello.
    static const uint8_t BADPSK[16] =
        {9,9,9,9,9,9,9,9,9,9,9,9,9,9,9,9};
    SecureSession cli, srv;
    cli.init(SecRole::Initiator, PSK, sizeof(PSK),
             (const uint8_t*)CLIENT_ID, strlen(CLIENT_ID), CLIENT_RAND);
    srv.init(SecRole::Responder, BADPSK, sizeof(BADPSK),
             (const uint8_t*)SERVER_ID, strlen(SERVER_ID), SERVER_RAND);

    uint8_t m[SEC_MSG_MAX], r[SEC_MSG_MAX];
    size_t n = cli.start(m, sizeof(m));
    size_t rn = srv.on_handshake(m, n, r, sizeof(r));
    CHECK(rn == 0);
    CHECK(srv.state == SecState::Idle);
    CHECK(srv.auth_failures == 1);

    // Tampering with an otherwise valid hello is rejected the same way.
    SecureSession good_srv;
    good_srv.init(SecRole::Responder, PSK, sizeof(PSK),
                  (const uint8_t*)SERVER_ID, strlen(SERVER_ID), SERVER_RAND);
    m[3] ^= 0x40;
    rn = good_srv.on_handshake(m, n, r, sizeof(r));
    CHECK(rn == 0);
    CHECK(good_srv.state == SecState::Idle);
    CHECK(good_srv.auth_failures == 1);

    // The rejected packet did not wedge the responder; a fresh legitimate
    // ClientHello can immediately proceed.
    SecureSession cli2;
    cli2.init(SecRole::Initiator, PSK, sizeof(PSK),
              (const uint8_t*)CLIENT_ID, strlen(CLIENT_ID), CLIENT_RAND);
    n = cli2.start(m, sizeof(m));
    rn = good_srv.on_handshake(m, n, r, sizeof(r));
    CHECK(rn > 0);
}

int main() {
    test_hkdf_rfc5869();
    test_handshake_loopback();
    test_identity();
    test_identity_lengths_bound();
    test_datagram_roundtrip();
    test_forgery();
    test_replay();
    test_rotation();
    test_downgrade();
    test_bad_psk_rejected();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
