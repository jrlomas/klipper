// I2C master (I2C0) on ESP32
//
// Register-level driver against the Apache-2.0 soc headers (RFC 0001
// doc 12's preference; the I2C controller is fully documented in the
// TRM).  The IDF alternative (the i2c_master driver) is built on
// interrupts plus FreeRTOS primitives and does not expose the
// distinct NACK-versus-timeout results klipper's i2ccmds error
// contract wants (I2C_BUS_START_NACK vs I2C_BUS_NACK vs
// I2C_BUS_TIMEOUT, matching stm32/i2c.c semantics).
//
// The ESP32 I2C engine executes a small command list (RSTART / WRITE
// / READ / STOP / END) against a 32-byte FIFO.  Each klipper
// transaction is split into segments: the address byte is sent in
// its own WRITE+END segment so an address NACK is distinguishable
// from a data NACK, then data moves in FIFO-sized chunks with END
// between chunks and STOP on the last (the TRM's documented >32 byte
// flow).  All waits are busy-polls on the raw interrupt bits bounded
// by klipper's clock, so a wedged bus becomes I2C_BUS_TIMEOUT (which
// i2ccmds turns into shutdown); after any error the controller is
// reset via periph_module_reset and reprogrammed.
//
// Everything here runs from task context (klipper command handlers
// and sensor tasks); nothing is called from timer dispatch.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "driver/gpio.h" // gpio_config
#include "esp_private/periph_ctrl.h" // periph_module_enable
#include "esp_rom_gpio.h" // esp_rom_gpio_connect_out_signal
#include "soc/gpio_sig_map.h" // I2CEXT0_SCL_OUT_IDX
#include "soc/i2c_struct.h" // I2C0
#include "board/gpio.h" // i2c_setup
#include "board/misc.h" // timer_read_time
#include "command.h" // shutdown
#include "compiler.h" // ARRAY_SIZE
#include "i2ccmds.h" // I2C_BUS_SUCCESS
#include "sched.h" // sched_shutdown

#define I2C_APB_FREQ 80000000
#define I2C_FIFO_SIZE 32
// Data bytes moved per hardware segment (a spare FIFO slot keeps the
// address-byte round trivially in bounds too)
#define I2C_CHUNK 30

// Command list opcodes (TRM: I2C_COMMAND registers)
enum {
    I2C_CMD_RSTART = 0, I2C_CMD_WRITE = 1, I2C_CMD_READ = 2,
    I2C_CMD_STOP = 3, I2C_CMD_END = 4,
};

DECL_ENUMERATION("i2c_bus", "i2c0", 0);
DECL_CONSTANT_STR("BUS_PINS_i2c0", "GPIO22,GPIO21");

#define I2C0_SCL_PIN 22
#define I2C0_SDA_PIN 21

// ESP32 silicon quirk: writes to the I2C TX FIFO must go through the
// APB mirror of the FIFO register, not the DR_REG_I2C_EXT_BASE
// (DPORT) mapping the I2C0 struct lives at - see IDF's
// hal/esp32/include/hal/i2c_ll.h i2c_ll_write_txfifo (reads of the
// RX FIFO use the normal mapping)
#define I2C0_TXFIFO_APB (*(volatile uint32_t *)0x6001301c)

static uint32_t i2c_rate_hz; // programmed bus rate (for reprogramming)

// Program controller mode and SCL timing (formulas match IDF's
// hal/esp32 i2c_ll with the input filters disabled)
static void
i2c_program(void)
{
    i2c_dev_t *i2c = &I2C0;
    typeof(i2c->ctr) ctr = { .val = 0 };
    ctr.ms_mode = 1;
    ctr.sda_force_out = 1;
    ctr.scl_force_out = 1;
    i2c->ctr.val = ctr.val;
    i2c->fifo_conf.nonfifo_en = 0;
    i2c->scl_filter_cfg.en = 0;
    i2c->sda_filter_cfg.en = 0;

    uint32_t half = I2C_APB_FREQ / i2c_rate_hz / 2;
    i2c->scl_low_period.period = half - 1;
    i2c->scl_high_period.period = half - 7;
    i2c->sda_hold.time = half / 2;
    i2c->sda_sample.time = half / 2;
    i2c->scl_rstart_setup.time = half;
    i2c->scl_stop_setup.time = half;
    i2c->scl_start_hold.time = half;
    i2c->scl_stop_hold.time = half;
    i2c->timeout.tout = half * 20; // SCL-stuck timeout: 10 bit times
}

// Full controller reset - releases a wedged bus state machine
static void
i2c_hw_reset(void)
{
    periph_module_reset(PERIPH_I2C0_MODULE);
    i2c_program();
}

static void
i2c_pin_setup(uint8_t pin, uint32_t sig_idx)
{
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT_OUTPUT_OD,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&config))
        shutdown("i2c pin config failed");
    // Open-drain: the peripheral drives and samples the same pad
    esp_rom_gpio_connect_out_signal(pin, sig_idx, false, false);
    esp_rom_gpio_connect_in_signal(pin, sig_idx, false);
}

struct i2c_config
i2c_setup(uint32_t bus, uint32_t rate, uint8_t addr)
{
    if (bus)
        shutdown("Unsupported i2c bus");
    if (rate < 25000)
        rate = 25000;
    if (rate > 1000000)
        rate = 1000000;

    static uint8_t init;
    if (!init) {
        init = 1;
        periph_module_enable(PERIPH_I2C0_MODULE);
        i2c_pin_setup(I2C0_SCL_PIN, I2CEXT0_SCL_OUT_IDX);
        i2c_pin_setup(I2C0_SDA_PIN, I2CEXT0_SDA_OUT_IDX);
    }
    i2c_rate_hz = rate;
    i2c_program();

    return (struct i2c_config){ .addr = addr << 1 };
}

static void
i2c_cmd(int idx, int op, int num, int ack_en, int ack_val)
{
    I2C0.command[idx].val = (num & 0xff) | (ack_en << 8) | (ack_val << 10)
        | (op << 11);
}

static void
i2c_fifo_reset(void)
{
    I2C0.fifo_conf.tx_fifo_rst = 1;
    I2C0.fifo_conf.tx_fifo_rst = 0;
    I2C0.fifo_conf.rx_fifo_rst = 1;
    I2C0.fifo_conf.rx_fifo_rst = 0;
}

// Start the programmed command list and wait for it to reach the END
// (wait_end) or STOP marker.  Returns an I2C_BUS_* code.
static int
i2c_run(int wait_end, uint32_t timeout)
{
    i2c_dev_t *i2c = &I2C0;
    i2c->int_clr.val = ~0;
    i2c->ctr.trans_start = 1;
    for (;;) {
        typeof(i2c->int_raw) raw;
        raw.val = i2c->int_raw.val;
        if (raw.ack_err)
            return I2C_BUS_NACK;
        if (raw.arbitration_lost || raw.time_out)
            return I2C_BUS_TIMEOUT;
        if (wait_end ? raw.end_detect : raw.trans_complete)
            return I2C_BUS_SUCCESS;
        if (!timer_is_before(timer_read_time(), timeout))
            return I2C_BUS_TIMEOUT;
    }
}

// Send start condition plus address byte as its own END-terminated
// segment (so an address NACK is distinguishable from a data NACK)
static int
i2c_start(uint8_t addr_byte, uint32_t timeout)
{
    i2c_fifo_reset();
    i2c_cmd(0, I2C_CMD_RSTART, 0, 0, 0);
    i2c_cmd(1, I2C_CMD_WRITE, 1, 1, 0);
    i2c_cmd(2, I2C_CMD_END, 0, 0, 0);
    I2C0_TXFIFO_APB = addr_byte;
    return i2c_run(1, timeout);
}

// Send the stop condition after a failed mid-transaction segment
static void
i2c_send_stop(uint32_t timeout)
{
    i2c_cmd(0, I2C_CMD_STOP, 0, 0, 0);
    i2c_run(0, timeout);
}

int
i2c_write(struct i2c_config config, uint8_t write_len, uint8_t *write)
{
    uint32_t timeout = timer_read_time()
        + timer_from_us(5000 + 250 * (uint32_t)write_len);

    int ret = i2c_start(config.addr, timeout);
    if (ret != I2C_BUS_SUCCESS) {
        ret = ret == I2C_BUS_NACK ? I2C_BUS_START_NACK : ret;
        goto fail;
    }
    for (;;) {
        uint8_t chunk = write_len > I2C_CHUNK ? I2C_CHUNK : write_len;
        uint8_t last = chunk == write_len;
        int idx = 0;
        if (chunk)
            i2c_cmd(idx++, I2C_CMD_WRITE, chunk, 1, 0);
        i2c_cmd(idx, last ? I2C_CMD_STOP : I2C_CMD_END, 0, 0, 0);
        for (uint8_t i = 0; i < chunk; i++)
            I2C0_TXFIFO_APB = *write++;
        ret = i2c_run(!last, timeout);
        if (ret != I2C_BUS_SUCCESS)
            goto fail;
        write_len -= chunk;
        if (last)
            return I2C_BUS_SUCCESS;
    }
fail:
    // Best-effort bus release, then controller reset (the ESP32 I2C
    // FSM is not reliably recoverable in place after an error)
    i2c_send_stop(timeout);
    i2c_hw_reset();
    return ret;
}

int
i2c_read(struct i2c_config config, uint8_t reg_len, uint8_t *reg
         , uint8_t read_len, uint8_t *read)
{
    uint32_t timeout = timer_read_time()
        + timer_from_us(5000 + 250 * ((uint32_t)reg_len + read_len));
    int ret;

    if (reg_len) {
        // Write the register/address prefix
        ret = i2c_start(config.addr, timeout);
        if (ret != I2C_BUS_SUCCESS) {
            ret = ret == I2C_BUS_NACK ? I2C_BUS_START_NACK : ret;
            goto fail;
        }
        while (reg_len) {
            uint8_t chunk = reg_len > I2C_CHUNK ? I2C_CHUNK : reg_len;
            i2c_cmd(0, I2C_CMD_WRITE, chunk, 1, 0);
            i2c_cmd(1, I2C_CMD_END, 0, 0, 0);
            for (uint8_t i = 0; i < chunk; i++)
                I2C0_TXFIFO_APB = *reg++;
            ret = i2c_run(1, timeout);
            if (ret != I2C_BUS_SUCCESS)
                goto fail;
            reg_len -= chunk;
        }
    }
    // Repeated start with the read address
    ret = i2c_start(config.addr | 0x01, timeout);
    if (ret != I2C_BUS_SUCCESS) {
        ret = ret == I2C_BUS_NACK ? I2C_BUS_START_READ_NACK : ret;
        goto fail;
    }
    for (;;) {
        uint8_t chunk = read_len > I2C_CHUNK ? I2C_CHUNK : read_len;
        uint8_t last = chunk == read_len;
        int idx = 0;
        if (last && chunk) {
            // NACK the final byte to end the device's transmission
            if (chunk > 1)
                i2c_cmd(idx++, I2C_CMD_READ, chunk - 1, 0, 0);
            i2c_cmd(idx++, I2C_CMD_READ, 1, 0, 1);
        } else if (chunk) {
            i2c_cmd(idx++, I2C_CMD_READ, chunk, 0, 0);
        }
        i2c_cmd(idx, last ? I2C_CMD_STOP : I2C_CMD_END, 0, 0, 0);
        ret = i2c_run(!last, timeout);
        if (ret != I2C_BUS_SUCCESS)
            goto fail;
        for (uint8_t i = 0; i < chunk; i++)
            *read++ = I2C0.fifo_data.val & 0xff;
        read_len -= chunk;
        if (last)
            return I2C_BUS_SUCCESS;
    }
fail:
    i2c_send_stop(timeout);
    i2c_hw_reset();
    return ret;
}
