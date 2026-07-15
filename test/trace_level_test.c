#include <assert.h>
#include <stdio.h>

#include "src/trace.h"

int
main(void)
{
    // OFF is a sentinel.  Its large wire value must not be treated as the
    // least-restrictive numeric threshold.
    for (uint8_t level = TRACE_LVL_ERROR; level <= TRACE_LVL_DEBUG; level++)
        assert(!trace_level_enabled(TRACE_LVL_OFF, level));

    assert(trace_level_enabled(TRACE_LVL_ERROR, TRACE_LVL_ERROR));
    assert(!trace_level_enabled(TRACE_LVL_ERROR, TRACE_LVL_WARN));
    assert(trace_level_enabled(TRACE_LVL_WARN, TRACE_LVL_ERROR));
    assert(trace_level_enabled(TRACE_LVL_WARN, TRACE_LVL_WARN));
    assert(!trace_level_enabled(TRACE_LVL_WARN, TRACE_LVL_INFO));
    assert(trace_level_enabled(TRACE_LVL_INFO, TRACE_LVL_INFO));
    assert(!trace_level_enabled(TRACE_LVL_INFO, TRACE_LVL_DEBUG));
    assert(trace_level_enabled(TRACE_LVL_DEBUG, TRACE_LVL_DEBUG));

    printf("PASS: trace OFF sentinel and severity thresholds are bounded\n");
    return 0;
}
