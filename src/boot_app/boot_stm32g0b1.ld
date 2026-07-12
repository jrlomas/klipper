/* First-class bootloader link script — STM32G0B1 (RFC 0001 doc 11).
 *
 * 16 KB bootloader at flash base; application at 0x08004000. 512 KB
 * dual-bank part (2x256 KB); the trailing 2 KB page holds the
 * validity record. _boot_reqword at RAM top - 1032 matches the
 * application's INTENTPROTO_BOOT_REQ_ADDR. Reuses armcm_boot.c
 * startup (ResetHandler -> boot_main()).
 */

OUTPUT_FORMAT("elf32-littlearm", "elf32-littlearm", "elf32-littlearm")
OUTPUT_ARCH(arm)

MEMORY
{
  rom (rx)  : ORIGIN = 0x08000000, LENGTH = 16K
  ram (rwx) : ORIGIN = 0x20000000, LENGTH = 144K
}

_boot_reqword = ORIGIN(ram) + LENGTH(ram) - 1032;

SECTIONS
{
    .text : {
        . = ALIGN(4);
        _text_vectortable_start = .;
        KEEP(*(.vector_table))
        _text_vectortable_end = .;
        *(.text .text.*)
        *(.rodata .rodata*)
    } > rom

    . = ALIGN(4);
    _data_flash = .;

    .data : AT (_data_flash) {
        . = ALIGN(4);
        _data_start = .;
        *(.ramfunc .ramfunc.*);
        *(.data .data.*);
        . = ALIGN(4);
        _data_end = .;
    } > ram

    .bss (NOLOAD) : {
        . = ALIGN(4);
        _bss_start = .;
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(4);
        _bss_end = .;
    } > ram

    _stack_start = ORIGIN(ram) + LENGTH(ram) - 512;
    .stack _stack_start (NOLOAD) : {
        . = . + 512;
        _stack_end = .;
    } > ram

    /DISCARD/ : {
        *(.init) *(.fini)
        *(.ARM.extab) *(.ARM.exidx)
    }
}
