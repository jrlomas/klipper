/* First-class bootloader link script — STM32F072 (RFC 0001 doc 11).
 *
 * 16 KB bootloader at flash base; the application (linked with the
 * ordinary armcm_link.lds.S and CONFIG_FLASH_APPLICATION_ADDRESS =
 * 0x08004000) occupies the rest, minus the trailing 2 KB validity
 * page. _boot_reqword is placed at RAM top - 1032, matching the
 * application's INTENTPROTO_BOOT_REQ_ADDR (bootentry.h) so the entry
 * request survives the reset and is seen here.
 *
 * The bootloader reuses the generic armcm startup (armcm_boot.c),
 * whose ResetHandler calls boot_main().
 */

OUTPUT_FORMAT("elf32-littlearm", "elf32-littlearm", "elf32-littlearm")
OUTPUT_ARCH(arm)

MEMORY
{
  rom (rx)  : ORIGIN = 0x08000000, LENGTH = 16K
  ram (rwx) : ORIGIN = 0x20000000, LENGTH = 16K
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
