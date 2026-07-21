#ifndef __GENERIC_UDP_GATEWAY_H
#define __GENERIC_UDP_GATEWAY_H

#include <stdarg.h>
#include <stdint.h>
#include "udp_console.h"

struct command_encoder;

void udp_gateway_init(const struct udp_console_ops *ops, void *ctx,
                      const uint8_t *psk, uint32_t psk_len);
void udp_gateway_note_rx(void);
void udp_gateway_sendf(const struct command_encoder *ce, va_list args);
void *udp_gateway_get_rx_buf(void);
int udp_gateway_serial_rx(uint16_t channel, const uint8_t *data,
                          uint16_t length, uint32_t hw_clock);

// Board UART/RS-485 ports override these weak hooks. Configuration is a
// transaction: validate first, then apply, and return <0 without mutation.
int gateway_serial_write(uint16_t channel, const uint8_t *data,
                         uint16_t length);
int gateway_serial_configure(uint16_t channel, const uint8_t *data,
                             uint16_t length);
int gateway_serial_break(uint16_t channel, uint32_t duration_us);

#endif
