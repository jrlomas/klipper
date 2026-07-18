#ifndef __GENERIC_ACQ_RING_H
#define __GENERIC_ACQ_RING_H

#include <stdint.h>

#define ACQ_RING_MAX_ENTRIES 8

struct acq_ring {
    uint8_t entries[ACQ_RING_MAX_ENTRIES];
    uint8_t capacity;
    volatile uint8_t head;
    volatile uint8_t tail;
    volatile uint8_t count;
    uint8_t highwater;
    uint32_t rejected;
};

void acq_ring_init(struct acq_ring *ring, uint8_t capacity);
int acq_ring_push(struct acq_ring *ring, uint8_t value);
int acq_ring_pop(struct acq_ring *ring, uint8_t *value);

#endif // generic/acq_ring.h
