// Host unit test for the nano_udp responder's pure framing helpers
// (src/generic/nano_udp.c).  Checks the Internet checksum against the
// canonical RFC 1071 / Wikipedia IPv4 example, and ARP/UDP framing
// against known-good byte vectors.
//
// Build and run (see test/nano_udp/README for the exact command):
//   cc -DNANO_UDP_TEST -Isrc -Isrc/generic
//      test/nano_udp/nano_udp_test.c src/generic/nano_udp.c -o nut && ./nut
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "generic/nano_udp.h"

static int failures;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

// Canonical IPv4 header checksum example (Wikipedia "IPv4 header
// checksum"): the 20-byte header below, with the checksum field zero,
// must yield 0xb861; folding the complete header (checksum included)
// back through must yield 0x0000.
static const uint8_t ip_hdr[20] = {
    0x45, 0x00, 0x00, 0x73, 0x00, 0x00, 0x40, 0x00, 0x40, 0x11,
    0x00, 0x00, // checksum field (zero for computation)
    0xc0, 0xa8, 0x00, 0x01, 0xc0, 0xa8, 0x00, 0xc7
};

static void
test_checksum(void)
{
    uint16_t c = nano_ip_checksum(ip_hdr, sizeof(ip_hdr), 0);
    CHECK(c == 0xb861);

    uint8_t full[20];
    memcpy(full, ip_hdr, 20);
    full[10] = 0xb8;
    full[11] = 0x61;
    CHECK(nano_ip_checksum(full, sizeof(full), 0) == 0x0000);
}

static void
test_arp(void)
{
    const uint8_t our_mac[6] = { 0x02, 0x00, 0xC0, 0xA8, 0x00, 0xFE };
    uint32_t our_ip = 0xC0A800FE; // 192.168.0.254

    // ARP request: who has 192.168.0.254? tell 192.168.0.10 (aa:bb..)
    uint8_t req[28] = {
        0x00, 0x01,             // htype ethernet
        0x08, 0x00,             // ptype IPv4
        0x06, 0x04,             // hlen, plen
        0x00, 0x01,             // oper request
        0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, // sender hw
        0xc0, 0xa8, 0x00, 0x0a, // sender ip 192.168.0.10
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // target hw (unknown)
        0xc0, 0xa8, 0x00, 0xfe  // target ip 192.168.0.254 (us)
    };
    uint8_t out[28];
    uint32_t n = nano_arp_build_reply(req, sizeof(req), our_mac, our_ip, out);
    CHECK(n == 28);
    CHECK(out[6] == 0x00 && out[7] == 0x02);       // oper = reply
    CHECK(memcmp(out + 8, our_mac, 6) == 0);        // sender hw = us
    CHECK(memcmp(out + 14, "\xc0\xa8\x00\xfe", 4) == 0); // sender ip = us
    CHECK(memcmp(out + 18, req + 8, 6) == 0);       // target hw = requester
    CHECK(memcmp(out + 24, "\xc0\xa8\x00\x0a", 4) == 0); // target ip

    // A request for a different IP must be ignored
    req[27] = 0x0b; // target ip now .11, not us
    CHECK(nano_arp_build_reply(req, sizeof(req), our_mac, our_ip, out) == 0);
}

static void
test_udp_roundtrip(void)
{
    const uint8_t src_mac[6] = { 0x02, 0x00, 0xC0, 0xA8, 0x00, 0xFE };
    const uint8_t dst_mac[6] = { 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff };
    uint32_t src_ip = 0xC0A800FE, dst_ip = 0xC0A8000A;
    uint16_t src_port = 1234, dst_port = 5678;
    const uint8_t payload[] = { 'k', 'l', 'i', 'p', 'p', 'e', 'r', '!' };

    uint8_t frame[128];
    uint32_t flen = nano_udp_build_frame(frame, sizeof(frame), src_mac
                                         , dst_mac, src_ip, dst_ip
                                         , src_port, dst_port
                                         , payload, sizeof(payload));
    CHECK(flen == NANO_UDP_OVERHEAD + sizeof(payload));
    CHECK(frame[12] == 0x08 && frame[13] == 0x00); // ethertype IPv4
    CHECK(frame[NANO_ETH_HLEN + 9] == 17);          // IP proto UDP

    // IP header checksum must fold to zero over the built header
    CHECK(nano_ip_checksum(frame + NANO_ETH_HLEN, NANO_IP_HLEN, 0) == 0);

    // UDP checksum (pseudo-header + udp header + data) must fold to zero
    const uint8_t *udp = frame + NANO_ETH_HLEN + NANO_IP_HLEN;
    uint16_t udp_len = NANO_UDP_HLEN + sizeof(payload);
    uint32_t psum = ((src_ip >> 16) & 0xffff) + (src_ip & 0xffff)
                    + ((dst_ip >> 16) & 0xffff) + (dst_ip & 0xffff)
                    + 17 + udp_len;
    CHECK(nano_ip_checksum(udp, udp_len, psum) == 0);

    // Parse it back as if received (swap the view: we are dst_ip:dst_port)
    const uint8_t *pp;
    uint32_t plen;
    uint8_t peer_mac[6];
    uint32_t peer_ip;
    uint16_t peer_port;
    int ok = nano_udp_parse(frame, flen, dst_ip, dst_port, &pp, &plen
                            , peer_mac, &peer_ip, &peer_port);
    CHECK(ok == 1);
    CHECK(plen == sizeof(payload));
    CHECK(memcmp(pp, payload, plen) == 0);
    CHECK(memcmp(peer_mac, src_mac, 6) == 0);
    CHECK(peer_ip == src_ip);
    CHECK(peer_port == src_port);

    // Wrong destination port must be rejected
    CHECK(nano_udp_parse(frame, flen, dst_ip, dst_port + 1, &pp, &plen
                         , peer_mac, &peer_ip, &peer_port) == 0);
    // Corrupting the IP header must fail the checksum test
    uint8_t bad[128];
    memcpy(bad, frame, flen);
    bad[NANO_ETH_HLEN + 16] ^= 0xff; // flip a byte of the source IP
    CHECK(nano_udp_parse(bad, flen, dst_ip, dst_port, &pp, &plen
                         , peer_mac, &peer_ip, &peer_port) == 0);
}

int
main(void)
{
    test_checksum();
    test_arp();
    test_udp_roundtrip();
    if (failures) {
        printf("nano_udp: %d check(s) FAILED\n", failures);
        return 1;
    }
    printf("nano_udp: all tests passed\n");
    return 0;
}
