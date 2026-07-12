#ifndef __GENERIC_W5500_H
#define __GENERIC_W5500_H
// WIZnet W5500 hardwired-TCP/IP SPI Ethernet console transport
// (RFC 0001 doc 07).  See w5500.c.

#include <stdint.h> // uint32_t

// Bring up the W5500 over the given SPI bus / chip-select pin, assign
// the static IPv4 configuration, and open socket 0 in UDP mode on
// 'port'.  Returns 0 on success, <0 if the chip did not answer with
// the expected version register (VERSIONR != 0x04).  IPs are host-
// order (e.g. 192.168.0.254 == 0xC0A800FE).
int w5500_open(uint32_t spi_bus, uint32_t cs_pin, uint8_t spi_mode
               , uint32_t spi_rate, uint32_t ip, uint32_t netmask
               , uint32_t gateway, uint16_t port);

// The datagram-console socket ops backed by the open W5500 socket.
struct udp_console_ops;
extern const struct udp_console_ops w5500_udp_ops;

#endif // w5500.h
