// Fixed-point arithmetic shared by the machine-time discipline filter and
// its native regression test.
#ifndef TIMESYNC_MATH_H
#define TIMESYNC_MATH_H

#include <stdint.h>

#define RATE_SHIFT 24
#define RATE_ONE (1U << RATE_SHIFT)
#define RATE_HALF (1U << (RATE_SHIFT - 1))
// Keep this limit signed. If it inherits RATE_ONE's unsigned type then
// -MAX_ADJ is a large positive value and every negative adjustment clamps.
#define MAX_ADJ ((int32_t)(RATE_ONE / 4U))

// Discrete PI gains used by the on-board clock discipline.  The firmware
// publishes the newly integrated base rate in the same sample in which it
// applies the proportional slew, so the phase/rate state transition is:
//
//   [e']   [1-Kp-Ki  1] [e]
//   [q'] = [  -Ki    1] [q]
//
// Kp=1/4 and Ki=1/32 put that matrix's poles off the real axis and produced
// a repeatable ~44-second phase oscillation on the Pico/EBB36 scope rig.
// Ki=1/64 is slightly overdamped; retain shifts here so the stability choice
// is visible to, and regression-testable by, the host-native math test.
#define TIMESYNC_PROP_SHIFT 2
#define TIMESYNC_INTEGRAL_SHIFT 6

_Static_assert(TIMESYNC_INTEGRAL_SHIFT >= 2 * TIMESYNC_PROP_SHIFT + 2,
               "timesync PI gains must not be underdamped");

// Convergence has deliberately asymmetric hysteresis: acquiring trust takes
// several bounded samples, and revoking established trust takes several
// consecutive marginal misses.  A gross timing excursion still revokes trust
// immediately.
#define TIMESYNC_CONVERGE_COUNT 3
#define TIMESYNC_DIVERGE_COUNT 3
#define TIMESYNC_HARD_ERROR_MULTIPLIER 4U

_Static_assert(MAX_ADJ > 0 && -MAX_ADJ < 0,
               "timesync adjustment bounds must be signed");

// Full-interval rate correction (Q8.24) implied by an offset error of 'err'
// local ticks accrued over 'dm' machine ticks.
static inline int32_t
timesync_err_to_adj(int32_t err, int32_t dm)
{
    // Multiplication is defined for negative err; left-shifting a negative
    // signed value is undefined C behavior.
    int64_t adj = (int64_t)err * RATE_ONE / dm;
    if (adj > MAX_ADJ)
        return MAX_ADJ;
    if (adj < -MAX_ADJ)
        return -MAX_ADJ;
    return (int32_t)adj;
}

// Update the convergence latch from one absolute offset-error sample.  The
// counters are caller-owned so this pure helper can be regression-tested on
// the host with exactly the state transitions used by MCU firmware.
static inline uint8_t
timesync_update_convergence(uint8_t converged, uint8_t *good_count,
                            uint8_t *bad_count, uint32_t magnitude,
                            uint32_t window)
{
    if (!window || magnitude <= window) {
        *bad_count = 0;
        if (*good_count < TIMESYNC_CONVERGE_COUNT)
            (*good_count)++;
        return converged || *good_count >= TIMESYNC_CONVERGE_COUNT;
    }

    *good_count = 0;
    if (!converged)
        return 0;

    uint32_t hard_limit = window > UINT32_MAX / TIMESYNC_HARD_ERROR_MULTIPLIER
        ? UINT32_MAX : window * TIMESYNC_HARD_ERROR_MULTIPLIER;
    if (magnitude > hard_limit) {
        *bad_count = TIMESYNC_DIVERGE_COUNT;
        return 0;
    }
    if (*bad_count < TIMESYNC_DIVERGE_COUNT)
        (*bad_count)++;
    return *bad_count < TIMESYNC_DIVERGE_COUNT;
}

#endif // timesync_math.h
