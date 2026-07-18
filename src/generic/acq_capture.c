// Bounded pre/post-event acquisition capture.

#include <string.h>
#include "generic/acq_capture.h"

int
acq_capture_arm(struct acq_capture *c, uint8_t pre, uint8_t post)
{
    if ((uint16_t)pre + 1u + post > ACQ_CAPTURE_MAX_RECORDS)
        return -1;
    memset(c, 0, sizeof(*c));
    c->pre = pre;
    c->capacity = pre + 1 + post;
    c->post_remaining = post;
    c->armed = 1;
    return 0;
}

int
acq_capture_push(struct acq_capture *c, uint32_t sequence, uint32_t epoch,
                 uint32_t status, const void *data, uint8_t size)
{
    if (!c->armed || c->ready || size > ACQ_CAPTURE_MAX_BYTES)
        return -1;
    uint8_t retain = c->triggered ? c->capacity : c->pre + 1;
    if (c->count == retain) {
        if (c->triggered)
            return -1;
        c->head = (c->head + 1) % ACQ_CAPTURE_MAX_RECORDS;
        c->count--;
    }
    uint8_t index = (c->head + c->count) % ACQ_CAPTURE_MAX_RECORDS;
    struct acq_capture_record *record = &c->records[index];
    record->sequence = sequence;
    record->epoch = epoch;
    record->status = status;
    record->size = size;
    memcpy(record->data, data, size);
    c->count++;
    if (c->triggered && c->post_remaining) {
        c->post_remaining--;
        if (!c->post_remaining)
            c->ready = 1;
    }
    return 0;
}

void
acq_capture_trigger(struct acq_capture *c, uint8_t terminal)
{
    if (!c->armed || c->triggered)
        return;
    c->triggered = 1;
    if (terminal || !c->post_remaining)
        c->ready = 1;
}

int
acq_capture_pop(struct acq_capture *c, struct acq_capture_record *record)
{
    if (!c->ready || !c->count)
        return -1;
    *record = c->records[c->head];
    c->head = (c->head + 1) % ACQ_CAPTURE_MAX_RECORDS;
    c->count--;
    if (!c->count)
        c->armed = c->triggered = c->ready = 0;
    return 0;
}
