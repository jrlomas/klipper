#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "src/generic/acq_capture.h"

int
main(void)
{
    struct acq_capture capture;
    uint8_t data[2];
    assert(!acq_capture_arm(&capture, 2, 2));
    for (uint32_t seq = 0; seq < 5; seq++) {
        data[0] = seq;
        assert(!acq_capture_push(&capture, seq, 7, 0, data, sizeof(data)));
    }
    acq_capture_trigger(&capture, 0);
    data[0] = 5;
    assert(!acq_capture_push(&capture, 5, 7, 0, data, sizeof(data)));
    data[0] = 6;
    assert(!acq_capture_push(&capture, 6, 7, 1, data, sizeof(data)));
    assert(capture.ready);
    struct acq_capture_record record;
    for (uint32_t seq = 2; seq <= 6; seq++) {
        assert(!acq_capture_pop(&capture, &record));
        assert(record.sequence == seq && record.data[0] == seq);
    }
    assert(acq_capture_pop(&capture, &record));

    assert(!acq_capture_arm(&capture, 1, 6));
    assert(!acq_capture_push(&capture, 9, 2, 4, data, sizeof(data)));
    acq_capture_trigger(&capture, 1);
    assert(!acq_capture_pop(&capture, &record));
    assert(record.sequence == 9 && record.status == 4);
    puts("PASS: bounded acquisition pre/post and terminal capture");
    return 0;
}
