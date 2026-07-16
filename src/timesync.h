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
// Convert a polynomial derivative from machine-tick units to local-tick
// units. order=1 is velocity, 2 acceleration, through 5 crackle.
int32_t timesync_derivative_to_local(int32_t value, uint8_t order);
// Convert a 32-bit machine-time instant to the local clock domain
uint32_t timesync_clock_to_local(uint32_t machine_clock);
// Convert an exact local hardware timestamp back to machine time. This is
// used by a CAN bridge to translate an FDCAN Tx Event timestamp after the
// bridge's local clock has been disciplined from USB SOF observations.
uint32_t timesync_local_to_clock(uint32_t local_clock);
// Ingest an exact machine/local timestamp pair captured by CAN hardware.
void timesync_ingest_can_sample(uint8_t seq, uint32_t machine_clock,
                                uint32_t local_clock);
// Report whether Class-0 (motion) ingest may trust the mapping:
// zero when the discipline filter has not converged or the last
// beacon is older than the freewheel budget.
int timesync_class0_ok(void);
int timesync_is_enabled(void);

#endif // timesync.h
