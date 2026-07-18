#ifndef __GENERIC_ACQ_CAPTURE_H
#define __GENERIC_ACQ_CAPTURE_H

#include <stdint.h>

#define ACQ_CAPTURE_MAX_RECORDS 8
#define ACQ_CAPTURE_MAX_BYTES 32

struct acq_capture_record {
    uint32_t sequence;
    uint32_t epoch;
    uint32_t status;
    uint8_t size;
    uint8_t data[ACQ_CAPTURE_MAX_BYTES];
};

struct acq_capture {
    struct acq_capture_record records[ACQ_CAPTURE_MAX_RECORDS];
    uint8_t head, count, capacity, pre, post_remaining;
    uint8_t armed, triggered, ready;
};

int acq_capture_arm(struct acq_capture *capture, uint8_t pre, uint8_t post);
int acq_capture_push(struct acq_capture *capture, uint32_t sequence,
                     uint32_t epoch, uint32_t status,
                     const void *data, uint8_t size);
void acq_capture_trigger(struct acq_capture *capture, uint8_t terminal);
int acq_capture_pop(struct acq_capture *capture,
                    struct acq_capture_record *record);

#endif // generic/acq_capture.h
