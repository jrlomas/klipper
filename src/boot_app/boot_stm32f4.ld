/* First-class bootloader link script — STM32F4 / F407 (FD-0001
 * doc 11).
 *
 * 32 KB bootloader in sectors 0-1; the application starts at sector 2
 * (0x08008000, CONFIG_FLASH_APPLICATION_ADDRESS). Large sectors
 * dictate the layout: sector 7 (last 128 KB) holds the validity
 * record. _boot_reqword at main-SRAM top - 1032 matches the
 * application's INTENTPROTO_BOOT_REQ_ADDR. Reuses armcm_boot.c
 * startup (ResetHandler -> boot_main()).
 */

OUTPUT_FORMAT("elf32-littlearm", "elf32-littlearm", "elf32-littlearm")
OUTPUT_ARCH(arm)

MEMORY
{
  rom (rx)  : ORIGIN = 0x08000000, LENGTH = 32K
  ram (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
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

    /* C++ static constructors: the KLIPPER_METHOD / KLIPPER_RESPONSE
     * descriptors register themselves here, so boot_startup.c must run
     * this array before boot_main(). */
    .init_array : {
        . = ALIGN(4);
        __init_array_start = .;
        KEEP(*(SORT(.init_array.*)))
        KEEP(*(.init_array))
        __init_array_end = .;
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
