// Host-side unit test for the shared-memory SPSC ring (shmem_ring.h)
//
// Two POSIX threads stand in for the ESP32's two cores and hammer
// one ring with randomly sized records under wrap pressure.  Checked
// invariants:
//  * content integrity: every record pops exactly as pushed (a
//    per-record LCG pattern seeded by the record sequence number),
//    in order, none lost, none duplicated
//  * two-segment pushes (hdr + payload, as the modem rx path uses)
//    reassemble correctly
//  * index sanity: head/tail only grow, used <= ring size at every
//    observation (would catch torn/misordered index updates)
//  * full-ring pushes fail atomically and the stream stays intact
//
// Build and run (no target hardware needed):
//   gcc -O2 -Wall -Wextra -pthread -fsanitize=thread  (dito: address)
//       src/esp32/shmem_ring_test.c -o shmem_ring_test && ./shmem_ring_test
// and once more with plain -O2 (fastest, most wrap pressure).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "shmem_ring.h"

// Definition for the extern in shmem_ring.h (unused by this test,
// which exercises a local ring, but keeps the header self-contained)
struct shmem_console_shared esp32_shmem;

#define NUM_RECORDS 2000000u
#define MAX_REC 700 // > SHMEM_RING_SIZE/16 so wrap+full are frequent

static struct shmem_ring ring;

static void
fail(const char *msg, uint32_t seq)
{
    fprintf(stderr, "FAIL: %s (record %u)\n", msg, seq);
    exit(1);
}

// Deterministic per-record length and content
static uint32_t
rec_len(uint32_t seq)
{
    uint32_t x = seq * 2654435761u;
    return 1 + (x >> 7) % MAX_REC;
}

static void
rec_fill(uint32_t seq, uint8_t *buf, uint32_t len)
{
    uint32_t lcg = seq ^ 0x9e3779b9u;
    for (uint32_t i = 0; i < len; i++) {
        lcg = lcg * 1664525u + 1013904223u;
        buf[i] = lcg >> 24;
    }
}

static void *
producer(void *arg)
{
    (void)arg;
    uint8_t buf[MAX_REC];
    for (uint32_t seq = 0; seq < NUM_RECORDS; seq++) {
        uint32_t len = rec_len(seq);
        rec_fill(seq, buf, len);
        // Alternate one-segment and two-segment (hdr+payload) pushes
        uint32_t hdrlen = (seq & 1) ? (len + 1) / 2 : 0;
        for (;;) {
            uint32_t ret = hdrlen
                ? shmem_ring_push(&ring, buf, hdrlen
                                  , buf + hdrlen, len - hdrlen)
                : shmem_ring_push(&ring, NULL, 0, buf, len);
            if (ret) {
                if (ret != len)
                    fail("push returned wrong size", seq);
                break;
            }
            // Ring full: verify the failure was honest, then retry
            uint32_t used = ring.head - SHMEM_LOAD_ACQ(&ring.tail);
            if (used > SHMEM_RING_SIZE)
                fail("used > ring size on producer side", seq);
            sched_yield();
        }
    }
    return NULL;
}

static void *
consumer(void *arg)
{
    (void)arg;
    uint8_t got[MAX_REC], want[MAX_REC];
    uint32_t last_head = 0, last_tail = 0;
    for (uint32_t seq = 0; seq < NUM_RECORDS; seq++) {
        int32_t len;
        for (;;) {
            // Index sanity from the consumer's viewpoint
            uint32_t head = SHMEM_LOAD_ACQ(&ring.head), tail = ring.tail;
            if ((int32_t)(head - last_head) < 0)
                fail("head went backwards", seq);
            if ((int32_t)(tail - last_tail) < 0)
                fail("tail went backwards", seq);
            if (head - tail > SHMEM_RING_SIZE)
                fail("used > ring size (torn index?)", seq);
            last_head = head;
            last_tail = tail;
            len = shmem_ring_pop(&ring, got, sizeof(got));
            if (len >= 0)
                break;
            sched_yield();
        }
        if ((uint32_t)len != rec_len(seq))
            fail("record length mismatch", seq);
        rec_fill(seq, want, len);
        if (memcmp(got, want, len))
            fail("record content mismatch", seq);
    }
    if (shmem_ring_readable(&ring))
        fail("ring not empty after final record", NUM_RECORDS);
    return NULL;
}

int
main(void)
{
    pthread_t prod, cons;
    pthread_create(&prod, NULL, producer, NULL);
    pthread_create(&cons, NULL, consumer, NULL);
    pthread_join(prod, NULL);
    pthread_join(cons, NULL);
    printf("PASS: %u records (1..%u bytes), %u-byte ring, "
           "content+order+index invariants held\n"
           , NUM_RECORDS, MAX_REC, SHMEM_RING_SIZE);
    return 0;
}
