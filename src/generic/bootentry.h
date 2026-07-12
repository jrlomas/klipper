#ifndef __GENERIC_BOOTENTRY_H
#define __GENERIC_BOOTENTRY_H
// First-class bootloader entry convention (RFC 0001 doc 11).
//
// enter_bootloader stamps a magic request word in a no-init RAM slot
// that survives a software reset (SRAM keeps its contents across
// NVIC_SystemReset) and then resets. The first-class bootloader —
// which owns the reset vector in the combined "one build, one flash"
// image (src/boot_app/) — reads this slot at startup, before it
// clears .bss, and stays in the in-band update loop when it sees the
// magic. When no first-class bootloader is installed the same command
// additionally issues the Katapult/CanBoot request (armcm_reset.c), so
// existing host "request bootloader" tooling keeps working unchanged.

#include "autoconf.h" // CONFIG_RAM_START

// No-init request slot: 8 bytes below Katapult/dfu_reboot's own flag
// (dfu_reboot.c uses RAM top - 1024), so the two never collide, and
// well below the stack region (RAM top - CONFIG_STACK_SIZE).
#define INTENTPROTO_BOOT_REQ_ADDR                                       \
    (CONFIG_RAM_START + CONFIG_RAM_SIZE - 1024 - 8)

// "IPBOOT" request magic. Chosen distinct from the CanBoot and
// "USB BOOT" signatures so a stray value cannot be mistaken for it.
#define INTENTPROTO_BOOT_REQUEST 0x49504200544f4f42ULL // BOOT\0BPI

// Command handler weak hook: motion subsystems that can report an
// active print override this (see traj_stepper.c). Default: idle.
int bootloader_entry_busy(void);

#endif // bootentry.h
