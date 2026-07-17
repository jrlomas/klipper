#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/adc_filter.h"

int
main(int argc, char **argv)
{
    struct adc_filter f;
    struct adc_filter_config config = {
        .input_div = 2, .osr = 4, .report_div = 3, .shift = 2,
    };
    assert(!adc_filter_configure(&f, &config));
    struct adc_filter_summary result;
    for (uint32_t i = 0; i < 24; i++) {
        int ready = adc_filter_push(&f, i * 3, i, &result);
        if (i != 22)
            assert(!ready);
        else {
            assert(ready);
            // Accepted groups are [0,6,12,18], [24,30,36,42], and
            // [48,54,60,66], rounded after a two-bit shift.
            assert(result.count == 3);
            assert(result.minimum == 9 && result.maximum == 57);
            assert(result.sum == 99);
            assert(result.first_scan == 6 && result.last_scan == 22);
            assert(!result.flags);
        }
    }

    adc_filter_reset(&f, 1);
    for (uint32_t i = 0; i < 22; i++)
        assert(!adc_filter_push(&f, 100, i, &result));
    assert(adc_filter_push(&f, 100, 22, &result));
    assert(result.flags == ADC_FILTER_FLAG_DISCONTINUITY);

    config.osr = 0;
    assert(adc_filter_configure(&f, &config) < 0);
    (void)argc;
    (void)argv;
    puts("PASS: ADC filter decimation, rounding, summaries, and reset");
    return 0;
}
