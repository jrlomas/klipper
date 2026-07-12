// Shared-memory ring backing for the datagram console - the core-1
// (bare klipper) side of the "IDF as modem" split (FD-0001 doc 12)
//
// This file is a drop-in replacement for the socket ops that
// udp_port.c provides in the component architecture: the generic
// console glue (src/generic/udp_console.c) and the intentproto
// datagram/HMAC layer (src/generic/udp_datagram.cpp) are reused
// unchanged; only the three struct udp_console_ops callbacks now
// move sealed datagrams through the lock-free SPSC rings of
// shmem_ring.h instead of an lwIP socket.
//
// Security property worth stating: HMAC verification happens HERE,
// on the klipper core.  The modem core (with its closed radio blobs)
// shuttles opaque sealed bytes; it cannot forge a datagram that core
// 1 will accept, and the peer address it replies to is only ever one
// that arrived attached to a datagram core 1 authenticated.  The
// address blob itself stays opaque to this file (it is a sockaddr in
// modem.c's encoding) - it is copied, never parsed.
//
// Peer publication is a classic seqlock (writer here on core 1,
// reader in modem.c on core 0): seq goes odd, addr is updated, seq
// goes even; the reader retries while seq is odd or moved.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "generic/udp_console.h" // udp_console_init
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX
#include "internal.h" // shmem_console_init
#include "shmem_ring.h" // esp32_shmem

// The one shared console area; .bss lives in internal DRAM, which
// both cores address uncached
struct shmem_console_shared esp32_shmem;

// Source address of the last datagram handed to the console glue;
// published as the transmit peer once that datagram authenticates
static uint8_t rx_candidate[SHMEM_ADDR_MAX];

// ops->recv: pop one sealed datagram (rx record = addr blob + bytes)
static int32_t
shmem_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    uint8_t rec[SHMEM_ADDR_MAX + UDPDG_DATAGRAM_MAX];
    int32_t len = shmem_ring_pop(&esp32_shmem.rx, rec, sizeof(rec));
    if (len <= SHMEM_ADDR_MAX)
        return 0; // empty (or malformed runt - drop)
    memcpy(rx_candidate, rec, SHMEM_ADDR_MAX);
    len -= SHMEM_ADDR_MAX;
    if ((uint32_t)len > cap)
        len = cap;
    memcpy(buf, rec + SHMEM_ADDR_MAX, len);
    return len;
}

// ops->rx_accepted: the datagram from shmem_recv passed HMAC - latch
// its source as the peer the modem may transmit to.  Seqlock write
// side (C11 idiom): seq odd -> release fence -> relaxed word stores
// -> release fence -> seq even; modem.c holds the matching reader.
static void
shmem_rx_accepted(void *ctx)
{
    uint32_t seq = esp32_shmem.peer_seq; // single writer - plain read
    uint32_t words[SHMEM_ADDR_MAX / 4];
    memcpy(words, rx_candidate, SHMEM_ADDR_MAX);
    __atomic_store_n(&esp32_shmem.peer_seq, seq + 1, __ATOMIC_RELAXED);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    for (uint32_t i = 0; i < SHMEM_ADDR_MAX / 4; i++)
        __atomic_store_n(&esp32_shmem.peer_addr[i], words[i]
                         , __ATOMIC_RELAXED);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    __atomic_store_n(&esp32_shmem.peer_seq, seq + 2, __ATOMIC_RELAXED);
    __atomic_store_n(&esp32_shmem.peer_valid, 1, __ATOMIC_RELEASE);
}

// ops->send: queue one sealed datagram for the modem to transmit
static void
shmem_send(void *ctx, const uint8_t *data, uint32_t len)
{
    // Full ring: drop, exactly as a wired port drops on tx overflow;
    // the host's retransmit machinery recovers
    shmem_ring_push(&esp32_shmem.tx, NULL, 0, data, len);
}

static const struct udp_console_ops shmem_console_ops = {
    .recv = shmem_recv,
    .send = shmem_send,
    .rx_accepted = shmem_rx_accepted,
};

// Surface pending rx records to the console task; called from core
// 1's irq_poll() (there is no cross-core interrupt in the polled
// design - the ring itself is the doorbell)
void DECL_IRAM
shmem_console_poll(void)
{
    if (shmem_ring_readable(&esp32_shmem.rx))
        udp_console_note_rx();
}

// Core-1 init, before sched_main: bind the console glue to the
// rings.  The PSK was staged into the shared area by core 0 (main.c
// load_psk) before core 1 was unstalled.
void
shmem_console_init(void)
{
    udp_console_init(&shmem_console_ops, NULL
                     , esp32_shmem.psk, esp32_shmem.psk_len);
}
