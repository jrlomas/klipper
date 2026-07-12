// Minimal Cortex-M startup for the first-class bootloader (RFC 0001
// doc 11).
//
// The bootloader owns the reset vector of the combined "one build, one
// flash" image, so it needs its own tiny startup rather than the full
// firmware's armcm_boot.c (which pulls in the compile_time_request /
// buildcommands.py vector-table machinery and the whole firmware build
// graph). This file is self-contained: a 16-entry system vector table
// placed at flash base by boot_stm32*.ld, a reset handler that sets up
// .data/.bss, runs the C++ static constructors (the KLIPPER_METHOD /
// KLIPPER_RESPONSE descriptors register themselves there — without
// .init_array the boot commands would never appear), and then calls
// boot_main(). The two port hooks boot_main.cpp declares weak
// (boot_system_reset, boot_jump_to_app) get their real definitions
// here.
//
// The device header (for SCB / NVIC_SystemReset / __VTOR_PRESENT) is
// selected per-target by the Makefile via -DBOOT_DEVICE_HEADER.

#include <stdint.h>
#include BOOT_DEVICE_HEADER

// Symbols from boot_stm32*.ld.
extern uint32_t _data_start, _data_end, _data_flash;
extern uint32_t _bss_start, _bss_end;
extern uint32_t _stack_end;
extern void (*__init_array_start[])(void);
extern void (*__init_array_end[])(void);

// The bootloader entry point (src/boot_app/boot_main.cpp).
extern void boot_main(void);

void ResetHandler(void);

// Any unexpected exception simply parks the core; the watchdog (or a
// power cycle) then re-enters the bootloader — never a hard brick.
static void
Default_Handler(void)
{
    for (;;)
        ;
}

// Cortex-M system vector table. Only the reset path is exercised (the
// transport is polled, so no device IRQs are wired); the fault vectors
// point at Default_Handler for safety. Placed at the image base
// (0x08000000) by the .vector_table output section.
__attribute__((section(".vector_table"), used))
void (*const g_boot_vectors[16])(void) = {
    (void (*)(void))(uintptr_t)&_stack_end, // 0: initial MSP
    ResetHandler,                           // 1: reset
    Default_Handler,                        // 2: NMI
    Default_Handler,                        // 3: HardFault
    Default_Handler,                        // 4: MemManage
    Default_Handler,                        // 5: BusFault
    Default_Handler,                        // 6: UsageFault
    0, 0, 0, 0,                             // 7-10: reserved
    Default_Handler,                        // 11: SVCall
    Default_Handler,                        // 12: DebugMon
    0,                                      // 13: reserved
    Default_Handler,                        // 14: PendSV
    Default_Handler,                        // 15: SysTick
};

// Reset entry. The CPU has already loaded MSP from vector[0], so we go
// straight to C runtime setup.
void
ResetHandler(void)
{
    __disable_irq();

    // Copy initialized data from flash to RAM.
    uint32_t *src = &_data_flash;
    for (uint32_t *dst = &_data_start; dst < &_data_end;)
        *dst++ = *src++;

    // Zero the bss.
    for (uint32_t *dst = &_bss_start; dst < &_bss_end;)
        *dst++ = 0;

    // Run C++ static constructors: this is where every KLIPPER_METHOD /
    // KLIPPER_RESPONSE descriptor links itself into the intentproto
    // registry, before boot_main() calls init().
    for (void (**f)(void) = __init_array_start; f < __init_array_end; f++)
        (*f)();

    boot_main();

    for (;;)
        ;
}

// ---- port hooks declared weak in boot_main.cpp ----

// Software reset used after a successful flash_boot so the freshly
// written application starts from a clean vector table (the next boot
// re-verifies the CRC and jumps — unbrickable by construction).
void
boot_system_reset(void)
{
    NVIC_SystemReset();
}

// Standard Cortex-M application handoff: point the vector table at the
// application (where the core has one), load its initial stack pointer
// and reset vector, and branch. Does not return.
void
boot_jump_to_app(uint32_t app_base)
{
    uint32_t app_sp = *(volatile uint32_t *)(uintptr_t)app_base;
    uint32_t app_pc = *(volatile uint32_t *)(uintptr_t)(app_base + 4);
#if defined(__VTOR_PRESENT) && (__VTOR_PRESENT == 1U)
    SCB->VTOR = app_base;
    __DSB();
    __ISB();
#endif
    __asm volatile("msr msp, %0\n"
                   "bx   %1\n"
                   :
                   : "r"(app_sp), "r"(app_pc)
                   : "memory");
}
