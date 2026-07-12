#ifndef __ESP32_SHMEM_RING_H
#define __ESP32_SHMEM_RING_H
// Lock-free single-producer/single-consumer byte ring in shared RAM
//
// This is the cross-core pipe of the "IDF as modem" architecture
// (FD-0001 doc 12): core 0 (the IDF/WiFi modem) and core 1 (bare
// metal klipper) exchange sealed console datagrams through two of
// these rings.  It is also used, unchanged, by the desktop unit test
// (shmem_ring_test.c) with two POSIX threads standing in for the two
// cores - the ring code itself has no target dependencies beyond the
// memory barrier below.
//
// Concurrency contract:
//  * exactly one producer core/thread calls shmem_ring_push()
//  * exactly one consumer core/thread calls shmem_ring_pop()
//  * head is written only by the producer, tail only by the
//    consumer; both are free-running uint32 indices (the ring size
//    is a power of two, so wraparound arithmetic is exact)
//  * the indices are moved with __atomic acquire/release accesses:
//    the producer release-stores head after writing the record
//    bytes, the consumer acquire-loads head before reading them
//    (and symmetrically tail guards space reuse).  On the Xtensa
//    LX6 - where the two cores share uncached internal SRAM and a
//    32-bit aligned store is single-copy atomic - gcc lowers these
//    to plain l32i/s32i fenced with "memw", the same barrier IDF
//    itself uses between cores; on the test hosts they are real
//    C11-style atomics, which lets ThreadSanitizer verify the
//    protocol (see shmem_ring_test.c).
//
// Records are length-prefixed ([u16 len][len bytes]) and copied
// bytewise (records may straddle the wrap point; no alignment is
// assumed).  A push that does not fit fails atomically (returns 0,
// ring untouched) - the caller drops the datagram and the console's
// frame-layer ARQ recovers, exactly as on rx-overflow of a wired
// port.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdint.h> // uint32_t

#define SHMEM_LOAD_ACQ(p) __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define SHMEM_STORE_REL(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)

// Ring capacity in bytes; must be a power of two.  8KiB holds ~5
// maximum-size sealed datagrams (1472B + record overhead) per
// direction - deeper than the 6-slot ring the pre-modem udp_port.c
// used, for 16KiB of the 320KiB DRAM.
#define SHMEM_RING_SIZE 8192
#define SHMEM_RING_MASK (SHMEM_RING_SIZE - 1)

struct shmem_ring {
    uint32_t head; // producer write index (free running)
    uint32_t tail; // consumer read index (free running)
    uint8_t buf[SHMEM_RING_SIZE];
};

// Bytewise copy into the ring starting at index 'pos' (mod size)
static inline void
shmem_ring_write_at(struct shmem_ring *r, uint32_t pos, const uint8_t *src
                    , uint32_t len)
{
    while (len--) {
        r->buf[pos++ & SHMEM_RING_MASK] = *src++;
    }
}

static inline void
shmem_ring_read_at(struct shmem_ring *r, uint32_t pos, uint8_t *dst
                   , uint32_t len)
{
    while (len--) {
        *dst++ = r->buf[pos++ & SHMEM_RING_MASK];
    }
}

// Push one record built from two segments (hdr may be NULL/0).
// Returns record payload size on success, 0 if the ring is full.
static inline uint32_t
shmem_ring_push(struct shmem_ring *r, const void *hdr, uint32_t hdrlen
                , const void *payload, uint32_t len)
{
    uint32_t total = hdrlen + len;
    if (!total || total > 0xffff)
        return 0;
    uint32_t head = r->head; // producer-owned, plain access
    // Acquire pairs with the consumer's release of tail: space is
    // only reused after the consumer finished copying out of it.  A
    // stale tail merely underestimates free space - safe.
    uint32_t used = head - SHMEM_LOAD_ACQ(&r->tail);
    if (2 + total > SHMEM_RING_SIZE - used)
        return 0;
    uint8_t lenb[2] = { total & 0xff, total >> 8 };
    shmem_ring_write_at(r, head, lenb, 2);
    if (hdrlen)
        shmem_ring_write_at(r, head + 2, hdr, hdrlen);
    if (len)
        shmem_ring_write_at(r, head + 2 + hdrlen, payload, len);
    // Publish: record bytes must be globally visible before head
    SHMEM_STORE_REL(&r->head, head + 2 + total);
    return total;
}

// True if a record is pending
static inline int
shmem_ring_readable(struct shmem_ring *r)
{
    return SHMEM_LOAD_ACQ(&r->head) != r->tail;
}

// Pop one record into dst (up to cap bytes; longer records are
// truncated to cap but fully consumed).  Returns the copied length,
// or -1 if the ring is empty.
static inline int32_t
shmem_ring_pop(struct shmem_ring *r, void *dst, uint32_t cap)
{
    uint32_t tail = r->tail; // consumer-owned, plain access
    // Acquire pairs with the producer's release of head: the record
    // bytes are read no earlier than the index that published them
    if (SHMEM_LOAD_ACQ(&r->head) == tail)
        return -1;
    uint8_t lenb[2];
    shmem_ring_read_at(r, tail, lenb, 2);
    uint32_t total = lenb[0] | (uint32_t)lenb[1] << 8;
    uint32_t copy = total > cap ? cap : total;
    shmem_ring_read_at(r, tail + 2, dst, copy);
    // Release: the copy must complete before the space is reusable
    SHMEM_STORE_REL(&r->tail, tail + 2 + total);
    return copy;
}


/****************************************************************
 * Shared console area (modem <-> bare klipper core)
 ****************************************************************/

// Opaque peer-address blob: written by the modem (core 0) into each
// rx record header, echoed back by core 1 on authentication.  Core 1
// never interprets it (it is a struct sockaddr_in over WiFi/lwIP -
// exactly 16 bytes - but any transport encoding fits).
#define SHMEM_ADDR_MAX 16

struct shmem_console_shared {
    // Modem -> klipper: sealed datagrams, each record prefixed with
    // the SHMEM_ADDR_MAX-byte source-address blob
    struct shmem_ring rx;
    // Klipper -> modem: sealed datagrams (no address; the modem
    // sends to the published peer below)
    struct shmem_ring tx;

    // Authenticated-peer publication.  Writer: core 1 (on HMAC
    // acceptance, ops->rx_accepted).  Reader: core 0 (before every
    // transmit).  Classic seqlock: writer makes seq odd, updates the
    // words, makes seq even; the reader retries while seq is odd or
    // moved.  The address is stored as whole words so both sides can
    // use relaxed atomic word accesses between the seqlock fences
    // (C11 seqlock idiom - plain accesses would be a data race).
    uint32_t peer_seq;
    uint32_t peer_valid;
    uint32_t peer_addr[SHMEM_ADDR_MAX / 4];

    // Boot handshake: core 0 fills psk/trust before unstalling core
    // 1; core 1 sets core1_alive once sched_main is entered.
    uint8_t psk[64];
    uint32_t psk_len;
    uint32_t trust_network;
    volatile uint32_t core1_alive;
};

extern struct shmem_console_shared esp32_shmem;

#endif // shmem_ring.h
