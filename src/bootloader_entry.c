// In-band bootloader entry command (FD-0001 doc 11).
//
// enter_bootloader is an ordinary Class-1 application command: it
// stamps the first-class-bootloader request magic in a no-init RAM
// slot (bootentry.h) and, for boards still running Katapult/CanBoot,
// issues that project's request too, then resets. A board mid-print
// refuses unless forced — the host is expected to drain and hold the
// queues first (doc 08 pause-and-hold); force=1 is the override for
// recovery when the queues cannot be drained.
//
// Gated behind CONFIG_WANT_BOOTLOADER (off by default on
// HAVE_LIMITED_CODE_SIZE parts), so the smallest targets pay nothing.

#include "autoconf.h" // CONFIG_RAM_START
#include "board/internal.h" // NVIC_SystemReset
#include "board/irq.h" // irq_disable
#include "command.h" // DECL_COMMAND_FLAGS
#include "generic/armcm_reset.h" // try_request_canboot
#include "generic/bootentry.h" // INTENTPROTO_BOOT_REQ_ADDR

// Default activity hook. A motion subsystem that can tell whether a
// print is in progress provides a strong override (traj_stepper.c);
// with no motion compiled in, the board is never "mid-print".
int __attribute__((weak))
bootloader_entry_busy(void)
{
    return 0;
}

// enter_bootloader force=%c
// force=0: refuse (and report) if a print is active.
// force=1: enter regardless (recovery path).
void
command_enter_bootloader(uint32_t *args)
{
    uint8_t force = args[0];
    if (!force && bootloader_entry_busy()) {
        // Refused: tell the host so it can drain and retry. result=1
        // means "busy"; the board keeps running.
        sendf("enter_bootloader_result result=%c", 1);
        return;
    }

    irq_disable();

    // First-class bootloader request: a magic word in no-init RAM the
    // src/boot_app/ bootloader reads at startup. Survives the reset.
    uint64_t *req = (uint64_t *)(uintptr_t)INTENTPROTO_BOOT_REQ_ADDR;
    *req = INTENTPROTO_BOOT_REQUEST;
#if defined(__DCACHE_PRESENT) && __DCACHE_PRESENT == 1U
    SCB_CleanDCache_by_Addr((void *)req, sizeof(*req));
#endif

    // Katapult/CanBoot compatibility: if a CanBoot bootloader is
    // installed instead, this stamps its request signature so existing
    // host "request bootloader" tooling reaches it. A no-op when no
    // CanBoot bootloader is present.
    try_request_canboot();

    NVIC_SystemReset();
}
DECL_COMMAND_FLAGS(command_enter_bootloader, HF_IN_SHUTDOWN,
                   "enter_bootloader force=%c");
