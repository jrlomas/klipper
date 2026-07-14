// GPIO edge-interrupt trigger sources on RP2040 (FD-0001 doc 09).
//
// Each GPIO has independent rising/falling edge latches in IO_BANK0.  The
// shared processor IRQ timestamps an asserted edge at ISR entry and feeds it
// directly into the generic trigger_source/trsync path.  The RP2040 system
// timer has no GPIO input-capture route, so the timestamp is necessarily the
// ISR-entry timer read rather than a hardware-captured edge tick.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "hardware/address_mapped.h" // hw_set_bits
#include "hardware/structs/iobank0.h" // iobank0_hw
#include "internal.h" // IO_IRQ_BANK0_IRQn
#include "trigger_source.h" // trigger_source_notify

#define RP2040_GPIO_COUNT 30
#define GPIO_PER_IRQ_WORD 8
#define GPIO_EVENT_BITS 4
#define GPIO_EDGE_LOW_BIT 2
#define GPIO_EDGE_HIGH_BIT 3

static struct trigger_source *gpio_irq_owner[RP2040_GPIO_COUNT];

static uint32_t
gpio_irq_word(uint32_t pin)
{
    return pin / GPIO_PER_IRQ_WORD;
}

static uint32_t
gpio_irq_mask(uint32_t pin, uint8_t rising)
{
    uint32_t shift = ((pin % GPIO_PER_IRQ_WORD) * GPIO_EVENT_BITS
                      + (rising ? GPIO_EDGE_HIGH_BIT : GPIO_EDGE_LOW_BIT));
    return 1UL << shift;
}

void
IO_IRQ_BANK0_IRQHandler(void)
{
    // Take one timestamp for the IRQ entry. Multiple edge latches serviced
    // by this pass intentionally share it; no pin can be mapped to the
    // RP2040 system timer's alarm/capture hardware.
    uint32_t isr_clock = timer_read_time();
    for (uint32_t pin = 0; pin < RP2040_GPIO_COUNT; pin++) {
        struct trigger_source *tsrc = gpio_irq_owner[pin];
        if (!tsrc)
            continue;
        uint32_t word = gpio_irq_word(pin);
        uint32_t mask = gpio_irq_mask(pin, tsrc->edge);
        if (!(iobank0_hw->proc0_irq_ctrl.ints[word] & mask))
            continue;
        // Edge status is write-one-to-clear. Clear before dispatch so a
        // qualification failure can safely re-arm and accept a fresh edge.
        iobank0_hw->intr[word] = mask;
        trigger_source_notify(tsrc, isr_clock);
    }
}

int
board_edge_trigger_setup(struct trigger_source *tsrc)
{
    uint32_t pin = tsrc->pin;
    if (pin >= RP2040_GPIO_COUNT)
        return -1;
    if (gpio_irq_owner[pin] && gpio_irq_owner[pin] != tsrc)
        return -1;

    irqstatus_t flag = irq_save();
    uint32_t word = gpio_irq_word(pin);
    uint32_t low_mask = gpio_irq_mask(pin, 0);
    uint32_t high_mask = gpio_irq_mask(pin, 1);
    hw_clear_bits(&iobank0_hw->proc0_irq_ctrl.inte[word],
                  low_mask | high_mask);
    iobank0_hw->intr[word] = low_mask | high_mask;
    gpio_irq_owner[pin] = tsrc;
    irq_restore(flag);

    // Priority 1 lets an endstop edge preempt the priority-2 scheduler IRQ.
    armcm_enable_irq(IO_IRQ_BANK0_IRQHandler, IO_IRQ_BANK0_IRQn, 1);
    return 0;
}

void
board_edge_trigger_arm(struct trigger_source *tsrc, int enable)
{
    uint32_t word = gpio_irq_word(tsrc->pin);
    uint32_t mask = gpio_irq_mask(tsrc->pin, tsrc->edge);
    if (enable) {
        iobank0_hw->intr[word] = mask;
        hw_set_bits(&iobank0_hw->proc0_irq_ctrl.inte[word], mask);
    } else {
        hw_clear_bits(&iobank0_hw->proc0_irq_ctrl.inte[word], mask);
        iobank0_hw->intr[word] = mask;
    }
}

int
board_timer_capture_setup(struct trigger_source *tsrc)
{
    (void)tsrc;
    return 0;
}

uint32_t
board_timer_capture_read(struct trigger_source *tsrc)
{
    (void)tsrc;
    return timer_read_time();
}

// RP2040's ADC FIFO can raise threshold IRQs based on FIFO depth/errors, but
// it has no per-sample high/low window comparator. Keep the generic analog
// trigger command unavailable rather than silently emulating it by polling.
int
board_adc_watchdog_setup(struct trigger_source *tsrc)
{
    (void)tsrc;
    return -1;
}

void
board_adc_watchdog_arm(struct trigger_source *tsrc, int enable)
{
    (void)tsrc;
    (void)enable;
}
