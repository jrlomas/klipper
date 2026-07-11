// GPIO edge-interrupt trigger sources via EXTI (RFC 0001 doc 09).
//
// Board half of src/trigger_source.c for STM32: routes a GPIO pin to
// its EXTI line, latches a timestamp at IRQ entry, and delivers the
// event to the generic qualification layer.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_MACH_*
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // shutdown
#include "internal.h" // GPIO2PORT
#include "sched.h" // DECL_INIT
#include "trigger_source.h" // trigger_source_notify

// One trigger source may own each of the 16 EXTI pin lines
static struct trigger_source *exti_owner[16];

#if CONFIG_MACH_STM32G0
 #define EXTI_IMR   (EXTI->IMR1)
 #define EXTI_RTSR  (EXTI->RTSR1)
 #define EXTI_FTSR  (EXTI->FTSR1)
 #define HAVE_SPLIT_PENDING 1
 #define EXTI_RPR   (EXTI->RPR1)
 #define EXTI_FPR   (EXTI->FPR1)
#else
 #define EXTI_IMR   (EXTI->IMR)
 #define EXTI_RTSR  (EXTI->RTSR)
 #define EXTI_FTSR  (EXTI->FTSR)
 #define HAVE_SPLIT_PENDING 0
 #define EXTI_PR    (EXTI->PR)
#endif

static void
exti_route_pin(uint32_t pin)
{
    uint32_t line = pin % 16, port = GPIO2PORT(pin);
    uint32_t reg = line / 4, shift = (line % 4) * 8;
#if CONFIG_MACH_STM32G0
    EXTI->EXTICR[reg] = ((EXTI->EXTICR[reg] & ~(0xffUL << shift))
                         | (port << shift));
#elif CONFIG_MACH_STM32F1
    RCC->APB2ENR |= RCC_APB2ENR_AFIOEN;
    AFIO->EXTICR[reg] = ((AFIO->EXTICR[reg] & ~(0xfUL << shift / 2))
                         | (port << shift / 2));
#else
    RCC->APB2ENR |= RCC_APB2ENR_SYSCFGEN;
    shift = (line % 4) * 4;
    SYSCFG->EXTICR[reg] = ((SYSCFG->EXTICR[reg] & ~(0xfUL << shift))
                           | (port << shift));
#endif
}

static void
exti_clear_pending(uint32_t mask)
{
#if HAVE_SPLIT_PENDING
    EXTI_RPR = mask;
    EXTI_FPR = mask;
#else
    EXTI_PR = mask;
#endif
}

static uint32_t
exti_read_pending(void)
{
#if HAVE_SPLIT_PENDING
    return (EXTI_RPR | EXTI_FPR) & 0xffff;
#else
    return EXTI_PR & 0xffff;
#endif
}

// Shared IRQ body: dispatch every pending owned line
static void
exti_dispatch(uint32_t lines)
{
    uint32_t clock = timer_read_time();
    uint32_t pending = exti_read_pending() & lines;
    if (!pending)
        return;
    exti_clear_pending(pending);
    while (pending) {
        uint32_t line = __builtin_ctz(pending);
        pending &= pending - 1;
        struct trigger_source *tsrc = exti_owner[line];
        if (tsrc)
            trigger_source_notify(tsrc, clock);
    }
}

#if CONFIG_MACH_STM32G0 || CONFIG_MACH_STM32F0

void EXTI0_1_IRQHandler(void) { exti_dispatch(0x0003); }
void EXTI2_3_IRQHandler(void) { exti_dispatch(0x000c); }
void EXTI4_15_IRQHandler(void) { exti_dispatch(0xfff0); }

static void
exti_enable_line_irq(uint32_t line)
{
    if (line < 2)
        armcm_enable_irq(EXTI0_1_IRQHandler, EXTI0_1_IRQn, 1);
    else if (line < 4)
        armcm_enable_irq(EXTI2_3_IRQHandler, EXTI2_3_IRQn, 1);
    else
        armcm_enable_irq(EXTI4_15_IRQHandler, EXTI4_15_IRQn, 1);
}

#else

void EXTI0_IRQHandler(void) { exti_dispatch(0x0001); }
void EXTI1_IRQHandler(void) { exti_dispatch(0x0002); }
void EXTI2_IRQHandler(void) { exti_dispatch(0x0004); }
void EXTI3_IRQHandler(void) { exti_dispatch(0x0008); }
void EXTI4_IRQHandler(void) { exti_dispatch(0x0010); }
void EXTI9_5_IRQHandler(void) { exti_dispatch(0x03e0); }
void EXTI15_10_IRQHandler(void) { exti_dispatch(0xfc00); }

static void
exti_enable_line_irq(uint32_t line)
{
    if (line == 0)
        armcm_enable_irq(EXTI0_IRQHandler, EXTI0_IRQn, 1);
    else if (line == 1)
        armcm_enable_irq(EXTI1_IRQHandler, EXTI1_IRQn, 1);
    else if (line == 2)
        armcm_enable_irq(EXTI2_IRQHandler, EXTI2_IRQn, 1);
    else if (line == 3)
        armcm_enable_irq(EXTI3_IRQHandler, EXTI3_IRQn, 1);
    else if (line == 4)
        armcm_enable_irq(EXTI4_IRQHandler, EXTI4_IRQn, 1);
    else if (line < 10)
        armcm_enable_irq(EXTI9_5_IRQHandler, EXTI9_5_IRQn, 1);
    else
        armcm_enable_irq(EXTI15_10_IRQHandler, EXTI15_10_IRQn, 1);
}

#endif

int
board_edge_trigger_setup(struct trigger_source *tsrc)
{
    uint32_t line = tsrc->pin % 16;
    if (exti_owner[line] && exti_owner[line] != tsrc)
        // Pins sharing pin-number across ports share one EXTI line
        return -1;
    exti_owner[line] = tsrc;
    exti_route_pin(tsrc->pin);
    irq_disable();
    uint32_t mask = 1UL << line;
    if (tsrc->edge)
        EXTI_RTSR |= mask;
    else
        EXTI_FTSR |= mask;
    exti_clear_pending(mask);
    irq_enable();
    exti_enable_line_irq(line);
    return 0;
}

void
board_edge_trigger_arm(struct trigger_source *tsrc, int enable)
{
    uint32_t mask = 1UL << (tsrc->pin % 16);
    if (enable) {
        exti_clear_pending(mask);
        EXTI_IMR |= mask;
    } else {
        EXTI_IMR &= ~mask;
    }
}
