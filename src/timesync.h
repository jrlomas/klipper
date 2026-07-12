#ifndef __TIMESYNC_H
#define __TIMESYNC_H

#include <stdint.h>

// Machine-time discipline (FD-0001 doc 01). Machine time is the
// primary MCU's free-running counter; secondaries maintain an
// (offset, rate) mapping from machine time to their local clock,
// disciplined by host-relayed sync beacons. When no discipline has
// been configured (primary MCU, or single-MCU setups) all conversions
// are the identity and timesync_class0_ok() always allows ingest.

// Convert a machine-time duration (ticks) to local clock ticks
uint32_t timesync_ticks_to_local(uint32_t machine_ticks);
// Convert a 32-bit machine-time instant to the local clock domain
uint32_t timesync_clock_to_local(uint32_t machine_clock);
// Report whether Class-0 (motion) ingest may trust the mapping:
// zero when the discipline filter has not converged or the last
// beacon is older than the freewheel budget.
int timesync_class0_ok(void);

#endif // timesync.h
