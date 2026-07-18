#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "src/generic/adc_safety.h"

int
main(void)
{
    struct adc_safety safety;
    struct adc_safety_config cfg = {
        .deadline_ticks = 100, .low = 1000, .high = 3000,
        .fault_count = 3, .fail_action = ADC_SAFETY_HOLD,
    };
    assert(!adc_safety_configure(&safety, &cfg));
    assert(!adc_safety_check_value(&safety, 999));
    assert(!adc_safety_check_value(&safety, 4000));
    assert(adc_safety_check_value(&safety, 4000)
           == ADC_SAFETY_EVENT_THRESHOLD);
    assert(!adc_safety_check_value(&safety, 2000));

    uint32_t deadline;
    assert(!adc_safety_begin_report(&safety, 7, UINT32_MAX - 20, &deadline));
    assert(deadline == 79);
    assert(!adc_safety_check_deadline(&safety, 78));
    assert(adc_safety_ack(&safety, 6));
    assert(!adc_safety_ack(&safety, 7));
    assert(!adc_safety_begin_report(&safety, 8, 1000, &deadline));
    assert(adc_safety_begin_report(&safety, 9, 1001, &deadline)
           == ADC_SAFETY_EVENT_REPLACED);
    assert(!adc_safety_check_deadline(&safety, 1099));
    assert(adc_safety_check_deadline(&safety, 1100)
           == ADC_SAFETY_EVENT_UNACKED);

    cfg.fail_action = ADC_SAFETY_NONE;
    assert(adc_safety_configure(&safety, &cfg));
    cfg.deadline_ticks = 0;
    cfg.fail_action = ADC_SAFETY_HOLD;
    assert(!adc_safety_configure(&safety, &cfg));
    cfg.fault_count = 0;
    assert(adc_safety_configure(&safety, &cfg));
    puts("PASS: ADC threshold and Class-0 deadline safety policy");
    return 0;
}
