#ifndef __HEATER_HOLD_MATH_H
#define __HEATER_HOLD_MATH_H

#include <stdint.h>

// Compare raw ADC values while honoring the configured sensor direction.
static inline int
heater_hold_hotter_than(uint8_t invert, uint16_t adc, uint16_t reference)
{
    return invert ? adc > reference : adc < reference;
}

static inline int
heater_hold_colder_than(uint8_t invert, uint16_t adc, uint16_t reference)
{
    return invert ? adc < reference : adc > reference;
}

static inline int
heater_hold_at_or_above_ceiling(uint8_t invert, uint16_t adc,
                                uint16_t ceiling)
{
    return adc == ceiling
        || heater_hold_hotter_than(invert, adc, ceiling);
}

#endif // heater_hold_math.h
