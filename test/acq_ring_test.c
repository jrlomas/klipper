#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/acq_ring.h"

int
main(void)
{
    struct acq_ring ring;
    acq_ring_init(&ring, 3);
    assert(!acq_ring_push(&ring, 7));
    assert(!acq_ring_push(&ring, 8));
    assert(!acq_ring_push(&ring, 9));
    assert(acq_ring_push(&ring, 10));
    assert(ring.rejected == 1 && ring.highwater == 3);
    uint8_t value;
    assert(!acq_ring_pop(&ring, &value) && value == 7);
    assert(!acq_ring_pop(&ring, &value) && value == 8);
    assert(!acq_ring_push(&ring, 10));
    assert(!acq_ring_push(&ring, 11));
    assert(acq_ring_push(&ring, 12));
    assert(!acq_ring_pop(&ring, &value) && value == 9);
    assert(!acq_ring_pop(&ring, &value) && value == 10);
    assert(!acq_ring_pop(&ring, &value) && value == 11);
    assert(acq_ring_pop(&ring, &value));
    puts("PASS: bounded acquisition ring wrap and exhaustion");
    return 0;
}
