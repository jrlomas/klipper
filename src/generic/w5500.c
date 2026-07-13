// WIZnet W5500 SPI Ethernet as a datagram-console transport
//
// FD-0001 doc 07 makes the point that "the same UDP binding runs
// unchanged over Ethernet."  This file is that binding for a wired
// link: it is a *transport* that supplies the generic datagram-console
// glue (src/generic/udp_console.c) with a struct udp_console_ops, no
// different in kind from linux/udp.c (a POSIX socket) or
// esp32/udp_port.c (an lwIP socket).  Nothing in the protocol changes:
// the HMAC-authenticated intentproto datagram (udp_datagram.h) rides
// inside the UDP payload exactly as it does over WiFi.
//
// The W5500 is a hardwired TCP/IP controller: it runs the IP/UDP/ARP
// stack in silicon behind an SPI register file, so this driver needs
// no software IP stack.  It works on ANY board that has an SPI bus (it
// is register-level SPI, mode 0, via the board's spi_setup /
// spi_transfer and a GPIO chip-select), which is why it lives in
// src/generic rather than a single port.
//
// Socket-mode UDP path (W5500 datasheet 5.1): Sn_MR = UDP, Sn_CR OPEN
// to bind the local port, then the RX/TX ring pointers (Sn_RX_RD /
// Sn_TX_WR) with Sn_CR RECV / SEND move whole datagrams.  A received
// UDP datagram is prefixed in the socket RX buffer by an 8-byte
// packet-info header (source IP, source port, length); we parse it to
// latch the peer only after the datagram passes authentication
// (ops.rx_accepted), mirroring the "an unauthenticated packet must not
// steal the link" rule of the other bindings.
//
// COMPILE-CHECKED, NOT HARDWARE-VALIDATED: this driver builds and
// links for the STM32 (and any SPI-capable ARM) target but has not
// been run against a physical W5500.  See docs/Ethernet.md.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_CONSOLE_W5500
#include "board/gpio.h" // spi_setup, gpio_out_setup
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND, shutdown
#include "sched.h" // DECL_TASK
#include "generic/udp_console.h" // udp_console_init
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX
#include "w5500.h" // w5500_open

/****************************************************************
 * W5500 register map (subset used by the UDP socket path)
 ****************************************************************/

// SPI frame control byte: block-select (5 bits) | read/write | OM(00
// = variable data length mode, i.e. length is bounded by chip-select)
#define W5500_BSB_COMMON 0x00
#define W5500_BSB_S0_REG 0x01
#define W5500_BSB_S0_TX  0x02
#define W5500_BSB_S0_RX  0x03
#define W5500_RWB_READ   0x00
#define W5500_RWB_WRITE  0x04

// Common registers
#define W5500_MR       0x0000 // mode
#define W5500_GAR      0x0001 // gateway address (4)
#define W5500_SUBR     0x0005 // subnet mask (4)
#define W5500_SHAR     0x0009 // source hw (MAC) address (6)
#define W5500_SIPR     0x000F // source IP (4)
#define W5500_VERSIONR 0x0039 // chip version - always 0x04 on a W5500

// Socket 0 registers (block W5500_BSB_S0_REG)
#define W5500_Sn_MR      0x0000
#define W5500_Sn_CR      0x0001
#define W5500_Sn_IR      0x0002
#define W5500_Sn_SR      0x0003
#define W5500_Sn_PORT    0x0004 // (2)
#define W5500_Sn_DIPR    0x000C // (4)
#define W5500_Sn_DPORT   0x0010 // (2)
#define W5500_Sn_TX_FSR  0x0020 // free size (2)
#define W5500_Sn_TX_WR   0x0024 // (2)
#define W5500_Sn_RX_RSR  0x0026 // received size (2)
#define W5500_Sn_RX_RD   0x0028 // (2)

#define W5500_MR_RST     0x80
#define W5500_Sn_MR_UDP  0x02
#define W5500_Sn_CR_OPEN 0x01
#define W5500_Sn_CR_CLOSE 0x10
#define W5500_Sn_CR_SEND 0x20
#define W5500_Sn_CR_RECV 0x40
#define W5500_Sn_SR_UDP  0x22

// Per-socket buffer geometry (chip default: 2KiB tx + 2KiB rx each)
#define W5500_BUF_SIZE 2048
#define W5500_BUF_MASK (W5500_BUF_SIZE - 1)
#define W5500_CMD_TIMEOUT_US 2000
#define W5500_REOPEN_US 1000000
#define W5500_HEALTH_US 250000

/****************************************************************
 * SPI plumbing
 ****************************************************************/

static struct spi_config w5500_spi;
static struct gpio_out w5500_cs;
static uint8_t w5500_ready, w5500_configured;
static uint32_t w5500_ip, w5500_netmask, w5500_gateway;
static uint16_t w5500_port;
static uint32_t w5500_reopen_next, w5500_health_next;

// Peer bookkeeping: the source of the most recently received datagram,
// and the last source that passed authentication (only the latter is
// ever transmitted to). A hardware reopen clears the authenticated peer.
static uint32_t rx_candidate_ip, tx_peer_ip;
static uint16_t rx_candidate_port, tx_peer_port;
static uint8_t have_peer;

// One variable-length SPI frame: 3-byte address+control header then
// 'len' data bytes, chip-select held low for the whole frame.  For a
// read, 'data' is used as the dummy MOSI source and receives MISO; the
// board spi_transfer len field is 8-bit, so long buffers are chunked.
static void
w5500_frame(uint16_t addr, uint8_t control, uint8_t *data, uint16_t len
            , uint8_t is_read)
{
    uint8_t hdr[3] = { addr >> 8, addr & 0xff, control };
    gpio_out_write(w5500_cs, 0);
    spi_prepare(w5500_spi);
    spi_transfer(w5500_spi, 0, sizeof(hdr), hdr);
    uint16_t off = 0;
    while (off < len) {
        uint16_t chunk = len - off;
        if (chunk > 255)
            chunk = 255;
        spi_transfer(w5500_spi, is_read, chunk, data + off);
        off += chunk;
    }
    gpio_out_write(w5500_cs, 1);
}

static void
w5500_wr(uint16_t addr, uint8_t bsb, const uint8_t *data, uint16_t len)
{
    // spi_transfer with receive_data==0 does not modify the buffer
    w5500_frame(addr, (uint8_t)(bsb << 3) | W5500_RWB_WRITE
                , (uint8_t *)data, len, 0);
}

static void
w5500_rd(uint16_t addr, uint8_t bsb, uint8_t *data, uint16_t len)
{
    memset(data, 0, len); // clock out zeros while sampling MISO
    w5500_frame(addr, (uint8_t)(bsb << 3) | W5500_RWB_READ, data, len, 1);
}

static void
w5500_wr8(uint16_t addr, uint8_t bsb, uint8_t val)
{
    w5500_wr(addr, bsb, &val, 1);
}

static uint8_t
w5500_rd8(uint16_t addr, uint8_t bsb)
{
    uint8_t v;
    w5500_rd(addr, bsb, &v, 1);
    return v;
}

static void
w5500_wr16(uint16_t addr, uint8_t bsb, uint16_t val)
{
    uint8_t b[2] = { val >> 8, val & 0xff };
    w5500_wr(addr, bsb, b, 2);
}

// 16-bit socket counters (RSR/FSR/pointers) can tick mid-read; the
// datasheet's guidance is to sample until two reads agree.
static uint16_t
w5500_rd16(uint16_t addr, uint8_t bsb)
{
    uint16_t prev = 0xffff;
    uint_fast8_t retries = 8;
    while (retries--) {
        uint8_t b[2];
        w5500_rd(addr, bsb, b, 2);
        uint16_t v = ((uint16_t)b[0] << 8) | b[1];
        if (v == prev)
            return v;
        prev = v;
    }
    // A counter that never stabilizes is treated conservatively by its
    // caller; most paths will retry on the next cooperative task pass.
    return prev;
}

static void
w5500_wr32(uint16_t addr, uint8_t bsb, uint32_t val)
{
    uint8_t b[4] = { val >> 24, val >> 16, val >> 8, val };
    w5500_wr(addr, bsb, b, 4);
}

// Copy into/out of the socket ring, honouring the power-of-two wrap
static void
w5500_buf_read(uint16_t ptr, uint8_t *dst, uint16_t len)
{
    uint16_t a = ptr & W5500_BUF_MASK;
    uint16_t first = W5500_BUF_SIZE - a;
    if (first > len)
        first = len;
    w5500_rd(a, W5500_BSB_S0_RX, dst, first);
    if (len > first)
        w5500_rd(0, W5500_BSB_S0_RX, dst + first, len - first);
}

static void
w5500_buf_write(uint16_t ptr, const uint8_t *src, uint16_t len)
{
    uint16_t a = ptr & W5500_BUF_MASK;
    uint16_t first = W5500_BUF_SIZE - a;
    if (first > len)
        first = len;
    w5500_wr(a, W5500_BSB_S0_TX, src, first);
    if (len > first)
        w5500_wr(0, W5500_BSB_S0_TX, src + first, len - first);
}

/****************************************************************
 * Socket engine
 ****************************************************************/

static int
w5500_cmd(uint8_t cmd)
{
    w5500_wr8(W5500_Sn_CR, W5500_BSB_S0_REG, cmd);
    // Sn_CR self-clears when the command engine has accepted it
    uint32_t deadline = timer_read_time()
                        + timer_from_us(W5500_CMD_TIMEOUT_US);
    while (w5500_rd8(W5500_Sn_CR, W5500_BSB_S0_REG)) {
        if (timer_is_before(deadline, timer_read_time())) {
            w5500_ready = 0;
            return -1;
        }
    }
    return 0;
}

static int
w5500_hw_open(void)
{
    w5500_ready = 1;
    have_peer = 0;

    // Software reset and settle
    w5500_wr8(W5500_MR, W5500_BSB_COMMON, W5500_MR_RST);
    uint32_t deadline = timer_read_time()
                        + timer_from_us(W5500_CMD_TIMEOUT_US);
    while (w5500_rd8(W5500_MR, W5500_BSB_COMMON) & W5500_MR_RST) {
        if (timer_is_before(deadline, timer_read_time())) {
            w5500_ready = 0;
            return -1;
        }
    }

    // A locally-administered MAC derived from the static IP keeps the
    // address stable and unique on a segment without a provisioning step
    uint32_t ip = w5500_ip;
    uint8_t mac[6] = { 0x02, 0x00, ip >> 24, ip >> 16, ip >> 8, ip };
    w5500_wr(W5500_SHAR, W5500_BSB_COMMON, mac, sizeof(mac));
    w5500_wr32(W5500_GAR, W5500_BSB_COMMON, w5500_gateway);
    w5500_wr32(W5500_SUBR, W5500_BSB_COMMON, w5500_netmask);
    w5500_wr32(W5500_SIPR, W5500_BSB_COMMON, ip);

    if (w5500_rd8(W5500_VERSIONR, W5500_BSB_COMMON) != 0x04) {
        w5500_ready = 0;
        return -1;
    }

    // Open socket 0 as UDP bound to the listen port
    w5500_wr8(W5500_Sn_MR, W5500_BSB_S0_REG, W5500_Sn_MR_UDP);
    w5500_wr16(W5500_Sn_PORT, W5500_BSB_S0_REG, w5500_port);
    if (w5500_cmd(W5500_Sn_CR_OPEN))
        return -1;
    if (w5500_rd8(W5500_Sn_SR, W5500_BSB_S0_REG) != W5500_Sn_SR_UDP) {
        w5500_ready = 0;
        return -1;
    }
    w5500_health_next = timer_read_time() + timer_from_us(W5500_HEALTH_US);
    return 0;
}

int
w5500_open(uint32_t spi_bus, uint32_t cs_pin, uint8_t spi_mode
           , uint32_t spi_rate, uint32_t ip, uint32_t netmask
           , uint32_t gateway, uint16_t port)
{
    w5500_spi = spi_setup(spi_bus, spi_mode, spi_rate);
    w5500_cs = gpio_out_setup(cs_pin, 1);
    w5500_ip = ip;
    w5500_netmask = netmask;
    w5500_gateway = gateway;
    w5500_port = port;
    w5500_configured = 1;
    return w5500_hw_open();
}

// Drain one received UDP datagram (strip the 8-byte packet-info header)
static int32_t
w5500_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    (void)ctx;
    if (!w5500_ready)
        return 0;
    uint16_t rsr = w5500_rd16(W5500_Sn_RX_RSR, W5500_BSB_S0_REG);
    if (rsr < 8)
        return 0;
    uint16_t rd = w5500_rd16(W5500_Sn_RX_RD, W5500_BSB_S0_REG);

    // Packet-info header: src IP (4), src port (2), UDP data length (2)
    uint8_t ph[8];
    w5500_buf_read(rd, ph, sizeof(ph));
    uint16_t dlen = ((uint16_t)ph[6] << 8) | ph[7];
    uint32_t total = 8u + dlen;
    if (dlen > W5500_BUF_SIZE - 8 || total > rsr) {
        // Corrupt/partial - resynchronise by draining the whole buffer
        w5500_wr16(W5500_Sn_RX_RD, W5500_BSB_S0_REG, rd + rsr);
        w5500_cmd(W5500_Sn_CR_RECV);
        return 0;
    }

    int32_t got = dlen;
    if ((uint32_t)got > cap)
        got = cap; // never overrun the console scratch buffer
    w5500_buf_read(rd + 8, buf, got);

    // Consume the full record even if we truncated the copy
    w5500_wr16(W5500_Sn_RX_RD, W5500_BSB_S0_REG,
               rd + (uint16_t)total);
    if (w5500_cmd(W5500_Sn_CR_RECV))
        return 0;

    rx_candidate_ip = ((uint32_t)ph[0] << 24) | ((uint32_t)ph[1] << 16)
                      | ((uint32_t)ph[2] << 8) | ph[3];
    rx_candidate_port = ((uint16_t)ph[4] << 8) | ph[5];
    if ((uint32_t)dlen > cap)
        return 0; // oversized - drop; the frame layer's ARQ recovers
    return got;
}

static void
w5500_rx_accepted(void *ctx)
{
    (void)ctx;
    tx_peer_ip = rx_candidate_ip;
    tx_peer_port = rx_candidate_port;
    have_peer = 1;
}

static void
w5500_send_to(uint32_t ip, uint16_t port, const uint8_t *data, uint32_t len)
{
    if (!w5500_ready || !len || len > W5500_BUF_SIZE)
        return;
    uint16_t fsr = w5500_rd16(W5500_Sn_TX_FSR, W5500_BSB_S0_REG);
    if (fsr < len)
        return; // no room - best effort, host retransmits on missing ack
    w5500_wr32(W5500_Sn_DIPR, W5500_BSB_S0_REG, ip);
    w5500_wr16(W5500_Sn_DPORT, W5500_BSB_S0_REG, port);
    uint16_t wr = w5500_rd16(W5500_Sn_TX_WR, W5500_BSB_S0_REG);
    w5500_buf_write(wr, data, len);
    w5500_wr16(W5500_Sn_TX_WR, W5500_BSB_S0_REG, wr + len);
    w5500_cmd(W5500_Sn_CR_SEND);
}

static void
w5500_send(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    if (have_peer)
        w5500_send_to(tx_peer_ip, tx_peer_port, data, len);
}

static void
w5500_send_candidate(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    w5500_send_to(rx_candidate_ip, rx_candidate_port, data, len);
}

const struct udp_console_ops w5500_udp_ops = {
    .recv = w5500_recv,
    .send = w5500_send,
    .send_candidate = w5500_send_candidate,
    .rx_accepted = w5500_rx_accepted,
};

// The W5500 has no free interrupt line wired on most boards, so a
// lightweight task polls the received-size register and wakes the
// datagram console when a datagram is waiting.  Polling is rate-limited
// so the SPI read does not dominate the main loop.
static uint32_t w5500_poll_next;

void
w5500_task(void)
{
    uint32_t now = timer_read_time();
    if (!w5500_ready) {
        if (w5500_configured && timer_is_before(w5500_reopen_next, now)) {
            w5500_reopen_next = now + timer_from_us(W5500_REOPEN_US);
            w5500_hw_open();
        }
        return;
    }
    if (timer_is_before(w5500_health_next, now)) {
        w5500_health_next = now + timer_from_us(W5500_HEALTH_US);
        if (w5500_rd8(W5500_VERSIONR, W5500_BSB_COMMON) != 0x04
            || w5500_rd8(W5500_Sn_SR, W5500_BSB_S0_REG)
               != W5500_Sn_SR_UDP) {
            w5500_ready = 0;
            w5500_reopen_next = now;
            return;
        }
    }
    if (!timer_is_before(w5500_poll_next, now))
        return;
    w5500_poll_next = now + timer_from_us(500);
    if (w5500_rd16(W5500_Sn_RX_RSR, W5500_BSB_S0_REG) >= 8)
        udp_console_note_rx();
}
DECL_TASK(w5500_task);

/****************************************************************
 * Pre-shared key (build-time provisioning, mirroring the esp32 port)
 ****************************************************************/

static uint8_t w5500_psk[64];

static uint32_t
w5500_load_psk(void)
{
    uint32_t len = sizeof(CONFIG_W5500_PSK) - 1;
    if (!len)
        return 0;
    if (len > sizeof(w5500_psk))
        len = sizeof(w5500_psk);
    memcpy(w5500_psk, CONFIG_W5500_PSK, len);
    return len;
}

/****************************************************************
 * Runtime configuration command
 ****************************************************************/

// Host-driven bring-up of the link.  Network parameters (SPI bus, CS
// pin, static IP config, listen port) come from the host, matching the
// config_spi idiom (host-encoded pin numbers); the PSK / trust choice
// stays build-time (FD-0001 doc 07 key provisioning).  When the W5500
// is itself the console (CONFIG_CONSOLE_W5500) the link is instead
// brought up at startup from Kconfig, since the console cannot carry
// the command that configures the console.
void
command_config_w5500(uint32_t *args)
{
    int ret = w5500_open(args[0], args[1], 0, CONFIG_W5500_SPI_RATE
                         , args[2], args[3], args[4], args[5]);
    if (ret)
        shutdown("W5500 not found on SPI bus");
    uint32_t plen = w5500_load_psk();
    udp_console_init(&w5500_udp_ops, NULL, w5500_psk, plen);
}
DECL_COMMAND(command_config_w5500, "config_w5500 spi_bus=%u cs_pin=%u"
             " ip=%u netmask=%u gateway=%u port=%hu");

/****************************************************************
 * Console-transport wiring (build-time selected)
 ****************************************************************/

#if CONFIG_CONSOLE_W5500
// When the W5500 is the MCU console, board console_sendf() /
// console_receive_buffer() route through the datagram glue, exactly as
// esp32/main.c does for its lwIP socket.
void
console_sendf(const struct command_encoder *ce, va_list args)
{
    udp_console_sendf(ce, args);
}

void *
console_receive_buffer(void)
{
    return udp_console_get_rx_buf();
}

// Bring the link up from the build-time static configuration before the
// scheduler starts dispatching, so the host's identify handshake has a
// socket to reach.
void
w5500_console_setup(void)
{
    int ret = w5500_open(CONFIG_W5500_SPI_BUS, CONFIG_W5500_CS_PIN, 0
                         , CONFIG_W5500_SPI_RATE, CONFIG_W5500_IP
                         , CONFIG_W5500_NETMASK, CONFIG_W5500_GATEWAY
                         , CONFIG_W5500_UDP_PORT);
    (void)ret; // a missing chip leaves w5500_ready clear; the link is
               // simply down (there is no console to report it on)
    uint32_t plen = w5500_load_psk();
    udp_console_init(&w5500_udp_ops, NULL, w5500_psk, plen);
}
DECL_INIT(w5500_console_setup);
#endif // CONFIG_CONSOLE_W5500
