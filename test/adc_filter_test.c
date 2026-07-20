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
    uint32_t filtered_value = 0;
    uint8_t filtered_ready = 0;
    for (uint32_t i = 0; i < 24; i++) {
        int ready = adc_filter_push_ex(&f, i * 3, i, &result,
                                       &filtered_value, &filtered_ready);
        if (i == 6)
            assert(filtered_ready && filtered_value == 9);
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

    config = (struct adc_filter_config) {
        .input_div = 1, .osr = 2, .report_div = 3, .shift = 1,
        .summary_mode = ADC_FILTER_SUMMARY_LATEST,
    };
    assert(!adc_filter_configure(&f, &config));
    const uint16_t latest_values[] = {10, 20, 30, 40, 50, 60};
    for (uint32_t i = 0; i < 6; i++) {
        int ready = adc_filter_push(&f, latest_values[i], i, &result);
        assert(ready == (i == 5));
    }
    assert(result.count == 1 && result.sum == 55);
    assert(result.minimum == 55 && result.maximum == 55);
    assert(result.first_scan == 5 && result.last_scan == 5);

    config = (struct adc_filter_config) {
        .input_div = 1, .osr = 4, .report_div = 1,
        .summary_mode = ADC_FILTER_SUMMARY_LATEST,
    };
    assert(!adc_filter_configure(&f, &config));
    assert(!adc_filter_set_postprocess(&f, 4, ADC_FILTER_ALPHA_ONE / 2));
    const uint16_t ewma_values[] = {
        100, 100, 100, 100, 200, 200, 200, 200, 100, 100, 100, 100,
    };
    const uint32_t ewma_expected[] = {100, 150, 125};
    for (uint32_t i = 0; i < 12; i++) {
        int ready = adc_filter_push_ex(&f, ewma_values[i], i, &result,
                                       &filtered_value, &filtered_ready);
        if ((i & 3) != 3) {
            assert(!ready && !filtered_ready);
            continue;
        }
        uint32_t group = i / 4;
        assert(ready && filtered_ready);
        assert(filtered_value == ewma_values[i]);
        assert(result.sum == ewma_expected[group]);
    }
    adc_filter_reset(&f, 1);
    for (uint32_t i = 0; i < 4; i++)
        assert(adc_filter_push(&f, 50, i, &result) == (i == 3));
    assert(result.sum == 50 && result.flags == ADC_FILTER_FLAG_DISCONTINUITY);

    config.osr = 0;
    assert(adc_filter_configure(&f, &config) < 0);
    (void)argc;
    (void)argv;
    puts("PASS: ADC filter decimation, rounding, summaries, and reset");
    return 0;
}
