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

    // Seeded producer/consumer starvation, wrap, full, and restart model.
    uint32_t random = 0x31415926;
    uint8_t model[8], model_count = 0, model_head = 0;
    uint32_t rejected = 0;
    acq_ring_init(&ring, 8);
    for (uint32_t operation = 0; operation < 100000; operation++) {
        random = random * 1664525u + 1013904223u;
        if (!(random & 0x3ff)) {
            acq_ring_init(&ring, 8);
            model_count = model_head = 0;
            rejected = 0;
            continue;
        }
        if ((random & 3) != 0) {
            uint8_t input = random >> 24;
            int ret = acq_ring_push(&ring, input);
            if (model_count == 8) {
                assert(ret);
                rejected++;
            } else {
                assert(!ret);
                model[(model_head + model_count) % 8] = input;
                model_count++;
            }
        } else {
            int ret = acq_ring_pop(&ring, &value);
            if (!model_count) {
                assert(ret);
            } else {
                assert(!ret && value == model[model_head]);
                model_head = (model_head + 1) % 8;
                model_count--;
            }
        }
        assert(ring.count == model_count && ring.rejected == rejected);
    }
    puts("PASS: bounded acquisition ring wrap and exhaustion");
    return 0;
}
