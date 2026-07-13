// APP-CPU (core 1) entry stub and private vector table for the
// "IDF as modem" architecture (FD-0001 doc 12 stage 3)
//
// The bare klipper core needs its own VECBASE because it must not
// share IDF's FreeRTOS-coupled vectors.  With windowed-ABI code,
// the six window overflow/underflow vectors are exercised constantly
// (every deep call chain) and MUST be present; they are the
// canonical Xtensa sequences, reproduced verbatim from ESP-IDF
// v5.3.2 components/xtensa/xtensa_vectors.S lines 2024-2233 (which
// carries them from Cadence's Xtensa HAL, MIT-style licensed).  The
// alloca (MOVSP) exception handler is the canonical _xt_alloca_exc
// from the same file.  Everything else - interrupts (never enabled;
// INTENABLE=0 and the core-1 interrupt matrix is cleared by
// appcpu_boot.c before unstall), kernel/user exceptions, double
// exceptions - parks the core in a fatal loop after recording
// EXCCAUSE/EPC1/EXCVADDR into esp32_core1_fault[], which the modem
// core reports over its log (modem.c).
//
// Vector offsets are the ESP32 LX6 core configuration constants
// (XCHAL_*_VECOFS, ESP-IDF components/xtensa/esp32/include/xtensa/
// config/core-isa.h lines 559-597): window vectors at 0x0..0x17F,
// int level 2/3/4/5 at 0x180/0x1C0/0x200/0x240, debug(6) at 0x280,
// NMI(7) at 0x2C0, kernel at 0x300, user at 0x340, double exception
// at 0x3C0.  VECBASE requires 1KiB alignment.
//
// The entry stub replays what the ROM + IDF give a C entry point:
// reset window state, mask all interrupts, install this vector
// table, switch to a private DRAM stack, and call4 into
// appcpu_main() (appcpu_boot.c).  PS is set to WOE|UM with
// INTLEVEL=0 - interrupts are architecturally enabled but none is
// unmasked, matching the polled bare runtime.
//
// RUNTIME UNPROVEN: assembled only by the xtensa toolchain (not
// available in this build environment) and never run on silicon.
// See the bring-up checklist in docs/ESP32.md.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#if defined(__XTENSA__)

// EXCSAVE_1 is a configured Xtensa special-register number, not an assembler
// register alias.  Pull in the target definitions used by ESP-IDF's vectors.
#include "xtensa/config/specreg.h"

/****************************************************************
 * Core-1 stack (private; DRAM)
 ****************************************************************/

    .section .bss.appcpu_stack, "aw", @nobits
    .align  16
    .global esp32_appcpu_stack
esp32_appcpu_stack:
    .space  16384
esp32_appcpu_stack_top:

/****************************************************************
 * Vector table (IRAM, 1KiB aligned)
 ****************************************************************/

    .section .iram1.appcpu_vectors, "ax"
    .align  1024
    .global esp32_appcpu_vectors
esp32_appcpu_vectors:

/* --- Window overflow/underflow: canonical sequences (see header
       comment for provenance).  Do not modify. --- */

    .org    0x0                     /* XCHAL_WINDOW_OF4_VECOFS */
_appcpu_WindowOverflow4:
    s32e    a0, a5, -16
    s32e    a1, a5, -12
    s32e    a2, a5,  -8
    s32e    a3, a5,  -4
    rfwo

    .org    0x40                    /* XCHAL_WINDOW_UF4_VECOFS */
_appcpu_WindowUnderflow4:
    l32e    a0, a5, -16
    l32e    a1, a5, -12
    l32e    a2, a5,  -8
    l32e    a3, a5,  -4
    rfwu

/* Alloca/MOVSP exception continuation - canonical _xt_alloca_exc,
   placed in the free space between the window vectors exactly as
   IDF does.  Entered from the user-exception vector with the
   original a0 in EXCSAVE_1.  PS_OWB shift/width = 8/4 (ESP32 LX6,
   core-isa.h XCHAL_PS_OWB_*). */
    .align  4
_appcpu_alloca_exc:
    rsr     a0, WINDOWBASE          /* grab WINDOWBASE before rotw changes it */
    rotw    -1                      /* WINDOWBASE goes to a4, new a0-a3 are scratch */
    rsr     a2, PS
    extui   a3, a2, 8, 4            /* XCHAL_PS_OWB_SHIFT, XCHAL_PS_OWB_BITS */
    xor     a3, a3, a4              /* bits changed from old to current windowbase */
    rsr     a4, EXCSAVE_1           /* restore original a0 (now in a4) */
    slli    a3, a3, 8
    xor     a2, a2, a3              /* flip changed bits in old window base */
    wsr     a2, PS                  /* update PS.OWB to new window base */
    rsync

    bbci.l  a4, 31, _appcpu_WindowUnderflow4
    rotw    -1                      /* original a0 goes to a8 */
    bbci.l  a8, 30, _appcpu_WindowUnderflow8
    rotw    -1
    j       _appcpu_WindowUnderflow12

    .org    0x80                    /* XCHAL_WINDOW_OF8_VECOFS */
_appcpu_WindowOverflow8:
    s32e    a0, a9, -16
    l32e    a0, a1, -12             /* a0 <- call[j-1]'s sp */
    s32e    a1, a9, -12
    s32e    a2, a9,  -8
    s32e    a3, a9,  -4
    s32e    a4, a0, -32
    s32e    a5, a0, -28
    s32e    a6, a0, -24
    s32e    a7, a0, -20
    rfwo

    .org    0xC0                    /* XCHAL_WINDOW_UF8_VECOFS */
_appcpu_WindowUnderflow8:
    l32e    a0, a9, -16
    l32e    a1, a9, -12
    l32e    a2, a9,  -8
    l32e    a7, a1, -12             /* a7 <- call[i-1]'s sp */
    l32e    a3, a9,  -4
    l32e    a4, a7, -32
    l32e    a5, a7, -28
    l32e    a6, a7, -24
    l32e    a7, a7, -20
    rfwu

    .org    0x100                   /* XCHAL_WINDOW_OF12_VECOFS */
_appcpu_WindowOverflow12:
    s32e    a0,  a13, -16
    l32e    a0,  a1,  -12           /* a0 <- call[j-1]'s sp */
    s32e    a1,  a13, -12
    s32e    a2,  a13,  -8
    s32e    a3,  a13,  -4
    s32e    a4,  a0,  -48
    s32e    a5,  a0,  -44
    s32e    a6,  a0,  -40
    s32e    a7,  a0,  -36
    s32e    a8,  a0,  -32
    s32e    a9,  a0,  -28
    s32e    a10, a0,  -24
    s32e    a11, a0,  -20
    rfwo

    .org    0x140                   /* XCHAL_WINDOW_UF12_VECOFS */
_appcpu_WindowUnderflow12:
    l32e    a0,  a13, -16
    l32e    a1,  a13, -12
    l32e    a2,  a13,  -8
    l32e    a11, a1,  -12           /* a11 <- call[i-1]'s sp */
    l32e    a3,  a13,  -4
    l32e    a4,  a11, -48
    l32e    a5,  a11, -44
    l32e    a6,  a11, -40
    l32e    a7,  a11, -36
    l32e    a8,  a11, -32
    l32e    a9,  a11, -28
    l32e    a10, a11, -24
    l32e    a11, a11, -20
    rfwu

/* --- Everything below parks the core.  No interrupt is ever
       enabled on the bare core; reaching one of these means a bug.
       Registers are expendable on the way to a park loop. --- */

    .org    0x180                   /* XCHAL_INTLEVEL2_VECOFS */
_appcpu_Level2Vector:
    movi    a0, 0x102
    j       _appcpu_fatal_code

    .org    0x1C0                   /* XCHAL_INTLEVEL3_VECOFS */
_appcpu_Level3Vector:
    movi    a0, 0x103
    j       _appcpu_fatal_code

    .org    0x200                   /* XCHAL_INTLEVEL4_VECOFS */
_appcpu_Level4Vector:
    movi    a0, 0x104
    j       _appcpu_fatal_code

    .org    0x240                   /* XCHAL_INTLEVEL5_VECOFS */
_appcpu_Level5Vector:
    movi    a0, 0x105
    j       _appcpu_fatal_code

    .org    0x280                   /* XCHAL_DEBUG_VECOFS (level 6) */
_appcpu_DebugVector:
    movi    a0, 0x106
    j       _appcpu_fatal_code

    .org    0x2C0                   /* XCHAL_NMI_VECOFS (level 7) */
_appcpu_NMIVector:
    movi    a0, 0x107
    j       _appcpu_fatal_code

    .org    0x300                   /* XCHAL_KERNEL_VECOFS */
_appcpu_KernelExceptionVector:
    wsr     a0, EXCSAVE_1
    rsr     a0, EXCCAUSE
    j       _appcpu_fatal_cause

    .org    0x340                   /* XCHAL_USER_VECOFS */
_appcpu_UserExceptionVector:
    wsr     a0, EXCSAVE_1
    rsr     a0, EXCCAUSE
    beqi    a0, 5, _appcpu_alloca_exc   /* EXCCAUSE_ALLOCA */
    j       _appcpu_fatal_cause

    .org    0x3C0                   /* XCHAL_DOUBLEEXC_VECOFS */
_appcpu_DoubleExceptionVector:
    movi    a0, 0x1FF
    j       _appcpu_fatal_code

/* Fatal park: record {flag, cause, epc1, excvaddr} into
   esp32_core1_fault[] (appcpu_boot.c) for the modem core to report,
   then spin forever.  a0 carries the cause; a1 is clobbered (the
   core is dead anyway). */
    .align  4
    .literal_position
_appcpu_fatal_code:                 /* a0 = 0x1nn synthetic code */
_appcpu_fatal_cause:                /* a0 = EXCCAUSE */
    movi    a1, esp32_core1_fault
    s32i    a0, a1, 4               /* [1] cause */
    rsr     a0, EPC1
    s32i    a0, a1, 8               /* [2] epc1 (garbage for 0x1nn codes) */
    rsr     a0, EXCVADDR
    s32i    a0, a1, 12              /* [3] excvaddr */
    movi    a0, 1
    s32i    a0, a1, 0               /* [0] flag - written last */
    memw
1:  j       1b

/****************************************************************
 * Entry from the APP-CPU boot ROM
 ****************************************************************/

/* The ROM polls the boot-address register (set by core 0 via
   ets_set_appcpu_boot_addr) and jumps here with its own stack and
   window state.  Reset the window bookkeeping (canonical crt
   sequence), mask everything, install our vectors, switch to the
   private stack and enter C.  PS = 0x40020: WOE (bit 18) | UM
   (bit 5), INTLEVEL 0. */

    .section .iram1.appcpu_entry, "ax"
    .align  4
    .literal_position
    .global appcpu_entry
    .type   appcpu_entry, @function
appcpu_entry:
    /* Window state: WINDOWSTART=1, WINDOWBASE=0.  Registers rotate
       at the windowbase write; use only a0 until the rsync and
       reload everything afterwards. */
    movi    a0, 1
    wsr     a0, WINDOWSTART
    movi    a0, 0
    wsr     a0, WINDOWBASE
    rsync

    /* No interrupts, ever (the runtime is polled) */
    movi    a0, 0
    wsr     a0, INTENABLE

    /* Private vector table */
    movi    a0, esp32_appcpu_vectors
    wsr     a0, VECBASE

    /* Windowed ABI on, user vector mode, INTLEVEL 0 */
    movi    a0, 0x40020
    wsr     a0, PS
    rsync

    /* Private stack; a0=0 terminates call/backtrace chains */
    movi    a1, esp32_appcpu_stack_top
    movi    a0, 0

    /* Enter the bare runtime (never returns) */
    movi    a2, appcpu_main
    callx4  a2
1:  j       1b

#endif /* __XTENSA__ */
