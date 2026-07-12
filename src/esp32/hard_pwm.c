// Hardware PWM via the ESP32 LEDC peripheral
//
// Split binding, matching the port's ADC/GPIO pattern: configuration
// (gpio_pwm_setup, task context only) goes through the IDF ledc
// driver, which owns the nontrivial clock-divider bookkeeping; the
// runtime duty update (gpio_pwm_write) is called from klipper timer
// dispatch (pwmcmds' pwm_event timer) where the IDF driver's
// spinlocks and flash-resident code are off limits, so it is a
// direct two-register write (LEDC duty + duty_start latch).
//
// cycle_time -> LEDC mapping: klipper requests a cycle time in
// CONFIG_CLOCK_FREQ (20MHz) ticks; LEDC high-speed timers divide the
// 80MHz APB clock by div*2^res where res is the duty resolution in
// bits.  The frequency is freq = 20MHz/cycle_ticks and res is chosen
// as the largest value (<=15 bits) the divider can realize, i.e.
// roughly res = log2(80MHz/freq).  Constraints: achievable cycle
// times span ~50ns*2 (res>=1) up to ~13s (20-bit divider integer
// part * 2^15 gives well past any klipper use); resolution shrinks
// as frequency rises (e.g. 20kHz -> 12 bits, 1MHz -> 6 bits), and
// duty values (0..32768 = PWM_MAX) are truncated to that resolution.
//
// Allocation: 8 high-speed channels, one per PWM pin; the 4
// high-speed timers are shared between channels with equal
// cycle_time.  A pin that is configured again (config reset without
// a chip reset) reuses its channel; timers freed that way are only
// reclaimed when no other channel shares them.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "driver/ledc.h" // ledc_timer_config
#include "soc/ledc_struct.h" // LEDC
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/gpio.h" // gpio_pwm_setup
#include "command.h" // shutdown
#include "compiler.h" // ARRAY_SIZE
#include "internal.h" // ESP32_GPIO_COUNT
#include "sched.h" // sched_shutdown

#define MAX_PWM 32768
DECL_CONSTANT("PWM_MAX", MAX_PWM);

#define PWM_APB_FREQ 80000000
#define NUM_CHAN 8
#define NUM_TIMER 4

static struct {
    int16_t pin; // -1 = free
    uint8_t timer;
} pwm_chan[NUM_CHAN] = {
    [0 ... NUM_CHAN-1] = { .pin = -1 },
};
static struct {
    uint32_t cycle_ticks; // 0 = free
    uint8_t users, res;
} pwm_timer[NUM_TIMER];

struct gpio_pwm
gpio_pwm_setup(uint32_t pin, uint32_t cycle_time, uint16_t val)
{
    if (pin >= 34) // 34..39 are input only
        shutdown("Not a valid PWM pin");
    if (!cycle_time)
        cycle_time = 1;
    uint32_t freq = CONFIG_CLOCK_FREQ / cycle_time;
    if (!freq)
        freq = 1;

    // Reuse the channel if this pin was configured before
    int chan = -1;
    for (int c = 0; c < NUM_CHAN; c++) {
        if (pwm_chan[c].pin == (int16_t)pin) {
            chan = c;
            if (--pwm_timer[pwm_chan[c].timer].users == 0)
                pwm_timer[pwm_chan[c].timer].cycle_ticks = 0;
            break;
        }
        if (chan < 0 && pwm_chan[c].pin < 0)
            chan = c;
    }
    if (chan < 0)
        shutdown("No free LEDC pwm channels");

    // Find (or configure) a high-speed timer for this cycle_time
    int timer = -1;
    for (int t = 0; t < NUM_TIMER; t++) {
        if (pwm_timer[t].cycle_ticks == cycle_time) {
            timer = t;
            break;
        }
        if (timer < 0 && !pwm_timer[t].cycle_ticks)
            timer = t;
    }
    if (timer < 0)
        shutdown("No free LEDC pwm timers (cycle_time conflict)");
    if (pwm_timer[timer].cycle_ticks != cycle_time) {
        uint32_t res = ledc_find_suitable_duty_resolution(PWM_APB_FREQ, freq);
        if (!res)
            shutdown("PWM cycle_time out of range");
        if (res > 15)
            res = 15; // keep the PWM_MAX -> duty shift non-negative
        ledc_timer_config_t tcfg = {
            .speed_mode = LEDC_HIGH_SPEED_MODE,
            .duty_resolution = res,
            .timer_num = timer,
            .freq_hz = freq,
            .clk_cfg = LEDC_USE_APB_CLK,
        };
        if (ledc_timer_config(&tcfg))
            shutdown("PWM timer config failed");
        pwm_timer[timer].cycle_ticks = cycle_time;
        pwm_timer[timer].res = res;
    }
    pwm_timer[timer].users++;
    pwm_chan[chan].pin = pin;
    pwm_chan[chan].timer = timer;

    struct gpio_pwm g = { .chan = chan, .shift = 15 - pwm_timer[timer].res };
    ledc_channel_config_t ccfg = {
        .gpio_num = pin,
        .speed_mode = LEDC_HIGH_SPEED_MODE,
        .channel = chan,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = timer,
        .duty = (uint32_t)val >> g.shift,
        .hpoint = 0,
    };
    if (ledc_channel_config(&ccfg))
        shutdown("PWM channel config failed");
    return g;
}

void
gpio_pwm_write(struct gpio_pwm g, uint16_t val)
{
    // val is 0..MAX_PWM (32768); duty 2^res means constant high.
    // Direct register write (ISR safe): high-speed channels latch
    // the new duty on the duty_start strobe.
    uint32_t duty = (uint32_t)val >> g.shift;
    LEDC.channel_group[0].channel[g.chan].duty.duty = duty << 4;
    LEDC.channel_group[0].channel[g.chan].conf1.duty_start = 1;
}
