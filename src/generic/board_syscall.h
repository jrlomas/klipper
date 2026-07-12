#ifndef __GENERIC_BOARD_SYSCALL_H
#define __GENERIC_BOARD_SYSCALL_H

#include <stdint.h>
#include "board/gpio.h" // struct gpio_out

// Unified cross-family board syscall surface (FD-0001 doc 13).
//
// Every Klipper port already implements the same board.h primitives
// (GPIO, timer, ADC, PWM, SPI, I2C) plus the shared scheduler and irq
// helpers; this header gathers them into ONE versioned function-pointer
// table so a module is written once against the table instead of against
// each family. It is the substrate the "author a module on the desktop
// and push it to the firmware without a rebuild" idea needs -- a stable,
// versioned call surface every board exposes identically. It is
// deliberately NOT a bytecode virtual machine: the VM was dropped as
// low-value; only the unifying API is kept.
//
// The table wraps the existing board primitives with no behavior change,
// so it is purely additive. Optional capabilities (ADC/PWM/SPI/I2C) are
// present only when the board builds them; `caps` advertises which, and
// their table slots are null otherwise. Check `caps` (or a slot for null)
// before calling an optional syscall.

// Semantic version of the table layout below. Bump the major (high 16
// bits) on any incompatible change to the struct; the minor on additive
// growth (new slots appended at the end).
#define BOARD_SYSCALL_ABI_VERSION 0x00010000u  // 1.0

enum {
    BSC_CAP_GPIO  = 1u << 0,
    BSC_CAP_ADC   = 1u << 1,
    BSC_CAP_PWM   = 1u << 2,
    BSC_CAP_SPI   = 1u << 3,
    BSC_CAP_I2C   = 1u << 4,
    BSC_CAP_TIMER = 1u << 5,
    BSC_CAP_SCHED = 1u << 6,
    BSC_CAP_IRQ   = 1u << 7,
};

struct timer;
struct task_wake;

struct board_syscalls {
    uint32_t abi_version;
    uint32_t caps;

    // --- GPIO (always present) ---
    struct gpio_out (*gpio_out_setup)(uint8_t pin, uint8_t val);
    void (*gpio_out_write)(struct gpio_out g, uint8_t val);
    void (*gpio_out_toggle)(struct gpio_out g);
    struct gpio_in (*gpio_in_setup)(uint8_t pin, int8_t pull_up);
    uint8_t (*gpio_in_read)(struct gpio_in g);

    // --- ADC (BSC_CAP_ADC) ---
    struct gpio_adc (*adc_setup)(uint8_t pin);
    uint32_t (*adc_sample)(struct gpio_adc g);
    uint16_t (*adc_read)(struct gpio_adc g);

    // --- PWM (BSC_CAP_PWM) ---
    struct gpio_pwm (*pwm_setup)(uint8_t pin, uint32_t cycle_time, uint8_t val);
    void (*pwm_write)(struct gpio_pwm g, uint8_t val);

    // --- SPI (BSC_CAP_SPI) ---
    struct spi_config (*spi_setup)(uint32_t bus, uint8_t mode, uint32_t rate);
    void (*spi_transfer)(struct spi_config c, uint8_t receive_data,
                         uint8_t len, uint8_t *data);

    // --- I2C (BSC_CAP_I2C) ---
    struct i2c_config (*i2c_setup)(uint32_t bus, uint32_t rate, uint8_t addr);
    int (*i2c_write)(struct i2c_config c, uint8_t write_len, uint8_t *data);
    int (*i2c_read)(struct i2c_config c, uint8_t reg_len, uint8_t *reg,
                    uint8_t read_len, uint8_t *read);

    // --- Timer (always present) ---
    uint32_t (*timer_read_time)(void);
    uint32_t (*timer_from_us)(uint32_t us);
    uint8_t (*timer_is_before)(uint32_t time1, uint32_t time2);

    // --- Scheduler (always present) ---
    void (*sched_add_timer)(struct timer *t);
    void (*sched_del_timer)(struct timer *t);
    void (*sched_wake_task)(struct task_wake *w);
    void (*shutdown)(uint_fast8_t reason);

    // --- IRQ (always present) ---
    void (*irq_disable)(void);
    void (*irq_enable)(void);
};

// The singleton table for this board. Never null.
const struct board_syscalls *board_syscalls(void);
// Convenience: the capability bitmap (== board_syscalls()->caps).
uint32_t board_syscall_caps(void);

#endif // board_syscall.h
