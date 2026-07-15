// Watchdog code on rp2040
//
// Copyright (C) 2021-2022  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdint.h> // uint32_t
#include "command.h" // DECL_COMMAND_FLAGS
#include "hardware/structs/psm.h" // psm_hw
#include "hardware/structs/watchdog.h" // watchdog_hw
#include "sched.h" // DECL_TASK

void
command_get_reset_reason(uint32_t *args)
{
    (void)args;
    // REASON is retained across reset.  Bit 0 denotes a watchdog timer
    // expiry and bit 1 a forced watchdog reset; zero covers power-on and
    // reset paths outside the watchdog block (including ARM SYSRESETREQ).
    sendf("reset_reason flags=%c", watchdog_hw->reason & 0x03);
}
DECL_COMMAND_FLAGS(command_get_reset_reason, HF_IN_SHUTDOWN,
                   "get_reset_reason");

void
watchdog_reset(void)
{
    watchdog_hw->load = 0x800000; // ~350ms
}
DECL_TASK(watchdog_reset);

void
watchdog_init(void)
{
    psm_hw->wdsel = PSM_WDSEL_BITS & ~(PSM_WDSEL_ROSC_BITS|PSM_WDSEL_XOSC_BITS);
    watchdog_reset();
    watchdog_hw->ctrl = (WATCHDOG_CTRL_PAUSE_DBG0_BITS
                         | WATCHDOG_CTRL_PAUSE_DBG1_BITS
                         | WATCHDOG_CTRL_PAUSE_JTAG_BITS
                         | WATCHDOG_CTRL_ENABLE_BITS);
}
DECL_INIT(watchdog_init);
