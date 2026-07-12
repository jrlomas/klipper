// Unified cross-family board syscall table (FD-0001 doc 13).
//
// One portable implementation serves every port: it wraps each port's
// existing board.h primitives, so STM32, ESP32, and the rest expose an
// identical, versioned call surface with no per-family code. Optional
// capabilities are compiled in only when the board provides them, so the
// table always links.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_HAVE_GPIO_ADC
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "board_syscall.h" // board_syscalls
#include "command.h" // DECL_COMMAND
#include "sched.h" // sched_add_timer

// Fold each optional capability's bit to 0 when the board lacks it, so
// the caps value is a plain integer constant expression (usable both in
// the table initializer and the query reply).
#if CONFIG_HAVE_GPIO_ADC
#define BSC_BIT_ADC BSC_CAP_ADC
#else
#define BSC_BIT_ADC 0
#endif
#if CONFIG_HAVE_GPIO_HARD_PWM
#define BSC_BIT_PWM BSC_CAP_PWM
#else
#define BSC_BIT_PWM 0
#endif
#if CONFIG_HAVE_GPIO_SPI
#define BSC_BIT_SPI BSC_CAP_SPI
#else
#define BSC_BIT_SPI 0
#endif
#if CONFIG_HAVE_GPIO_I2C
#define BSC_BIT_I2C BSC_CAP_I2C
#else
#define BSC_BIT_I2C 0
#endif

#define BSC_CAPS (BSC_CAP_GPIO | BSC_CAP_TIMER | BSC_CAP_SCHED | BSC_CAP_IRQ \
                  | BSC_BIT_ADC | BSC_BIT_PWM | BSC_BIT_SPI | BSC_BIT_I2C)

static const struct board_syscalls the_syscalls = {
    .abi_version = BOARD_SYSCALL_ABI_VERSION,
    .caps = BSC_CAPS,

    .gpio_out_setup = gpio_out_setup,
    .gpio_out_write = gpio_out_write,
    .gpio_out_toggle = gpio_out_toggle,
    .gpio_in_setup = gpio_in_setup,
    .gpio_in_read = gpio_in_read,

#if CONFIG_HAVE_GPIO_ADC
    .adc_setup = gpio_adc_setup,
    .adc_sample = gpio_adc_sample,
    .adc_read = gpio_adc_read,
#endif
#if CONFIG_HAVE_GPIO_HARD_PWM
    .pwm_setup = gpio_pwm_setup,
    .pwm_write = gpio_pwm_write,
#endif
#if CONFIG_HAVE_GPIO_SPI
    .spi_setup = spi_setup,
    .spi_transfer = spi_transfer,
#endif
#if CONFIG_HAVE_GPIO_I2C
    .i2c_setup = i2c_setup,
    .i2c_write = i2c_write,
    .i2c_read = i2c_read,
#endif

    .timer_read_time = timer_read_time,
    .timer_from_us = timer_from_us,
    .timer_is_before = timer_is_before,

    .sched_add_timer = sched_add_timer,
    .sched_del_timer = sched_del_timer,
    .sched_wake_task = sched_wake_task,
    .shutdown = sched_shutdown,

    .irq_disable = irq_disable,
    .irq_enable = irq_enable,
};

const struct board_syscalls *
board_syscalls(void)
{
    return &the_syscalls;
}

uint32_t
board_syscall_caps(void)
{
    return the_syscalls.caps;
}

// Host capability negotiation: report the syscall ABI version and the
// capability bitmap. Reading the table here also keeps it linked.
void
command_query_board_syscalls(uint32_t *args)
{
    (void)args;
    const struct board_syscalls *bs = board_syscalls();
    sendf("board_syscalls_state abi=%u caps=%u", bs->abi_version, bs->caps);
}
DECL_COMMAND_FLAGS(command_query_board_syscalls, HF_IN_SHUTDOWN,
                   "query_board_syscalls");

// Also advertise the ABI in the static dictionary so a host can see the
// surface without a round-trip.
DECL_CONSTANT("BOARD_SYSCALL_ABI", BOARD_SYSCALL_ABI_VERSION);
DECL_CONSTANT("BOARD_SYSCALL_CAPS", BSC_CAPS);
