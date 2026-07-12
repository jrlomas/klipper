#ifndef __EXECLOG_H
#define __EXECLOG_H

#include <stdint.h>

// Execution log record types (FD-0001 doc 08)
enum {
    EL_SEG_DONE = 1, // segment completed: pos = end pos, aux = 0
    EL_TRIGGER = 2,  // trsync stop: aux = reason
    EL_UNDERRUN = 3, // queue ran dry, ramp taken: pos = ramp end
    EL_HOLD = 4,     // entered hold/idle: aux = reason
    EL_REBASE = 5,   // host re-anchored: pos = new anchor
    EL_HEATER = 6,   // failsafe policy transition: aux = state<<16|target
    EL_FAULT = 7,    // anything else: aux = code
    EL_DISCIPLINE = 8, // clock sync adjustment: pos = offset err, aux = rate
};

// Append a record; safe from irq, timer, and task context. A no-op
// until the host configures the log.
void execlog_append(uint8_t type, uint8_t src_oid, uint32_t clock
                    , int32_t pos, uint32_t aux);

#endif // execlog.h
