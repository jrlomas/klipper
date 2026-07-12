// Minimal single-socket UDP/IP/ARP responder
//
// This is the pluggable IP layer for the native RMII Ethernet path
// (RFC 0001 doc 07): the on-chip MAC (src/stm32/eth_mac.c) hands raw
// ethernet frames to nano_udp_input() and transmits frames produced
// here.  It is deliberately just enough for ONE UDP console socket -
// ARP replies so a host can find us, IPv4 header + checksum, UDP demux
// to the console - and no more.  A full lwIP integration plugs in at
// the identical MAC seam and would replace this file; nano_udp exists
// so the RMII path is functional without dragging in a general IP
// stack (see the scope note in docs/Ethernet.md).
//
// The HMAC-authenticated intentproto datagram (udp_datagram.h) rides
// inside the UDP payload unchanged - nano_udp only moves bytes, exactly
// like the W5500's silicon stack or an lwIP socket.
//
// The pure framing helpers (checksum / ARP / build / parse) carry no
// state and are exercised by test/nano_udp/nano_udp_test.c against
// known-good byte vectors.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "nano_udp.h" // nano_udp_input

#define ETH_TYPE_ARP  0x0806
#define ETH_TYPE_IPV4 0x0800
#define IP_PROTO_UDP  17
#define ARP_OP_REQUEST 1
#define ARP_OP_REPLY   2

// ---- byte helpers (network big-endian) ----
static inline uint16_t rd_be16(const uint8_t *p) { return (p[0] << 8) | p[1]; }
static inline uint32_t
rd_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
           | ((uint32_t)p[2] << 8) | p[3];
}
static inline void
wr_be16(uint8_t *p, uint16_t v)
{
    p[0] = v >> 8;
    p[1] = v & 0xff;
}
static inline void
wr_be32(uint8_t *p, uint32_t v)
{
    p[0] = v >> 24;
    p[1] = v >> 16;
    p[2] = v >> 8;
    p[3] = v & 0xff;
}

/****************************************************************
 * Pure framing helpers
 ****************************************************************/

uint16_t
nano_ip_checksum(const uint8_t *data, uint32_t len, uint32_t init)
{
    uint32_t sum = init;
    while (len > 1) {
        sum += rd_be16(data);
        data += 2;
        len -= 2;
    }
    if (len)
        sum += (uint32_t)data[0] << 8; // odd byte, high-order padded
    while (sum >> 16)
        sum = (sum & 0xffff) + (sum >> 16);
    return (uint16_t)~sum;
}

uint32_t
nano_arp_build_reply(const uint8_t *req, uint32_t req_len
                     , const uint8_t our_mac[6], uint32_t our_ip
                     , uint8_t *out)
{
    if (req_len < NANO_ARP_LEN)
        return 0;
    if (rd_be16(req) != 1 || rd_be16(req + 2) != ETH_TYPE_IPV4)
        return 0; // htype ethernet, ptype IPv4
    if (req[4] != 6 || req[5] != 4)
        return 0; // hlen/plen
    if (rd_be16(req + 6) != ARP_OP_REQUEST)
        return 0;
    if (rd_be32(req + 24) != our_ip)
        return 0; // target protocol address is not ours

    const uint8_t *sender_mac = req + 8;
    const uint8_t *sender_ip = req + 14;
    wr_be16(out, 1);              // htype
    wr_be16(out + 2, ETH_TYPE_IPV4); // ptype
    out[4] = 6;                   // hlen
    out[5] = 4;                   // plen
    wr_be16(out + 6, ARP_OP_REPLY);
    memcpy(out + 8, our_mac, 6);  // sender hw = us
    wr_be32(out + 14, our_ip);    // sender proto = us
    memcpy(out + 18, sender_mac, 6); // target hw = requester
    memcpy(out + 24, sender_ip, 4);  // target proto = requester
    return NANO_ARP_LEN;
}

uint32_t
nano_udp_build_frame(uint8_t *out, uint32_t out_cap
                     , const uint8_t src_mac[6], const uint8_t dst_mac[6]
                     , uint32_t src_ip, uint32_t dst_ip
                     , uint16_t src_port, uint16_t dst_port
                     , const uint8_t *payload, uint32_t payload_len)
{
    uint32_t total = NANO_UDP_OVERHEAD + payload_len;
    if (total > out_cap)
        return 0;

    // Ethernet header
    memcpy(out, dst_mac, 6);
    memcpy(out + 6, src_mac, 6);
    wr_be16(out + 12, ETH_TYPE_IPV4);

    // IPv4 header
    uint8_t *ip = out + NANO_ETH_HLEN;
    uint16_t ip_total = NANO_IP_HLEN + NANO_UDP_HLEN + payload_len;
    ip[0] = 0x45;                 // version 4, IHL 5
    ip[1] = 0;                    // DSCP/ECN
    wr_be16(ip + 2, ip_total);
    wr_be16(ip + 4, 0);           // identification
    wr_be16(ip + 6, 0x4000);      // don't fragment
    ip[8] = 64;                   // TTL
    ip[9] = IP_PROTO_UDP;
    wr_be16(ip + 10, 0);          // checksum placeholder
    wr_be32(ip + 12, src_ip);
    wr_be32(ip + 16, dst_ip);
    wr_be16(ip + 10, nano_ip_checksum(ip, NANO_IP_HLEN, 0));

    // UDP header
    uint8_t *udp = ip + NANO_IP_HLEN;
    uint16_t udp_len = NANO_UDP_HLEN + payload_len;
    wr_be16(udp, src_port);
    wr_be16(udp + 2, dst_port);
    wr_be16(udp + 4, udp_len);
    wr_be16(udp + 6, 0);          // checksum placeholder
    if (payload_len)
        memcpy(udp + NANO_UDP_HLEN, payload, payload_len);

    // UDP checksum over the IPv4 pseudo-header + UDP header + data
    uint32_t psum = 0;
    psum += (src_ip >> 16) & 0xffff;
    psum += src_ip & 0xffff;
    psum += (dst_ip >> 16) & 0xffff;
    psum += dst_ip & 0xffff;
    psum += IP_PROTO_UDP;
    psum += udp_len;
    uint16_t csum = nano_ip_checksum(udp, udp_len, psum);
    if (!csum)
        csum = 0xffff; // 0 means "no checksum"; transmit as all-ones
    wr_be16(udp + 6, csum);
    return total;
}

int
nano_udp_parse(const uint8_t *frame, uint32_t len, uint32_t our_ip
               , uint16_t our_port, const uint8_t **payload
               , uint32_t *payload_len, uint8_t peer_mac[6]
               , uint32_t *peer_ip, uint16_t *peer_port)
{
    if (len < NANO_UDP_OVERHEAD)
        return 0;
    if (rd_be16(frame + 12) != ETH_TYPE_IPV4)
        return 0;
    const uint8_t *ip = frame + NANO_ETH_HLEN;
    if ((ip[0] >> 4) != 4)
        return 0;
    uint32_t ihl = (ip[0] & 0x0f) * 4;
    if (ihl < NANO_IP_HLEN || len < NANO_ETH_HLEN + ihl + NANO_UDP_HLEN)
        return 0;
    if (ip[9] != IP_PROTO_UDP)
        return 0;
    if (rd_be32(ip + 16) != our_ip)
        return 0;
    if (nano_ip_checksum(ip, ihl, 0) != 0)
        return 0; // includes the stored checksum -> must fold to zero

    const uint8_t *udp = ip + ihl;
    if (rd_be16(udp + 2) != our_port)
        return 0;
    uint16_t udp_len = rd_be16(udp + 4);
    if (udp_len < NANO_UDP_HLEN
        || (uint32_t)(NANO_ETH_HLEN + ihl + udp_len) > len)
        return 0;

    if (peer_mac)
        memcpy(peer_mac, frame + 6, 6);
    if (peer_ip)
        *peer_ip = rd_be32(ip + 12);
    if (peer_port)
        *peer_port = rd_be16(udp);
    *payload = udp + NANO_UDP_HLEN;
    *payload_len = udp_len - NANO_UDP_HLEN;
    return 1;
}

/****************************************************************
 * Stateful console glue
 ****************************************************************/
#ifndef NANO_UDP_TEST

#include "generic/udp_console.h" // udp_console_ops, udp_console_note_rx
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX

static uint8_t our_mac[6];
static uint32_t our_ip;
static uint16_t our_port;
static void (*mac_emit)(const uint8_t *frame, uint32_t len);

// Latched peer (only after datagram authentication) and the candidate
// from the most recently received frame
static uint8_t cand_mac[6], peer_mac[6];
static uint32_t cand_ip, peer_ip;
static uint16_t cand_port, peer_port;
static uint8_t have_peer;

// Single-slot inbound datagram queue drained by ops->recv
static uint8_t rx_payload[UDPDG_DATAGRAM_MAX];
static uint32_t rx_len;
static uint8_t rx_full;

void
nano_udp_setup(const uint8_t mac[6], uint32_t ip, uint16_t listen_port
               , void (*emit)(const uint8_t *frame, uint32_t len))
{
    memcpy(our_mac, mac, 6);
    our_ip = ip;
    our_port = listen_port;
    mac_emit = emit;
}

void
nano_udp_input(const uint8_t *frame, uint32_t len)
{
    if (len < NANO_ETH_HLEN)
        return;
    uint16_t ethertype = rd_be16(frame + 12);
    if (ethertype == ETH_TYPE_ARP) {
        uint8_t reply[NANO_ETH_HLEN + NANO_ARP_LEN];
        uint32_t alen = nano_arp_build_reply(frame + NANO_ETH_HLEN
                                             , len - NANO_ETH_HLEN
                                             , our_mac, our_ip
                                             , reply + NANO_ETH_HLEN);
        if (alen && mac_emit) {
            memcpy(reply, frame + 6, 6); // dst = requester
            memcpy(reply + 6, our_mac, 6);
            wr_be16(reply + 12, ETH_TYPE_ARP);
            mac_emit(reply, NANO_ETH_HLEN + alen);
        }
        return;
    }
    if (ethertype != ETH_TYPE_IPV4)
        return;
    const uint8_t *payload;
    uint32_t plen;
    if (!nano_udp_parse(frame, len, our_ip, our_port, &payload, &plen
                        , cand_mac, &cand_ip, &cand_port))
        return;
    if (rx_full || plen > sizeof(rx_payload))
        return; // console has not drained the slot yet - ARQ recovers
    memcpy(rx_payload, payload, plen);
    rx_len = plen;
    rx_full = 1;
    udp_console_note_rx();
}

static int32_t
nano_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    if (!rx_full)
        return 0;
    uint32_t n = rx_len;
    if (n > cap)
        n = cap;
    memcpy(buf, rx_payload, n);
    rx_full = 0;
    return n;
}

static void
nano_rx_accepted(void *ctx)
{
    memcpy(peer_mac, cand_mac, 6);
    peer_ip = cand_ip;
    peer_port = cand_port;
    have_peer = 1;
}

static void
nano_send(void *ctx, const uint8_t *data, uint32_t len)
{
    if (!have_peer || !mac_emit)
        return;
    uint8_t frame[NANO_UDP_OVERHEAD + UDPDG_DATAGRAM_MAX];
    uint32_t flen = nano_udp_build_frame(frame, sizeof(frame), our_mac
                                         , peer_mac, our_ip, peer_ip
                                         , our_port, peer_port, data, len);
    if (flen)
        mac_emit(frame, flen);
}

const struct udp_console_ops nano_udp_ops = {
    .recv = nano_recv,
    .send = nano_send,
    .rx_accepted = nano_rx_accepted,
};

#endif // !NANO_UDP_TEST
