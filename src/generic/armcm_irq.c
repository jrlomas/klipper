// Definitions for irq enable/disable on ARM Cortex-M processors
//
// Copyright (C) 2017-2018  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_*
#include "board/internal.h" // __CORTEX_M
#include "irq.h" // irqstatus_t
#include "sched.h" // DECL_SHUTDOWN
#if CONFIG_MACH_STM32 && CONFIG_USB && CONFIG_HAVE_STM32_USBFS
#include "usb_sof.h" // usb_sof_board_guard_begin
#endif

// STM32 USB FS timestamps SOF at ISR entry.  A SOF that arrives while
// PRIMASK is set would otherwise remain pending and receive a falsely late
// timestamp when interrupts resume.  Discard that peripheral flag while
// PRIMASK is still set, immediately before restoring interrupts.  Endpoint
// flags remain pending and are serviced normally.
static inline uint8_t
irq_timing_guard_probe(void)
{
#if CONFIG_MACH_STM32 && CONFIG_USB && CONFIG_HAVE_STM32_USBFS
    if (!usb_sof_guard_enabled)
        return 0;
    return usb_sof_board_guard_probe();
#else
    return 0;
#endif
}

static inline void
irq_timing_guard_begin(uint32_t source, uint32_t caller, uint8_t probe)
{
#if CONFIG_MACH_STM32 && CONFIG_USB && CONFIG_HAVE_STM32_USBFS
    usb_sof_board_guard_begin(source, caller, probe);
#endif
}

static inline void
irq_timing_guard_end(uint32_t source, uint32_t caller)
{
#if CONFIG_MACH_STM32 && CONFIG_USB && CONFIG_HAVE_STM32_USBFS
    usb_sof_board_guard_end(source, caller);
#endif
}

// Use an address inside each inlined IRQ wrapper expansion instead of a
// function return address.  Link-time optimization folds most task functions
// into run_tasks(), but each expansion retains a distinct code address and
// DWARF inline-call chain.
static inline uint32_t
irq_timing_site(void)
{
    uint32_t site;
    asm volatile("mov %0, pc" : "=r" (site));
    return site;
}

void
irq_disable(void)
{
    irqstatus_t flag;
    asm volatile("mrs %0, primask" : "=r" (flag) :: "memory");
    uint8_t probe = flag ? 0 : irq_timing_guard_probe();
    uint32_t source = irq_timing_site();
    uint32_t caller = (uint32_t)__builtin_return_address(0);
    asm volatile("cpsid i" ::: "memory");
    if (!flag && probe)
        irq_timing_guard_begin(source, caller, probe);
}

void
irq_enable(void)
{
    uint32_t source = irq_timing_site();
    uint32_t caller = (uint32_t)__builtin_return_address(0);
    irq_timing_guard_end(source, caller);
    asm volatile("cpsie i" ::: "memory");
}

irqstatus_t
irq_save(void)
{
    irqstatus_t flag;
    asm volatile("mrs %0, primask" : "=r" (flag) :: "memory");
    uint8_t probe = flag ? 0 : irq_timing_guard_probe();
    uint32_t source = irq_timing_site();
    uint32_t caller = (uint32_t)__builtin_return_address(0);
    asm volatile("cpsid i" ::: "memory");
    if (!flag && probe)
        irq_timing_guard_begin(source, caller, probe);
    return flag;
}

void
irq_restore(irqstatus_t flag)
{
    if (!flag)
        irq_timing_guard_end(
            irq_timing_site(), (uint32_t)__builtin_return_address(0));
    asm volatile("msr primask, %0" :: "r" (flag) : "memory");
}

void
irq_wait(void)
{
    uint32_t source = irq_timing_site();
    uint32_t caller = (uint32_t)__builtin_return_address(0);
    irq_timing_guard_end(source, caller);
    if (__CORTEX_M == 7)
        // Cortex-m7 may disable cpu counter on wfi, so use nop
        asm volatile("cpsie i\n    nop\n    cpsid i\n" ::: "memory");
    else
        asm volatile("cpsie i\n    wfi\n    cpsid i\n" ::: "memory");
    uint8_t probe = irq_timing_guard_probe();
    if (probe)
        irq_timing_guard_begin(
            source, caller, probe & USB_SOF_GUARD_PROBE_PENDING);
}

void
irq_poll(void)
{
}

// Clear the active irq if a shutdown happened in an irq handler
void
clear_active_irq(void)
{
    uint32_t psr;
    asm volatile("mrs %0, psr" : "=r" (psr));
    if (!(psr & 0x1ff))
        // Shutdown did not occur in an irq - nothing to do.
        return;
    // Clear active irq status
    psr = 1<<24; // T-bit
    uint32_t temp;
    asm volatile(
        "  push { %1 }\n"
        "  adr %0, 1f\n"
        "  push { %0 }\n"
        "  push { r0, r1, r2, r3, r4, lr }\n"
        "  bx %2\n"
        ".balign 4\n"
        "1:\n"
        : "=&r"(temp) : "r"(psr), "r"(0xfffffff9) : "r12", "cc");
}
DECL_SHUTDOWN(clear_active_irq);
