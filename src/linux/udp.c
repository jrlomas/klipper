// UDP socket binding for the datagram console (desktop-testable
// network transport)
//
// This wires the generic datagram console glue (src/generic/
// udp_console.c) to a plain UDP socket so the complete RFC 0001
// doc 07 network stack - klippy -> lib/intentproto/tools/
// udp_bridge.py -> HMAC-authenticated datagrams -> klipper frames -
// can be exercised on a desktop with zero hardware.  An ESP32 board
// uses the identical glue over an lwIP socket.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <ctype.h> // isspace
#include <netinet/in.h> // sockaddr_in
#include <stdio.h> // fprintf
#include <stdlib.h> // atoi
#include <string.h> // memset
#include <sys/socket.h> // socket
#include <unistd.h> // close
#include "generic/udp_console.h" // udp_console_init
#include "internal.h" // console_use_udp

static int udp_fd = -1;
// Source of the most recently received datagram, and the last source
// that passed datagram authentication (only the latter is ever
// transmitted to - an unauthenticated packet must not steal the link)
static struct sockaddr_storage rx_candidate, tx_peer;
static socklen_t rx_candidate_len, tx_peer_len;

static int32_t
udp_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    struct sockaddr_storage src;
    socklen_t sl = sizeof(src);
    int ret = recvfrom(udp_fd, buf, cap, MSG_DONTWAIT
                       , (struct sockaddr *)&src, &sl);
    if (ret <= 0)
        return 0;
    rx_candidate = src;
    rx_candidate_len = sl;
    return ret;
}

static void
udp_rx_accepted(void *ctx)
{
    tx_peer = rx_candidate;
    tx_peer_len = rx_candidate_len;
}

static void
udp_send(void *ctx, const uint8_t *data, uint32_t len)
{
    if (!tx_peer_len)
        return;
    int ret = sendto(udp_fd, data, len, MSG_DONTWAIT
                     , (const struct sockaddr *)&tx_peer, tx_peer_len);
    if (ret < 0)
        report_errno("sendto", ret);
}

static const struct udp_console_ops linux_udp_ops = {
    .recv = udp_recv,
    .send = udp_send,
    .rx_accepted = udp_rx_accepted,
};

// Read the pre-shared key (whitespace-trimmed, matching
// udp_bridge.py's psk file handling)
static int
read_psk(const char *fname, uint8_t *psk, uint32_t cap)
{
    FILE *f = fopen(fname, "rb");
    if (!f) {
        fprintf(stderr, "Unable to open PSK file '%s'\n", fname);
        return -1;
    }
    size_t len = fread(psk, 1, cap, f);
    fclose(f);
    while (len && isspace(psk[len-1]))
        len--;
    size_t skip = 0;
    while (skip < len && isspace(psk[skip]))
        skip++;
    if (skip) {
        memmove(psk, &psk[skip], len - skip);
        len -= skip;
    }
    if (!len) {
        fprintf(stderr, "Empty PSK file '%s'\n", fname);
        return -1;
    }
    return len;
}

static uint8_t psk_buf[64];

int
udp_console_setup(int port, const char *psk_file, int trust_network)
{
    int psk_len = 0;
    if (psk_file) {
        psk_len = read_psk(psk_file, psk_buf, sizeof(psk_buf));
        if (psk_len < 0)
            return -1;
    } else if (!trust_network) {
        fprintf(stderr, "Authentication is mandatory on network"
                " transports: give a PSK file (-k), or confess"
                " -t (trust network) for an isolated segment\n");
        return -1;
    }

    udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd < 0) {
        report_errno("socket", udp_fd);
        return -1;
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    int ret = bind(udp_fd, (struct sockaddr *)&addr, sizeof(addr));
    if (ret < 0) {
        report_errno("bind", ret);
        close(udp_fd);
        return -1;
    }
    ret = set_non_blocking(udp_fd);
    if (ret)
        return -1;
    ret = set_close_on_exec(udp_fd);
    if (ret)
        return -1;
    ret = set_non_blocking(STDERR_FILENO);
    if (ret)
        return -1;

    udp_console_init(&linux_udp_ops, NULL, psk_buf, psk_len);
    console_use_udp(udp_fd);
    return 0;
}
