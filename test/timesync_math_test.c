#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/timesync_math.h"

int
main(void)
{
    // Live Pico/EBB36 failure case: this must be a small negative
    // correction, not the -MAX_ADJ saturation seen with an unsigned limit.
    int32_t neg = timesync_err_to_adj(-4399, 11815309);
    assert(neg < -6000 && neg > -7000);
    assert(timesync_err_to_adj(4399, 11815309) == -neg);

    assert(timesync_err_to_adj(INT32_MAX, 1) == MAX_ADJ);
    assert(timesync_err_to_adj(INT32_MIN, 1) == -MAX_ADJ);
    printf("PASS: timesync signed corrections remain symmetric and bounded\n");
    return 0;
}
