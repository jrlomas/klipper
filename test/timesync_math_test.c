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

    uint8_t good = 0, bad = 0, converged = 0;
    converged = timesync_update_convergence(converged, &good, &bad, 0, 640);
    assert(!converged && good == 1 && bad == 0);
    converged = timesync_update_convergence(converged, &good, &bad, 640, 640);
    assert(!converged && good == 2 && bad == 0);
    converged = timesync_update_convergence(converged, &good, &bad, 1, 640);
    assert(converged && good == TIMESYNC_CONVERGE_COUNT && bad == 0);

    // Live EBB36 print failure: 718 ticks is only 1.22us outside a 10us
    // window.  One such USB timestamp sample must not revoke a healthy lock.
    converged = timesync_update_convergence(converged, &good, &bad, 718, 640);
    assert(converged && good == 0 && bad == 1);
    converged = timesync_update_convergence(converged, &good, &bad, 100, 640);
    assert(converged && good == 1 && bad == 0);

    // Sustained marginal drift still fails closed on the third miss.
    for (uint8_t i = 1; i <= TIMESYNC_DIVERGE_COUNT; i++) {
        converged = timesync_update_convergence(
            converged, &good, &bad, 641, 640);
        assert(converged == (i < TIMESYNC_DIVERGE_COUNT));
        assert(bad == i);
    }

    // Reacquire, then reject a gross excursion without waiting for hysteresis.
    for (uint8_t i = 0; i < TIMESYNC_CONVERGE_COUNT; i++)
        converged = timesync_update_convergence(
            converged, &good, &bad, 0, 640);
    assert(converged);
    converged = timesync_update_convergence(converged, &good, &bad, 2561, 640);
    assert(!converged && bad == TIMESYNC_DIVERGE_COUNT);

    // A zero window retains the legacy behavior: every post-prime sample is
    // eligible, while acquisition still requires the normal sample count.
    good = bad = converged = 0;
    for (uint8_t i = 0; i < TIMESYNC_CONVERGE_COUNT; i++)
        converged = timesync_update_convergence(
            converged, &good, &bad, UINT32_MAX, 0);
    assert(converged && bad == 0);

    printf("PASS: timesync corrections and convergence hysteresis are bounded\n");
    return 0;
}
