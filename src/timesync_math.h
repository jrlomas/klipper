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

#endif // timesync_math.h
