// GPIO functions on ESP32
//
// The speed-critical set/clear/read paths used from timer dispatch
// are direct register writes through the cached GPIO.out_w1ts/
// out_w1tc (out1_* bank for pins 32..39) addresses in both
// architectures - single-instruction and safe from ISRs.
//
// Pad configuration differs by architecture (FD-0001 doc 12):
//  * component: the IDF gpio driver (task context only)
//  * modem: register level against the vendored soc headers
//    (lib/esp32) - the IDF driver takes FreeRTOS locks that do not
//    exist on the bare motion core.  The sequence mirrors IDF's own
//    hal/esp32/include/hal/gpio_ll.h (v5.3.2): IO_MUX MCU_SEL to the
//    plain-GPIO function, FUN_IE/FUN_PU/FUN_PD, GPIO matrix
//    func_out_sel to the "simple GPIO out" signal (SIG_GPIO_OUT_IDX,
//    256), enable_w1ts/w1tc for the output driver, pin[].pad_driver
//    for open drain.  Pull resistors of the RTC-capable pads live in
//    RTC_IO registers instead of the IO_MUX (TRM 4.10; table data
//    from IDF components/soc/esp32/rtc_io_periph.c).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "soc/gpio_sig_map.h" // SIG_GPIO_OUT_IDX
#include "soc/gpio_struct.h" // GPIO
#include "soc/io_mux_reg.h" // IO_MUX_GPIO0_REG, FUN_IE, FUN_PU, FUN_PD
#include "soc/rtc_io_reg.h" // RTC_IO_TOUCH_PAD0_REG, *_RUE_M, *_RDE_M
#include "soc/soc.h" // REG_SET_FIELD
#include "autoconf.h" // CONFIG_WANT_ESP32_I2S_SHIFT
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_save
#include "command.h" // shutdown
#include "internal.h" // ESP32_GPIO_COUNT, KLIPPER_ARCH_MODEM
#include "i2s_shift.h" // ESP32_I2S_OUT_BASE
#include "sched.h" // sched_shutdown
#if !KLIPPER_ARCH_MODEM
#include "driver/gpio.h" // gpio_config
#endif

DECL_ENUMERATION_RANGE("pin", "GPIO0", 0, ESP32_GPIO_COUNT);
#if CONFIG_WANT_ESP32_I2S_SHIFT
DECL_ENUMERATION_RANGE("pin", "I2SO0", ESP32_I2S_OUT_BASE,
                       ESP32_I2S_OUT_COUNT);
#endif

// GPIO34..39 are input only on the ESP32
#define GPIO_FIRST_INPUT_ONLY 34


/****************************************************************
 * Register-level pad configuration (used by the modem arch; the
 * matrix helpers also serve spi.c/i2c.c/hard_pwm.c)
 ****************************************************************/

// IO_MUX configuration register for each GPIO (0 = no such pad).
// Data from IDF components/soc/esp32/gpio_periph.c GPIO_PIN_MUX_REG[]
// (the pad names are historical; io_mux_reg.h carries the aliases).
static const uint32_t pin_mux_reg[ESP32_GPIO_COUNT] = {
    IO_MUX_GPIO0_REG, IO_MUX_GPIO1_REG, IO_MUX_GPIO2_REG, IO_MUX_GPIO3_REG,
    IO_MUX_GPIO4_REG, IO_MUX_GPIO5_REG, IO_MUX_GPIO6_REG, IO_MUX_GPIO7_REG,
    IO_MUX_GPIO8_REG, IO_MUX_GPIO9_REG, IO_MUX_GPIO10_REG, IO_MUX_GPIO11_REG,
    IO_MUX_GPIO12_REG, IO_MUX_GPIO13_REG, IO_MUX_GPIO14_REG,
    IO_MUX_GPIO15_REG, IO_MUX_GPIO16_REG, IO_MUX_GPIO17_REG,
    IO_MUX_GPIO18_REG, IO_MUX_GPIO19_REG, IO_MUX_GPIO20_REG,
    IO_MUX_GPIO21_REG, IO_MUX_GPIO22_REG, IO_MUX_GPIO23_REG,
    0 /* no GPIO24 pad */, IO_MUX_GPIO25_REG, IO_MUX_GPIO26_REG,
    IO_MUX_GPIO27_REG, 0, 0, 0, 0,
    IO_MUX_GPIO32_REG, IO_MUX_GPIO33_REG, IO_MUX_GPIO34_REG,
    IO_MUX_GPIO35_REG, IO_MUX_GPIO36_REG, IO_MUX_GPIO37_REG,
    IO_MUX_GPIO38_REG, IO_MUX_GPIO39_REG,
};

// Pull-up/pull-down control of the RTC-capable pads (GPIO 34..39
// have no pull resistors at all; every other RTC pad's pulls are in
// these RTC_IO registers, not in the IO_MUX).  Data from IDF
// components/soc/esp32/rtc_io_periph.c rtc_io_desc[] (v5.3.2).
static const struct rtc_pull_info {
    uint8_t gpio;
    uint32_t reg, rue, rde;
} rtc_pull[] = {
    { 0, RTC_IO_TOUCH_PAD1_REG, RTC_IO_TOUCH_PAD1_RUE_M
      , RTC_IO_TOUCH_PAD1_RDE_M },
    { 2, RTC_IO_TOUCH_PAD2_REG, RTC_IO_TOUCH_PAD2_RUE_M
      , RTC_IO_TOUCH_PAD2_RDE_M },
    { 4, RTC_IO_TOUCH_PAD0_REG, RTC_IO_TOUCH_PAD0_RUE_M
      , RTC_IO_TOUCH_PAD0_RDE_M },
    { 12, RTC_IO_TOUCH_PAD5_REG, RTC_IO_TOUCH_PAD5_RUE_M
      , RTC_IO_TOUCH_PAD5_RDE_M },
    { 13, RTC_IO_TOUCH_PAD4_REG, RTC_IO_TOUCH_PAD4_RUE_M
      , RTC_IO_TOUCH_PAD4_RDE_M },
    { 14, RTC_IO_TOUCH_PAD6_REG, RTC_IO_TOUCH_PAD6_RUE_M
      , RTC_IO_TOUCH_PAD6_RDE_M },
    { 15, RTC_IO_TOUCH_PAD3_REG, RTC_IO_TOUCH_PAD3_RUE_M
      , RTC_IO_TOUCH_PAD3_RDE_M },
    { 25, RTC_IO_PAD_DAC1_REG, RTC_IO_PDAC1_RUE_M, RTC_IO_PDAC1_RDE_M },
    { 26, RTC_IO_PAD_DAC2_REG, RTC_IO_PDAC2_RUE_M, RTC_IO_PDAC2_RDE_M },
    { 27, RTC_IO_TOUCH_PAD7_REG, RTC_IO_TOUCH_PAD7_RUE_M
      , RTC_IO_TOUCH_PAD7_RDE_M },
    { 32, RTC_IO_XTAL_32K_PAD_REG, RTC_IO_X32P_RUE_M, RTC_IO_X32P_RDE_M },
    { 33, RTC_IO_XTAL_32K_PAD_REG, RTC_IO_X32N_RUE_M, RTC_IO_X32N_RDE_M },
};

// Program pull resistors (1 = up, -1 = down, 0 = none).  RTC pads
// are matched in the table above; GPIO 34..39 simply have none.
static void
esp32_pad_pull(uint32_t pin, int pull)
{
    for (uint32_t i = 0; i < sizeof(rtc_pull)/sizeof(rtc_pull[0]); i++) {
        const struct rtc_pull_info *rp = &rtc_pull[i];
        if (rp->gpio != pin)
            continue;
        if (pull > 0)
            REG_SET_BIT(rp->reg, rp->rue);
        else
            REG_CLR_BIT(rp->reg, rp->rue);
        if (pull < 0)
            REG_SET_BIT(rp->reg, rp->rde);
        else
            REG_CLR_BIT(rp->reg, rp->rde);
        return;
    }
    if (pin >= GPIO_FIRST_INPUT_ONLY)
        return; // no pull hardware on 34..39
    uint32_t mux = pin_mux_reg[pin];
    if (pull > 0)
        REG_SET_BIT(mux, FUN_PU);
    else
        REG_CLR_BIT(mux, FUN_PU);
    if (pull < 0)
        REG_SET_BIT(mux, FUN_PD);
    else
        REG_CLR_BIT(mux, FUN_PD);
}

// Register-level equivalent of the IDF gpio_config() subset this
// port uses (gpio_ll.h sequence; task/init context on the mcu core)
void
esp32_pad_config(uint32_t pin, int input_en, int output_en
                 , int open_drain, int pull)
{
    uint32_t mux = pin_mux_reg[pin];
    if (!mux)
        shutdown("Not a valid gpio pad");
    // Pad to the plain-GPIO IO_MUX function
    REG_SET_FIELD(mux, MCU_SEL, PIN_FUNC_GPIO);
    if (input_en)
        REG_SET_BIT(mux, FUN_IE);
    else
        REG_CLR_BIT(mux, FUN_IE);
    esp32_pad_pull(pin, pull);
    GPIO.pin[pin].pad_driver = open_drain ? 1 : 0;
    // Output driver via the matrix "simple GPIO out" signal
    if (output_en) {
        esp32_matrix_out(pin, SIG_GPIO_OUT_IDX);
        if (pin < 32)
            GPIO.enable_w1ts = 1u << pin;
        else
            GPIO.enable1_w1ts.val = 1u << (pin - 32);
    } else {
        if (pin < 32)
            GPIO.enable_w1tc = 1u << pin;
        else
            GPIO.enable1_w1tc.val = 1u << (pin - 32);
    }
}

// Route a pad to a peripheral output signal through the GPIO matrix
// (gpio_ll_func_sel/esp_rom gpio_matrix_out equivalent)
void
esp32_matrix_out(uint32_t pin, uint32_t sig_idx)
{
    GPIO.func_out_sel_cfg[pin].func_sel = sig_idx;
    GPIO.func_out_sel_cfg[pin].inv_sel = 0;
    GPIO.func_out_sel_cfg[pin].oen_sel = 0;
    GPIO.func_out_sel_cfg[pin].oen_inv_sel = 0;
}

// Route a pad to a peripheral input signal through the GPIO matrix
void
esp32_matrix_in(uint32_t pin, uint32_t sig_idx)
{
    GPIO.func_in_sel_cfg[sig_idx].func_sel = pin;
    GPIO.func_in_sel_cfg[sig_idx].sig_in_inv = 0;
    GPIO.func_in_sel_cfg[sig_idx].sig_in_sel = 1; // through the matrix
}


/****************************************************************
 * Output pins
 ****************************************************************/

struct gpio_out
gpio_out_setup(uint32_t pin, uint8_t val)
{
#if CONFIG_WANT_ESP32_I2S_SHIFT
    if (pin >= ESP32_I2S_OUT_BASE
        && pin < ESP32_I2S_OUT_BASE + ESP32_I2S_OUT_COUNT) {
        struct gpio_out g = {
            .pin = pin - ESP32_I2S_OUT_BASE,
            .is_i2s = 1,
        };
        gpio_out_reset(g, val);
        return g;
    }
#endif
    if (pin >= GPIO_FIRST_INPUT_ONLY)
        goto fail;
    struct gpio_out g = { };
    g.pin = pin;
    if (pin < 32) {
        g.bit = 1u << pin;
        g.w1ts = (volatile uint32_t *)&GPIO.out_w1ts;
        g.w1tc = (volatile uint32_t *)&GPIO.out_w1tc;
        g.out = (volatile uint32_t *)&GPIO.out;
    } else {
        g.bit = 1u << (pin - 32);
        g.w1ts = (volatile uint32_t *)&GPIO.out1_w1ts;
        g.w1tc = (volatile uint32_t *)&GPIO.out1_w1tc;
        g.out = (volatile uint32_t *)&GPIO.out1;
    }
    gpio_out_reset(g, val);
    return g;
fail:
    shutdown("Not an output pin");
}

void
gpio_out_reset(struct gpio_out g, uint8_t val)
{
#if CONFIG_WANT_ESP32_I2S_SHIFT
    if (g.is_i2s) {
        i2s_shift_write(g.pin, val);
        return;
    }
#endif
#if KLIPPER_ARCH_MODEM
    irqstatus_t flag = irq_save();
    gpio_out_write(g, val);
    esp32_pad_config(g.pin, 0, 1, 0, 0);
    irq_restore(flag);
#else
    // gpio_config() enters an ESP-IDF/FreeRTOS critical section.  Calling it
    // inside Klipper's irq_save() deadlocks the component architecture on
    // the first native output configuration (observed on Rodent GPIO5).
    // Set the output latch before enabling the pad, then let the IDF own its
    // own locking boundary.
    gpio_out_write(g, val);
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << g.pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    int ret = gpio_config(&config);
    if (ret)
        shutdown("gpio_config failed");
#endif
}

void
gpio_out_write(struct gpio_out g, uint8_t val)
{
#if CONFIG_WANT_ESP32_I2S_SHIFT
    if (g.is_i2s) {
        i2s_shift_write(g.pin, val);
        return;
    }
#endif
    if (val)
        *g.w1ts = g.bit;
    else
        *g.w1tc = g.bit;
}

void
gpio_out_toggle_noirq(struct gpio_out g)
{
#if CONFIG_WANT_ESP32_I2S_SHIFT
    if (g.is_i2s) {
        i2s_shift_toggle(g.pin);
        return;
    }
#endif
    if (*g.out & g.bit)
        *g.w1tc = g.bit;
    else
        *g.w1ts = g.bit;
}

void
gpio_out_toggle(struct gpio_out g)
{
    irqstatus_t flag = irq_save();
    gpio_out_toggle_noirq(g);
    irq_restore(flag);
}


/****************************************************************
 * Input pins
 ****************************************************************/

struct gpio_in
gpio_in_setup(uint32_t pin, int8_t pull_up)
{
    if (pin >= ESP32_GPIO_COUNT)
        goto fail;
    struct gpio_in g;
    g.pin = pin;
    if (pin < 32) {
        g.bit = 1u << pin;
        g.in = (volatile uint32_t *)&GPIO.in;
    } else {
        g.bit = 1u << (pin - 32);
        g.in = (volatile uint32_t *)&GPIO.in1;
    }
    gpio_in_reset(g, pull_up);
    return g;
fail:
    shutdown("Not a valid input pin");
}

void
gpio_in_reset(struct gpio_in g, int8_t pull_up)
{
    // Note: pins 34..39 have no internal pull resistors
#if KLIPPER_ARCH_MODEM
    esp32_pad_config(g.pin, 1, 0, 0, pull_up);
#else
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << g.pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = pull_up > 0 ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .pull_down_en = (pull_up < 0
                         ? GPIO_PULLDOWN_ENABLE : GPIO_PULLDOWN_DISABLE),
        .intr_type = GPIO_INTR_DISABLE,
    };
    int ret = gpio_config(&config);
    if (ret)
        shutdown("gpio_config failed");
#endif
}

uint8_t
gpio_in_read(struct gpio_in g)
{
    return !!(*g.in & g.bit);
}
