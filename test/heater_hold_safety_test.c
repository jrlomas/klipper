#include <assert.h>
#include <stdio.h>

#include "heater_hold_math.h"

int
main(void)
{
    // Common NTC dividers produce lower ADC values at higher temperatures.
    assert(!heater_hold_at_or_above_ceiling(0, 3600, 3200));
    assert(heater_hold_at_or_above_ceiling(0, 3200, 3200));
    assert(heater_hold_at_or_above_ceiling(0, 3000, 3200));
    assert(heater_hold_hotter_than(0, 3500, 3600));
    assert(!heater_hold_hotter_than(0, 3700, 3600));

    // The inverse sensor direction must retain the same physical meaning.
    assert(!heater_hold_at_or_above_ceiling(1, 3000, 3200));
    assert(heater_hold_at_or_above_ceiling(1, 3200, 3200));
    assert(heater_hold_at_or_above_ceiling(1, 3400, 3200));
    assert(heater_hold_hotter_than(1, 3700, 3600));
    assert(!heater_hold_hotter_than(1, 3500, 3600));

    puts("PASS: heater hold ceiling follows both ADC sensor directions");
    return 0;
}
