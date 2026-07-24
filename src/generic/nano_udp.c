// Minimal single-socket UDP/IP/ARP responder
//
// This is the pluggable IP layer for the native RMII Ethernet path
// (FD-0001 doc 07): the on-chip MAC (src/stm32/eth_mac.c) hands raw
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
#include "nano_dhcp.h"

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

static int
nano_udp_parse_inner(const uint8_t *frame, uint32_t len, uint32_t our_ip,
                     uint16_t our_port, uint8_t any_ip,
                     const uint8_t **payload, uint32_t *payload_len,
                     uint8_t peer_mac[6], uint32_t *peer_ip,
                     uint16_t *peer_port)
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
    if (!any_ip && rd_be32(ip + 16) != our_ip)
        return 0;
    if (nano_ip_checksum(ip, ihl, 0) != 0)
        return 0; // includes the stored checksum -> must fold to zero
    uint16_t ip_total = rd_be16(ip + 2);
    if (ip_total < ihl + NANO_UDP_HLEN
        || (uint32_t)NANO_ETH_HLEN + ip_total > len)
        return 0;
    // This tiny stack has no fragment reassembly. Reject MF or non-zero
    // fragment offsets instead of interpreting a fragment as a UDP packet.
    if (rd_be16(ip + 6) & 0x3fff)
        return 0;

    const uint8_t *udp = ip + ihl;
    if (rd_be16(udp + 2) != our_port)
        return 0;
    uint16_t udp_len = rd_be16(udp + 4);
    if (udp_len < NANO_UDP_HLEN
        || udp_len != ip_total - ihl)
        return 0;
    uint16_t udp_csum = rd_be16(udp + 6);
    if (udp_csum) {
        uint32_t src_ip = rd_be32(ip + 12);
        uint32_t dst_ip = rd_be32(ip + 16);
        uint32_t psum = ((src_ip >> 16) & 0xffff) + (src_ip & 0xffff)
                        + ((dst_ip >> 16) & 0xffff) + (dst_ip & 0xffff)
                        + IP_PROTO_UDP + udp_len;
        if (nano_ip_checksum(udp, udp_len, psum) != 0)
            return 0;
    }

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

int
nano_udp_parse(const uint8_t *frame, uint32_t len, uint32_t our_ip
               , uint16_t our_port, const uint8_t **payload
               , uint32_t *payload_len, uint8_t peer_mac[6]
               , uint32_t *peer_ip, uint16_t *peer_port)
{
    return nano_udp_parse_inner(frame, len, our_ip, our_port, 0, payload,
                                payload_len, peer_mac, peer_ip, peer_port);
}

/****************************************************************
 * Stateful console glue
 ****************************************************************/
#ifndef NANO_UDP_TEST

#ifdef NANO_UDP_HOST_TEST
#define CONFIG_RMII_NETMASK 0xffffff00u
#define CONFIG_RMII_GATEWAY 0
#define CONFIG_RMII_DHCP 0
#else
#include "autoconf.h" // CONFIG_RMII_*
#endif
#include "generic/udp_console.h" // udp_console_ops, udp_console_note_rx
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX

static uint8_t our_mac[6];
static uint32_t our_ip;
static uint16_t our_port;
static struct helix_network_config network_config;
static struct helix_dhcp_client dhcp_client;
static uint32_t network_now_ms;
static uint8_t network_apply_delay;
static int (*mac_emit)(const uint8_t *frame, uint32_t len);
static void (*rx_notify)(void);

// Latched peer (only after datagram authentication) and the candidate
// associated with the datagram most recently returned by nano_recv().
static uint8_t cand_mac[6], peer_mac[6];
static uint32_t cand_ip, peer_ip;
static uint16_t cand_port, peer_port;
static uint8_t have_peer;

// Ethernet DMA may deliver a short burst before the cooperative console task
// runs.  Keep a bounded ring instead of dropping every datagram after the
// first; v1 ARQ remains the final overflow recovery mechanism.
#define NANO_RX_QUEUE_DEPTH 4
struct nano_rx_slot {
    uint8_t payload[UDPDG_DATAGRAM_MAX];
    uint8_t mac[6];
    uint32_t len;
    uint32_t ip;
    uint16_t port;
};
static struct nano_rx_slot rx_queue[NANO_RX_QUEUE_DEPTH];
static uint8_t rx_head, rx_count, rx_highwater;
static uint32_t rx_udp_frames, rx_slot_drops;

static void
nano_apply_network(const struct helix_network_params *params)
{
    our_ip = params->ip;
    our_port = params->port;
    have_peer = rx_head = rx_count = 0;
}

void
nano_udp_setup(const uint8_t mac[6], uint32_t ip, uint16_t listen_port
               , int (*emit)(const uint8_t *frame, uint32_t len)
               , void (*notify_rx)(void))
{
    memcpy(our_mac, mac, 6);
    our_ip = ip;
    our_port = listen_port;
    mac_emit = emit;
    rx_notify = notify_rx;
    struct helix_network_params initial = {
        .mode = HELIX_NETWORK_STATIC, .ip = ip,
#ifdef CONFIG_RMII_NETMASK
        .netmask = CONFIG_RMII_NETMASK,
        .gateway = CONFIG_RMII_GATEWAY,
#else
        .netmask = 0xffffff00u, .gateway = 0,
#endif
        .port = listen_port,
    };
    helix_network_config_init(&network_config, &initial);
#if CONFIG_RMII_DHCP
    struct helix_network_params dhcp = initial;
    dhcp.mode = HELIX_NETWORK_DHCP;
    helix_dhcp_start(&dhcp_client, 0, 0x48444d50u, &initial);
    network_config.active = dhcp;
    nano_apply_network(&dhcp);
#endif
}

static int
nano_handle_dhcp(const uint8_t *frame, uint32_t len)
{
    if (dhcp_client.state == HELIX_DHCP_DISABLED)
        return 0;
    const uint8_t *payload;
    uint32_t plen, src_ip;
    uint16_t src_port;
    if (!nano_udp_parse_inner(frame, len, 0, NANO_DHCP_CLIENT_PORT, 1,
                              &payload, &plen, NULL, &src_ip, &src_port)
        || src_port != NANO_DHCP_SERVER_PORT)
        return 0;
    struct nano_dhcp_message message;
    if (nano_dhcp_parse(&message, payload, plen, our_mac, dhcp_client.xid)) {
        dhcp_client.malformed++;
        return 1;
    }
    if (message.type == NANO_DHCP_OFFER)
        helix_dhcp_offer(&dhcp_client, message.xid, message.yiaddr,
                         message.lease.server, network_now_ms);
    else if (message.type == NANO_DHCP_ACK) {
        if (!helix_dhcp_ack(&dhcp_client, message.xid, &message.lease,
                            network_now_ms)) {
            struct helix_network_params params = {
                .mode = HELIX_NETWORK_DHCP, .ip = message.lease.ip,
                .netmask = message.lease.netmask,
                .gateway = message.lease.gateway,
                .port = network_config.active.port,
            };
            network_config.active = params;
            network_config.generation++;
            nano_apply_network(&params);
        }
    } else if (message.type == NANO_DHCP_NAK) {
        helix_dhcp_nak(&dhcp_client, message.xid, network_now_ms);
        our_ip = 0;
        have_peer = 0;
    }
    return 1;
}

void
nano_udp_input(const uint8_t *frame, uint32_t len)
{
    if (len < NANO_ETH_HLEN)
        return;
    uint16_t ethertype = rd_be16(frame + 12);
    if (ethertype == ETH_TYPE_ARP) {
        static const uint8_t broadcast[6] = {
            0xff, 0xff, 0xff, 0xff, 0xff, 0xff
        };
        if (memcmp(frame, our_mac, 6) && memcmp(frame, broadcast, 6))
            return;
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
    static const uint8_t broadcast[6] = {
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff
    };
    if (memcmp(frame, our_mac, 6) && memcmp(frame, broadcast, 6))
        return;
    if (nano_handle_dhcp(frame, len))
        return;
    if (memcmp(frame, our_mac, 6) || !our_ip)
        return;
    const uint8_t *payload;
    uint32_t plen;
    uint8_t src_mac[6];
    uint32_t src_ip;
    uint16_t src_port;
    if (!nano_udp_parse(frame, len, our_ip, our_port, &payload, &plen,
                        src_mac, &src_ip, &src_port))
        return;
    if (rx_count >= NANO_RX_QUEUE_DEPTH
        || plen > sizeof(rx_queue[0].payload)) {
        if (rx_count >= NANO_RX_QUEUE_DEPTH)
            rx_slot_drops++;
        return; // console queue is full - v1 ARQ recovers
    }
    uint8_t tail = (rx_head + rx_count) % NANO_RX_QUEUE_DEPTH;
    struct nano_rx_slot *slot = &rx_queue[tail];
    memcpy(slot->mac, src_mac, sizeof(slot->mac));
    slot->ip = src_ip;
    slot->port = src_port;
    memcpy(slot->payload, payload, plen);
    slot->len = plen;
    rx_count++;
    if (rx_count > rx_highwater)
        rx_highwater = rx_count;
    rx_udp_frames++;
    if (rx_notify)
        rx_notify();
}

void
nano_udp_get_io_stats(uint32_t *udp_rx, uint32_t *slot_drops)
{
    if (udp_rx)
        *udp_rx = rx_udp_frames;
    if (slot_drops)
        *slot_drops = rx_slot_drops;
}

void
nano_udp_get_queue_stats(uint8_t *depth, uint8_t *highwater)
{
    if (depth)
        *depth = rx_count;
    if (highwater)
        *highwater = rx_highwater;
}

static int32_t
nano_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    (void)ctx;
    if (!rx_count)
        return 0;
    struct nano_rx_slot *slot = &rx_queue[rx_head];
    uint32_t n = slot->len;
    if (n > cap)
        n = cap;
    memcpy(buf, slot->payload, n);
    // Keep the return path for this exact datagram stable until the console
    // authenticates it and calls rx_accepted() (or sends a handshake reply).
    memcpy(cand_mac, slot->mac, sizeof(cand_mac));
    cand_ip = slot->ip;
    cand_port = slot->port;
    rx_head = (rx_head + 1) % NANO_RX_QUEUE_DEPTH;
    rx_count--;
    return n;
}

static void
nano_rx_accepted(void *ctx)
{
    (void)ctx;
    memcpy(peer_mac, cand_mac, 6);
    peer_ip = cand_ip;
    peer_port = cand_port;
    have_peer = 1;
}

static void
nano_send_to(const uint8_t *mac, uint32_t ip, uint16_t port,
             const uint8_t *data, uint32_t len)
{
    if (!mac_emit)
        return;
    uint8_t frame[NANO_UDP_OVERHEAD + UDPDG_DATAGRAM_MAX];
    uint32_t flen = nano_udp_build_frame(frame, sizeof(frame), our_mac
                                         , mac, our_ip, ip
                                         , our_port, port, data, len);
    if (flen)
        mac_emit(frame, flen);
}

static void
nano_send(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    if (have_peer)
        nano_send_to(peer_mac, peer_ip, peer_port, data, len);
}

static int
nano_send_checked(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    if (!have_peer || !mac_emit)
        return UDP_CONSOLE_SEND_NO_PEER;
    uint8_t frame[NANO_UDP_OVERHEAD + UDPDG_DATAGRAM_MAX];
    uint32_t flen = nano_udp_build_frame(frame, sizeof(frame), our_mac,
                                         peer_mac, our_ip, peer_ip,
                                         our_port, peer_port, data, len);
    return flen ? mac_emit(frame, flen) : UDP_CONSOLE_SEND_REJECTED;
}

static void
nano_send_candidate(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    nano_send_to(cand_mac, cand_ip, cand_port, data, len);
}

const struct udp_console_ops nano_udp_ops = {
    .recv = nano_recv,
    .send = nano_send,
    .send_checked = nano_send_checked,
    .send_candidate = nano_send_candidate,
    .rx_accepted = nano_rx_accepted,
};

int
nano_udp_network_prepare(uint32_t epoch,
                         const struct helix_network_params *params)
{
    return helix_network_prepare(&network_config, epoch, params);
}

int
nano_udp_network_commit(uint32_t epoch)
{
    int ret = helix_network_commit(&network_config, epoch);
    if (!ret)
        // Preserve the old authenticated path long enough to flush the commit
        // response before changing source address and clearing the peer.
        network_apply_delay = 2;
    return ret;
}

void
nano_udp_network_abort(uint32_t epoch)
{
    helix_network_abort(&network_config, epoch);
}

void
nano_udp_network_get_status(struct helix_network_params *params,
                            uint32_t *epoch, uint32_t *generation,
                            uint8_t *dhcp_state, uint32_t *rejected,
                            uint32_t *dhcp_malformed, uint32_t *dhcp_naks,
                            uint32_t *dhcp_retries)
{
    if (params)
        *params = network_config.active;
    if (epoch)
        *epoch = network_config.active_epoch;
    if (generation)
        *generation = network_config.generation;
    if (dhcp_state)
        *dhcp_state = dhcp_client.state;
    if (rejected)
        *rejected = network_config.rejected;
    if (dhcp_malformed)
        *dhcp_malformed = dhcp_client.malformed;
    if (dhcp_naks)
        *dhcp_naks = dhcp_client.naks;
    if (dhcp_retries)
        *dhcp_retries = dhcp_client.retries;
}

static void
nano_send_dhcp(uint8_t action)
{
    uint8_t type = action == HELIX_DHCP_ACTION_DISCOVER
                   ? NANO_DHCP_DISCOVER : NANO_DHCP_REQUEST;
    uint32_t requested = dhcp_client.offered_ip;
    if (action == HELIX_DHCP_ACTION_RENEW
        || action == HELIX_DHCP_ACTION_REBIND)
        requested = dhcp_client.lease.ip;
    uint32_t server = action == HELIX_DHCP_ACTION_REBIND
                      ? 0 : (dhcp_client.offered_server
                             ? dhcp_client.offered_server
                             : dhcp_client.lease.server);
    uint8_t payload[320];
    uint32_t plen = nano_dhcp_build(payload, sizeof(payload), our_mac, type,
                                     dhcp_client.xid, requested, server);
    if (!plen || !mac_emit)
        return;
    static const uint8_t broadcast[6] = {
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff
    };
    uint8_t frame[NANO_UDP_OVERHEAD + sizeof(payload)];
    uint32_t flen = nano_udp_build_frame(
        frame, sizeof(frame), our_mac, broadcast,
        action == HELIX_DHCP_ACTION_RENEW ? our_ip : 0,
        0xffffffffu, NANO_DHCP_CLIENT_PORT, NANO_DHCP_SERVER_PORT,
        payload, plen);
    if (flen)
        mac_emit(frame, flen);
}

void
nano_udp_poll(uint32_t now_ms)
{
    network_now_ms = now_ms;
    if (network_apply_delay && !--network_apply_delay) {
        struct helix_network_params params;
        if (helix_network_take_apply(&network_config, &params, NULL)) {
            if (params.mode == HELIX_NETWORK_DHCP) {
                struct helix_network_params fallback = params;
                fallback.mode = HELIX_NETWORK_STATIC;
                if (!helix_network_params_valid(&fallback))
                    fallback = dhcp_client.fallback;
                helix_dhcp_start(&dhcp_client, now_ms,
                                 0x48444d50u ^ network_config.active_epoch,
                                 &fallback);
                params.ip = 0;
            } else {
                memset(&dhcp_client, 0, sizeof(dhcp_client));
            }
            nano_apply_network(&params);
        }
    }
    uint8_t action = helix_dhcp_poll(&dhcp_client, now_ms);
    if (action == HELIX_DHCP_ACTION_DISCOVER
        || action == HELIX_DHCP_ACTION_REQUEST
        || action == HELIX_DHCP_ACTION_RENEW
        || action == HELIX_DHCP_ACTION_REBIND)
        nano_send_dhcp(action);
    else if (action == HELIX_DHCP_ACTION_EXPIRE) {
        our_ip = 0;
        have_peer = 0;
    } else if (action == HELIX_DHCP_ACTION_FALLBACK) {
        network_config.active = dhcp_client.fallback;
        network_config.generation++;
        nano_apply_network(&dhcp_client.fallback);
    }
}

#endif // !NANO_UDP_TEST
