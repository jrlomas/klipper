// SPI master (SPI2/HSPI and SPI3/VSPI) on ESP32
//
// This binding is register level (soc/spi_struct.h), not the IDF
// spi_master driver, for two reasons.  First, klipper's contract is
// a synchronous polled byte transfer that may also run from the
// shutdown path (spidev_shutdown sends shutdown messages after any
// failure, including ones raised inside timer dispatch); the IDF
// driver takes FreeRTOS mutexes and is unusable there.  Second, the
// fork's ESP32 stance (FD-0001 doc 12) prefers register drivers
// against the Apache-2.0 soc headers wherever the peripheral is
// documented - and the SPI master is fully documented in the TRM.
// The transfer is a simple CPU-copy through the 64-byte W0..W15
// buffer with a busy-poll on SPI_CMD_REG.usr; no DMA, no interrupts.
//
// Pins are routed through the GPIO matrix, so any output-capable
// pins would work; the enumerated buses use the classic devkit
// IO_MUX pin sets.  Matrix routing adds an apparent half-cycle of
// MISO delay at high clock rates - the divider below therefore caps
// the bus at 20MHz, comfortably above klipper's usual 100kHz..10MHz
// device rates.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "soc/gpio_sig_map.h" // HSPICLK_OUT_IDX
#include "soc/spi_struct.h" // SPI2, SPI3
#include "board/gpio.h" // spi_setup
#include "command.h" // shutdown
#include "compiler.h" // ARRAY_SIZE
#include "internal.h" // KLIPPER_ARCH_MODEM, esp32_pad_config
#include "sched.h" // sched_shutdown
#if !KLIPPER_ARCH_MODEM
#include "driver/gpio.h" // gpio_config
#include "esp_private/periph_ctrl.h" // periph_module_enable
#include "esp_rom_gpio.h" // esp_rom_gpio_connect_out_signal
#define SPI_MODULE(m) .module = m,
#else
// Modem arch: the bare motion core cannot take the IDF driver path;
// pads/matrix are programmed via gpio.c's register helpers and the
// peripheral clocks were enabled from core 0 before this core booted
// (appcpu_boot.c esp32_appcpu_start)
#define SPI_MODULE(m)
#endif

#define SPI_APB_FREQ 80000000

struct spi_bus_info {
    spi_dev_t *spi;
#if !KLIPPER_ARCH_MODEM
    periph_module_t module;
#endif
    uint8_t miso_pin, mosi_pin, sck_pin;
    uint8_t miso_sig, mosi_sig, sck_sig;
};

DECL_ENUMERATION("spi_bus", "spi2", 0);
DECL_CONSTANT_STR("BUS_PINS_spi2", "GPIO12,GPIO13,GPIO14");
DECL_ENUMERATION("spi_bus", "spi3", 1);
DECL_CONSTANT_STR("BUS_PINS_spi3", "GPIO19,GPIO23,GPIO18");

static const struct spi_bus_info spi_bus[] = {
    { .spi = &SPI2, SPI_MODULE(PERIPH_HSPI_MODULE)
      .miso_pin = 12, .mosi_pin = 13, .sck_pin = 14,
      .miso_sig = HSPIQ_IN_IDX, .mosi_sig = HSPID_OUT_IDX,
      .sck_sig = HSPICLK_OUT_IDX },
    { .spi = &SPI3, SPI_MODULE(PERIPH_VSPI_MODULE)
      .miso_pin = 19, .mosi_pin = 23, .sck_pin = 18,
      .miso_sig = VSPIQ_IN_IDX, .mosi_sig = VSPID_OUT_IDX,
      .sck_sig = VSPICLK_OUT_IDX },
};

// Route a pad to a peripheral output signal through the GPIO matrix
static void
spi_pin_out(uint8_t pin, uint8_t signal)
{
#if KLIPPER_ARCH_MODEM
    esp32_pad_config(pin, 0, 1, 0, 0);
    // Must come after the pad config (which reclaims the pad for the
    // plain GPIO output signal)
    esp32_matrix_out(pin, signal);
#else
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&config))
        shutdown("spi pin config failed");
    // Must come after gpio_config (which reclaims the pad for the
    // plain GPIO output signal)
    esp_rom_gpio_connect_out_signal(pin, signal, false, false);
#endif
}

static void
spi_pin_in(uint8_t pin, uint8_t signal)
{
#if KLIPPER_ARCH_MODEM
    esp32_pad_config(pin, 1, 0, 0, 0);
    esp32_matrix_in(pin, signal);
#else
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&config))
        shutdown("spi pin config failed");
    esp_rom_gpio_connect_in_signal(pin, signal, false);
#endif
}

struct spi_config
spi_setup(uint32_t bus, uint8_t mode, uint32_t rate)
{
    if (bus >= ARRAY_SIZE(spi_bus))
        shutdown("Invalid spi bus");
    const struct spi_bus_info *sb = &spi_bus[bus];

    static uint8_t bus_init[ARRAY_SIZE(spi_bus)];
    if (!bus_init[bus]) {
        bus_init[bus] = 1;
#if !KLIPPER_ARCH_MODEM
        periph_module_enable(sb->module);
#endif
        spi_pin_out(sb->sck_pin, sb->sck_sig);
        spi_pin_out(sb->mosi_pin, sb->mosi_sig);
        spi_pin_in(sb->miso_pin, sb->miso_sig);
    }

    // Clock: fspi = 80MHz / (pre * n) with n=2 (h=1, l=2 - 50% duty).
    // pre is rounded up so the resulting rate never exceeds the
    // requested one; reachable range 4.9kHz..40MHz, capped at 20MHz
    // for GPIO-matrix-routed MISO timing.
    if (rate > SPI_APB_FREQ / 4)
        rate = SPI_APB_FREQ / 4; // 20MHz cap
    if (!rate)
        rate = 1;
    uint32_t pre = (SPI_APB_FREQ / 2 + rate - 1) / rate;
    if (pre > 8192)
        pre = 8192;
    // Register value: clk_equ_sysclk=0, clkdiv_pre=pre-1, clkcnt_n=1
    // (n=2), clkcnt_h=0 (h=1), clkcnt_l=1 (l=2)
    typeof(sb->spi->clock) clkreg = { .val = 0 };
    clkreg.clk_equ_sysclk = 0;
    clkreg.clkdiv_pre = pre - 1;
    clkreg.clkcnt_n = 1;
    clkreg.clkcnt_h = 0;
    clkreg.clkcnt_l = 1;

    return (struct spi_config)
        { .spi = (void *)sb->spi, .clock = clkreg.val, .mode = mode };
}

void
spi_prepare(struct spi_config config)
{
    spi_dev_t *spi = config.spi;
    spi->clock.val = config.clock;

    // Full-duplex MOSI+MISO, no command/address/dummy phases
    typeof(spi->user) user = { .val = 0 };
    user.doutdin = 1;
    user.usr_mosi = 1;
    user.usr_miso = 1;
    // CPHA (see hal/esp32 spi_ll master_set_mode: modes 1 and 2 shift
    // on the trailing clock edge)
    user.ck_out_edge = (config.mode == 1 || config.mode == 2);
    spi->user.val = user.val;

    // CPOL; hardware CS lines unused (klipper drives CS as a gpio)
    typeof(spi->pin) pin = { .val = 0 };
    pin.ck_idle_edge = (config.mode == 2 || config.mode == 3);
    pin.cs0_dis = 1;
    pin.cs1_dis = 1;
    pin.cs2_dis = 1;
    spi->pin.val = pin.val;

    // MSB-first in both directions
    spi->ctrl.wr_bit_order = 0;
    spi->ctrl.rd_bit_order = 0;
    spi->slave.val = 0;
}

void
spi_transfer(struct spi_config config, uint8_t receive_data
             , uint8_t len, uint8_t *data)
{
    spi_dev_t *spi = config.spi;
    while (len) {
        // The W0..W15 buffer moves up to 64 bytes per hardware
        // transaction; bytes are shifted out W0 low byte first.
        uint8_t chunk = len > 64 ? 64 : len, words = (chunk + 3) / 4;
        for (uint8_t w = 0; w < words; w++) {
            uint32_t v = 0;
            for (uint8_t b = 0; b < 4; b++) {
                uint8_t i = w * 4 + b;
                if (i < chunk)
                    v |= (uint32_t)data[i] << (b * 8);
            }
            spi->data_buf[w] = v;
        }
        spi->mosi_dlen.usr_mosi_dbitlen = chunk * 8 - 1;
        spi->miso_dlen.usr_miso_dbitlen = chunk * 8 - 1;
        spi->cmd.usr = 1;
        while (spi->cmd.usr)
            ;
        if (receive_data) {
            for (uint8_t w = 0; w < words; w++) {
                uint32_t v = spi->data_buf[w];
                for (uint8_t b = 0; b < 4; b++) {
                    uint8_t i = w * 4 + b;
                    if (i < chunk)
                        data[i] = v >> (b * 8);
                }
            }
        }
        data += chunk;
        len -= chunk;
    }
}
