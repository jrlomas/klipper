// Timer input-capture hardware timestamps for trigger sources
// (FD-0001 doc 09 section 3).
//
// Detecting an edge (gpio_exti.c) and timestamping it are separate
// jobs. The EXTI ISR can only read the clock at *ISR entry* -- later
// than the physical edge by interrupt latency plus ISR jitter. A
// timer capture unit latches the counter on the edge itself, in
// hardware, giving a timestamp good to ~1 tick regardless of when the
// CPU gets around to servicing the interrupt.
//
// Semantics / open question from doc 09: the captured tick is exact,
// but the *stop* still begins at ISR time (trsync fires from the EXTI
// handler, which ran after the edge). So the trsync record carries
// the hardware-exact trigger time while the actuator's actual halt
// lagged it by the ISR latency. Probe math should use the capture
// timestamp for the trigger *position*, understanding that the
// mechanical stop trailed it slightly -- the two are not the same
// instant and downstream trajectory reconstruction must not conflate
// them.
//
// This is exact only where the free-running system-time counter is
// itself a capture-capable hardware timer. On STM32F0/G0 the system
// clock is the 32-bit TIM2 (see stm32f0_timer.c), so a TIM2 capture
// channel latches directly in timer_read_time()'s own tick base --
// no correlation math, no second time domain. On families whose
// timebase is the Cortex-M cycle counter (F1/F2/F4/F7 via
// armcm_timer.c) no hardware timer shares that base, so capture is
// left unwired here and callers fall back to the ISR-entry read.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_MACH_STM32G0
#include "board/misc.h" // timer_read_time
#include "compiler.h" // ARRAY_SIZE
#include "internal.h" // TIM2, GPIO
#include "trigger_source.h" // board_timer_capture_setup

#if (CONFIG_MACH_STM32F0 || CONFIG_MACH_STM32G0) && defined(TIM2)

// The 32-bit TIM2 is the system-time counter on these families and
// its CH1 is reserved for the scheduler's compare (stm32f0_timer.c),
// so only CH2/CH3/CH4 are available for input capture. Any capture
// they latch is directly a timer_read_time() tick.
struct capture_pin { uint8_t pin; uint8_t channel; };
static const struct capture_pin capture_pins[] = {
    { GPIO('A', 1),  2 }, { GPIO('B', 3),  2 },
    { GPIO('A', 2),  3 }, { GPIO('B', 10), 3 },
    { GPIO('A', 3),  4 }, { GPIO('B', 11), 4 },
};

int
board_timer_capture_setup(struct trigger_source *tsrc)
{
    uint8_t channel = 0;
    for (int i = 0; i < ARRAY_SIZE(capture_pins); i++)
        if (capture_pins[i].pin == tsrc->pin) {
            channel = capture_pins[i].channel;
            break;
        }
    if (!channel)
        return 0; // Pin not routable to a free TIM2 capture channel

    // Route pin to TIM2 (AF2 on F0/G0) as an input-capture input
    gpio_peripheral(tsrc->pin, GPIO_FUNCTION(2), 0);

    // Configure the channel: map ICx to TIx (CCyS=01), no prescale/
    // filter, capture on the same edge the EXTI detection uses
    // (rising == trigger level high). Then enable the capture.
    uint8_t rising = tsrc->edge;
    switch (channel) {
    case 2:
        TIM2->CCMR1 = (TIM2->CCMR1 & ~TIM_CCMR1_CC2S_Msk) | TIM_CCMR1_CC2S_0;
        TIM2->CCER &= ~(TIM_CCER_CC2P | TIM_CCER_CC2NP);
        if (!rising)
            TIM2->CCER |= TIM_CCER_CC2P;
        TIM2->CCER |= TIM_CCER_CC2E;
        break;
    case 3:
        TIM2->CCMR2 = (TIM2->CCMR2 & ~TIM_CCMR2_CC3S_Msk) | TIM_CCMR2_CC3S_0;
        TIM2->CCER &= ~(TIM_CCER_CC3P | TIM_CCER_CC3NP);
        if (!rising)
            TIM2->CCER |= TIM_CCER_CC3P;
        TIM2->CCER |= TIM_CCER_CC3E;
        break;
    case 4:
        TIM2->CCMR2 = (TIM2->CCMR2 & ~TIM_CCMR2_CC4S_Msk) | TIM_CCMR2_CC4S_0;
        TIM2->CCER &= ~(TIM_CCER_CC4P | TIM_CCER_CC4NP);
        if (!rising)
            TIM2->CCER |= TIM_CCER_CC4P;
        TIM2->CCER |= TIM_CCER_CC4E;
        break;
    }
    tsrc->hw[0] = channel;
    return 1;
}

uint32_t
board_timer_capture_read(struct trigger_source *tsrc)
{
    switch (tsrc->hw[0]) {
    case 2: return TIM2->CCR2;
    case 3: return TIM2->CCR3;
    case 4: return TIM2->CCR4;
    }
    return timer_read_time();
}

#else

// No capturable system-time timer on this family: leave capture
// unwired so callers fall back to the ISR-entry timestamp.
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

#endif
