// Bounded single-producer/single-consumer acquisition descriptor ring.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "compiler.h" // barrier
#include "generic/acq_ring.h"

void
acq_ring_init(struct acq_ring *ring, uint8_t capacity)
{
    ring->capacity = capacity <= ACQ_RING_MAX_ENTRIES ? capacity : 0;
    ring->head = ring->tail = ring->count = 0;
    ring->highwater = 0;
    ring->rejected = 0;
}

int
acq_ring_push(struct acq_ring *ring, uint8_t value)
{
    uint8_t count = ring->count;
    if (!ring->capacity || count >= ring->capacity) {
        ring->rejected++;
        return -1;
    }
    ring->entries[ring->head] = value;
    barrier();
    ring->head = (ring->head + 1) % ring->capacity;
    ring->count = count + 1;
    if (ring->count > ring->highwater)
        ring->highwater = ring->count;
    return 0;
}

int
acq_ring_pop(struct acq_ring *ring, uint8_t *value)
{
    uint8_t count = ring->count;
    if (!count)
        return -1;
    barrier();
    *value = ring->entries[ring->tail];
    ring->tail = (ring->tail + 1) % ring->capacity;
    barrier();
    ring->count = count - 1;
    return 0;
}
