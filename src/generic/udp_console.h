#ifndef __GENERIC_UDP_CONSOLE_H
#define __GENERIC_UDP_CONSOLE_H
// Datagram (UDP) console transport glue - see udp_console.c

#include <stdarg.h> // va_list
#include <stdint.h> // uint32_t

struct command_encoder;

// Per-port socket operations.  All callbacks run in klipper task (or
// timer) context on the mcu core; none may block.
struct udp_console_ops {
    // Fetch one pending datagram into buf (up to cap bytes); return
    // its length, or 0 if nothing is pending.
    int32_t (*recv)(void *ctx, uint8_t *buf, uint32_t cap);
    // Transmit one datagram to the current peer (best effort).
    void (*send)(void *ctx, const uint8_t *data, uint32_t len);
    // Optional checked variant: return 0 only once the lower transport has
    // accepted the datagram into its bounded transmit queue.
    int (*send_checked)(void *ctx, const uint8_t *data, uint32_t len);
    // Transmit to the source of the most recently received datagram
    // without changing the authenticated peer. Used only for a session
    // ServerHello, before the candidate has proved PSK possession.
    void (*send_candidate)(void *ctx, const uint8_t *data, uint32_t len);
    // Optional (may be NULL): the most recently received datagram
    // passed authentication - safe point to latch its source address
    // as the peer to transmit to.
    void (*rx_accepted)(void *ctx);
};

// Select XOR erasure pair protection before udp_console_init: fec_k=2
// emits parity after each pair and reconstructs either single loss in order.
// 0 leaves the erasure layer off; other values fail closed at encode.
void udp_console_set_fec_k(uint8_t fec_k);
void udp_console_init(const struct udp_console_ops *ops, void *ctx
                      , const uint8_t *psk, uint32_t psk_len);
// Signal that datagram(s) are ready for ops->recv (callable from
// irq handlers or another cpu/task).
void udp_console_note_rx(void);
// Board console_sendf() implementation for datagram transports
void udp_console_sendf(const struct command_encoder *ce, va_list args);
// Board console_receive_buffer() implementation
void *udp_console_get_rx_buf(void);

#endif // udp_console.h
