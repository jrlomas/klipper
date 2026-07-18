#ifndef __STM32_ADC_WATCHDOG_H
#define __STM32_ADC_WATCHDOG_H

#include "autoconf.h"
#include "internal.h"

#if CONFIG_WANT_TRIGGER_SOURCE \
    && (CONFIG_MACH_STM32F2 || CONFIG_MACH_STM32F4 || CONFIG_MACH_STM32F7)
uint32_t stm32_adc_watchdog_stream_configure(ADC_TypeDef *adc,
                                              uint32_t channel_mask);
void stm32_adc_watchdog_stream_stopped(ADC_TypeDef *adc);
#else
static inline uint32_t
stm32_adc_watchdog_stream_configure(ADC_TypeDef *adc, uint32_t channel_mask)
{
    (void)adc;
    (void)channel_mask;
    return 0;
}
static inline void
stm32_adc_watchdog_stream_stopped(ADC_TypeDef *adc) { (void)adc; }
#endif

#endif // stm32/adc_watchdog.h
