#ifndef __GENERIC_NANO_UDP_H
#define __GENERIC_NANO_UDP_H
// Minimal single-socket UDP/IP/ARP responder for the native RMII
// Ethernet datagram console (FD-0001 doc 07).  See nano_udp.c.

#include <stdint.h> // uint32_t
#include "network_config.h"

// Ethernet / protocol geometry
#define NANO_ETH_HLEN 14
#define NANO_IP_HLEN 20
#define NANO_UDP_HLEN 8
#define NANO_ARP_LEN 28
#define NANO_ETH_ALEN 6
// eth(14) + ip(20) + udp(8) of overhead ahead of the datagram payload
#define NANO_UDP_OVERHEAD (NANO_ETH_HLEN + NANO_IP_HLEN + NANO_UDP_HLEN)

// ---- Pure framing helpers (no state; host unit-tested) ----

// Standard 16-bit one's-complement Internet checksum over 'len' bytes,
// folded with the running 32-bit accumulator 'init' (0 to start).
uint16_t nano_ip_checksum(const uint8_t *data, uint32_t len, uint32_t init);

// Build an ARP reply for 'req' (a 28-byte ARP payload that must be a
// request whose target protocol address is our_ip) into the 28-byte
// 'out'.  Returns NANO_ARP_LEN, or 0 if 'req' is not a request for us.
uint32_t nano_arp_build_reply(const uint8_t *req, uint32_t req_len
                              , const uint8_t our_mac[6], uint32_t our_ip
                              , uint8_t *out);

// Build a complete ethernet+IPv4+UDP frame carrying 'payload' into
// 'out'.  Returns the total frame length, or 0 if it would not fit.
// IPs are host order; ports host order.
uint32_t nano_udp_build_frame(uint8_t *out, uint32_t out_cap
                              , const uint8_t src_mac[6]
                              , const uint8_t dst_mac[6]
                              , uint32_t src_ip, uint32_t dst_ip
                              , uint16_t src_port, uint16_t dst_port
                              , const uint8_t *payload, uint32_t payload_len);

// Parse an ethernet+IPv4+UDP frame addressed to our_ip:our_port.  On
// success sets *payload/*payload_len to the UDP data and the peer
// mac/ip/port, and returns 1.  Returns 0 if the frame is not a valid
// UDP datagram for us (wrong ethertype/proto/port, bad IP checksum,
// truncated).
int nano_udp_parse(const uint8_t *frame, uint32_t len
                   , uint32_t our_ip, uint16_t our_port
                   , const uint8_t **payload, uint32_t *payload_len
                   , uint8_t peer_mac[6], uint32_t *peer_ip
                   , uint16_t *peer_port);

// ---- Stateful console glue (RMII path) ----

struct udp_console_ops;
// The datagram-console socket ops backed by this responder.
extern const struct udp_console_ops nano_udp_ops;

// Configure the responder (called by the MAC bring-up).  'emit' is the
// MAC transmit hook: nano_udp hands it complete ethernet frames.
void nano_udp_setup(const uint8_t mac[6], uint32_t ip, uint16_t listen_port
                    , int (*emit)(const uint8_t *frame, uint32_t len)
                    , void (*notify_rx)(void));

// Feed one received ethernet frame from the MAC.  ARP requests are
// answered immediately (via emit); UDP datagrams to the listen port are
// queued for the console (ops->recv) and wake it.
void nano_udp_input(const uint8_t *frame, uint32_t len);

// Poll DHCP and deferred network transactions from the MAC task.  now_ms is
// a wrapping monotonic millisecond counter owned by the MAC backend.
void nano_udp_poll(uint32_t now_ms);
int nano_udp_network_prepare(uint32_t epoch,
                             const struct helix_network_params *params);
int nano_udp_network_commit(uint32_t epoch);
void nano_udp_network_abort(uint32_t epoch);
void nano_udp_network_get_status(struct helix_network_params *params,
                                 uint32_t *epoch, uint32_t *generation,
                                 uint8_t *dhcp_state, uint32_t *rejected,
                                 uint32_t *dhcp_malformed,
                                 uint32_t *dhcp_naks,
                                 uint32_t *dhcp_retries);

#endif // nano_udp.h
