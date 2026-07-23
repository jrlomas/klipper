// ESP32 hardware-timed I2S output engine for BIGTREETECH Rodent V1.x.
//
// Rodent routes its four TMC2160 STEP, DIR, and ENABLE groups through two
// chained output registers on BCK GPIO22, DATA GPIO21, and WS/latch GPIO17.
// The output word must therefore be a continuous, hardware-clocked timeline;
// issuing a blocking SPI transaction from each trajectory timer event permits
// WiFi/RTOS interrupt latency to become motor pulse jitter.
//
// This implementation follows FluidNC's proven Rodent architecture: I2S0
// emits one complete output-state sample every two microseconds and a
// level-three FIFO interrupt refills eight future samples at a time.  The
// Helix trajectory solver is advanced against each future sample clock, so
// STEP rise/fall timing is committed to the peripheral FIFO before it is due.
//
// FluidNC i2s_engine.c:
// Copyright (c) 2024 Mitch Bradley, GPLv3.
// Helix adaptation:
// Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h"

#if CONFIG_WANT_ESP32_I2S_SHIFT

#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "esp_attr.h" // IRAM_ATTR
#include "esp_intr_alloc.h" // esp_intr_alloc_intrstatus
#include "esp_private/periph_ctrl.h" // periph_module_enable
#include "hal/i2s_ll.h"
#include "internal.h" // esp32_matrix_out, esp32_pad_config
#include "i2s_shift.h"
#include "sched.h" // DECL_INIT
#include "soc/gpio_sig_map.h"
#include "soc/i2s_struct.h"
#include "soc/interrupts.h"

// Active-low enables for X, Y, Z, and A/E start disabled.  Rodent's status
// LEDs are active-low as well, so hold them high/off until configured.
#define RODENT_I2S_SAFE_STATE ((1u << 0) | (1u << 7) | (1u << 8) \
                              | (1u << 3) | (1u << 11) | (1u << 14) \
                              | (1u << 15))

// FluidNC's Rodent engine uses a 500kHz 16-bit frame: one latched output word
// every 2us.  A configured 4us STEP pulse consequently occupies two samples.
#define I2S_FRAME_US 2
#define I2S_FRAME_TICKS (CONFIG_CLOCK_FREQ / (1000000 / I2S_FRAME_US))
#define I2S_FIFO_LENGTH (I2S_TX_DATA_NUM + 1)
#define I2S_FIFO_THRESHOLD (I2S_FIFO_LENGTH / 4)
#define I2S_FIFO_RELOAD 8

static volatile uint32_t i2s_shadow = RODENT_I2S_SAFE_STATE;
static uint32_t i2s_fill_clock;
static uint32_t i2s_samples;
static uint32_t i2s_refills;
static uint64_t i2s_total_cycles;
static uint32_t i2s_max_cycles;
// Optional commissioning mirror.  This modifies the serialized FIFO word,
// so a scope on an accessible LED pad observes the real post-latch waveform.
// It is disabled during normal operation.
static uint8_t mirror_source_bit = 0xff;
static uint8_t mirror_output_bit = 0xff;
static uint8_t mirror_invert;

// Called from the I2S level-three ISR.  It advances all trajectory steppers
// whose STEP output belongs to this serialized bus up to the latch time of the
// sample that is about to be queued.
void traj_stepper_i2s_advance(uint32_t sample_clock);

// Optional output-timeline monitor.  Unlike the old per-edge monitor, this
// observes the exact sample clock committed to I2S, not ISR entry/exit time.
static uint8_t monitor_step_bit = 0xff;
static uint8_t monitor_dir_bit = 0xff;
static uint8_t monitor_have_rise;
static uint8_t monitor_have_dir;
static uint8_t monitor_dir_value;
static uint8_t monitor_step_value;
static uint32_t monitor_last_rise;
static uint32_t monitor_step_rises;
static uint32_t monitor_dir_changes;
static uint32_t monitor_interval_count;
static uint32_t monitor_interval_min;
static uint32_t monitor_interval_max;
static uint64_t monitor_interval_total;
static uint32_t monitor_high_count;
static uint32_t monitor_high_min;
static uint32_t monitor_high_max;
static uint64_t monitor_high_total;

static inline uint32_t IRAM_ATTR
read_ccount(void)
{
    uint32_t value;
    __asm__ __volatile__("rsr.ccount %0" : "=a"(value));
    return value;
}

static void
i2s_monitor_reset(void)
{
    monitor_have_rise = 0;
    monitor_have_dir = 0;
    monitor_dir_value = 0;
    monitor_step_value = monitor_step_bit < ESP32_I2S_OUT_COUNT
        ? !!(i2s_shadow & (1u << monitor_step_bit)) : 0;
    monitor_last_rise = 0;
    monitor_step_rises = 0;
    monitor_dir_changes = 0;
    monitor_interval_count = 0;
    monitor_interval_min = UINT32_MAX;
    monitor_interval_max = 0;
    monitor_interval_total = 0;
    monitor_high_count = 0;
    monitor_high_min = UINT32_MAX;
    monitor_high_max = 0;
    monitor_high_total = 0;
}

static inline void IRAM_ATTR
i2s_monitor_sample(uint32_t state, uint32_t clock)
{
    if (monitor_step_bit >= ESP32_I2S_OUT_COUNT)
        return;
    uint8_t step = !!(state & (1u << monitor_step_bit));
    if (step == monitor_step_value)
        return;
    monitor_step_value = step;
    if (step) {
        if (monitor_have_rise) {
            uint32_t interval = clock - monitor_last_rise;
            monitor_interval_total += interval;
            monitor_interval_count++;
            if (interval < monitor_interval_min)
                monitor_interval_min = interval;
            if (interval > monitor_interval_max)
                monitor_interval_max = interval;
        }
        monitor_last_rise = clock;
        monitor_have_rise = 1;
        monitor_step_rises++;
        if (monitor_dir_bit < ESP32_I2S_OUT_COUNT) {
            uint8_t dir = !!(state & (1u << monitor_dir_bit));
            if (monitor_have_dir && dir != monitor_dir_value)
                monitor_dir_changes++;
            monitor_dir_value = dir;
            monitor_have_dir = 1;
        }
    } else if (monitor_have_rise) {
        uint32_t high = clock - monitor_last_rise;
        monitor_high_total += high;
        monitor_high_count++;
        if (high < monitor_high_min)
            monitor_high_min = high;
        if (high > monitor_high_max)
            monitor_high_max = high;
    }
}

static void IRAM_ATTR
i2s_fifo_isr(void *arg)
{
    uint32_t before = read_ccount();
    uint_fast8_t count = I2S_FIFO_RELOAD;
    while (count--) {
        i2s_fill_clock += I2S_FRAME_TICKS;
        traj_stepper_i2s_advance(i2s_fill_clock);
        uint32_t state = i2s_shadow;
        i2s_monitor_sample(state, i2s_fill_clock);
        uint8_t output_bit = mirror_output_bit;
        if (output_bit < ESP32_I2S_OUT_COUNT) {
            uint8_t value = !!(state & (1u << mirror_source_bit));
            value ^= mirror_invert;
            if (value)
                state |= 1u << output_bit;
            else
                state &= ~(1u << output_bit);
        }
        // FluidNC and the Rodent schematic both use direct bit numbering:
        // I2SO0/Q0 is the last bit shifted into the first 74AHCT595 and
        // I2SO15/Q7 is the first.  The ESP32 sends the slot MSB-first, so
        // the FIFO word already lands at the correspondingly numbered Q pin.
        I2S0.fifo_wr = state;
        i2s_samples++;
    }
    i2s_ll_clear_intr_status(&I2S0, I2S_PUT_DATA_INT_CLR);
    uint32_t elapsed = read_ccount() - before;
    i2s_refills++;
    i2s_total_cycles += elapsed;
    if (elapsed > i2s_max_cycles)
        i2s_max_cycles = elapsed;
}

static void
i2s_gpio_attach(void)
{
    esp32_pad_config(CONFIG_KLIPPER_I2S_DATA_PIN, 0, 1, 0, 0);
    esp32_pad_config(CONFIG_KLIPPER_I2S_BCK_PIN, 0, 1, 0, 0);
    esp32_pad_config(CONFIG_KLIPPER_I2S_WS_PIN, 0, 1, 0, 0);
    esp32_matrix_out(CONFIG_KLIPPER_I2S_DATA_PIN, I2S0O_DATA_OUT23_IDX);
    esp32_matrix_out(CONFIG_KLIPPER_I2S_BCK_PIN, I2S0O_BCK_OUT_IDX);
    esp32_matrix_out(CONFIG_KLIPPER_I2S_WS_PIN, I2S0O_WS_OUT_IDX);
}

void
i2s_shift_init(void)
{
    i2s_monitor_reset();
    periph_module_reset(PERIPH_I2S0_MODULE);
    periph_module_enable(PERIPH_I2S0_MODULE);
    i2s_gpio_attach();

    i2s_ll_tx_stop_link(&I2S0);
    i2s_ll_tx_stop(&I2S0);
    i2s_ll_tx_reset(&I2S0);
    i2s_ll_rx_reset(&I2S0);
    i2s_ll_tx_reset_fifo(&I2S0);
    i2s_ll_rx_reset_fifo(&I2S0);
    i2s_ll_enable_lcd(&I2S0, false);
    i2s_ll_enable_camera(&I2S0, false);
    i2s_ll_tx_enable_std(&I2S0);
    i2s_ll_enable_dma(&I2S0, false);
    i2s_ll_tx_select_std_slot(&I2S0, I2S_STD_SLOT_BOTH, false);
    i2s_ll_tx_set_sample_bit(
        &I2S0, I2S_DATA_BIT_WIDTH_32BIT, I2S_DATA_BIT_WIDTH_16BIT);
    i2s_ll_tx_enable_mono_mode(&I2S0, false);
    i2s_ll_rx_stop(&I2S0);
    i2s_ll_tx_enable_msb_right(&I2S0, true);
    i2s_ll_tx_enable_right_first(&I2S0, false);
    i2s_ll_tx_set_slave_mod(&I2S0, false);
    i2s_ll_tx_force_enable_fifo_mod(&I2S0, true);
    i2s_ll_tx_set_ws_width(&I2S0, 0);
    i2s_ll_tx_enable_msb_shift(&I2S0, false);
    i2s_ll_tx_clk_set_src(&I2S0, I2S_CLK_SRC_DEFAULT);

    // 160MHz / 5 / 2 / 32 = 500kHz 16-bit frame clock.
    hal_utils_clk_div_t div = {
        .integer = 5,
        .denominator = 0,
        .numerator = 0,
    };
    i2s_ll_tx_set_mclk(&I2S0, &div);
    i2s_ll_tx_set_bck_div_num(&I2S0, 2);

    I2S0.fifo_conf.tx_data_num = I2S_FIFO_THRESHOLD;
    int ret = esp_intr_alloc_intrstatus(
        ETS_I2S0_INTR_SOURCE, ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL3,
        (uint32_t)i2s_ll_get_intr_status_reg(&I2S0),
        I2S_PUT_DATA_INT_CLR_M, i2s_fifo_isr, NULL, NULL);
    if (ret)
        shutdown("Unable to allocate I2S output interrupt");

    // Seed a full FIFO with the safe idle word.  Once running, the refill
    // interrupt remains only 16-24 samples (32-48us) ahead of the pins.
    uint_fast8_t count = I2S_FIFO_LENGTH;
    while (count--) {
        I2S0.fifo_wr = i2s_shadow;
        i2s_samples++;
    }
    uint32_t now = timer_read_time();
    i2s_fill_clock = now + (I2S_FIFO_LENGTH - 1) * I2S_FRAME_TICKS;
    i2s_ll_tx_stop_on_fifo_empty(&I2S0, false);
    i2s_ll_clear_intr_status(&I2S0, I2S_PUT_DATA_INT_CLR);
    i2s_ll_enable_intr(&I2S0, I2S_TX_PUT_DATA_INT_ENA, 1);
    i2s_ll_tx_start(&I2S0);
}
DECL_INIT(i2s_shift_init);

void IRAM_ATTR
i2s_shift_write(uint8_t bit, uint8_t value)
{
    if (bit >= ESP32_I2S_OUT_COUNT)
        shutdown("Invalid I2S output bit");
    irqstatus_t flag = irq_save();
    uint32_t mask = 1u << bit;
    if (value)
        i2s_shadow |= mask;
    else
        i2s_shadow &= ~mask;
    irq_restore(flag);
}

void IRAM_ATTR
i2s_shift_toggle(uint8_t bit)
{
    if (bit >= ESP32_I2S_OUT_COUNT)
        shutdown("Invalid I2S output bit");
    i2s_shadow ^= 1u << bit;
}

uint8_t IRAM_ATTR
i2s_shift_read(uint8_t bit)
{
    return !!(i2s_shadow & (1u << bit));
}

void
command_i2s_shift_get_status(uint32_t *args)
{
    irqstatus_t flag = irq_save();
    uint32_t refills = i2s_refills;
    uint32_t average = refills ? i2s_total_cycles / refills : 0;
    uint32_t state = i2s_shadow;
    uint32_t max_cycles = i2s_max_cycles;
    uint32_t samples = i2s_samples;
    uint32_t step_bit = monitor_step_bit;
    uint32_t dir_bit = monitor_dir_bit;
    uint32_t rises = monitor_step_rises;
    uint32_t dir_changes = monitor_dir_changes;
    uint32_t dir_value = monitor_dir_value;
    uint32_t interval_count = monitor_interval_count;
    uint32_t interval_average = interval_count
        ? monitor_interval_total / interval_count : 0;
    uint32_t interval_min = interval_count ? monitor_interval_min : 0;
    uint32_t interval_max = interval_count ? monitor_interval_max : 0;
    uint32_t high_count = monitor_high_count;
    uint32_t high_average = high_count
        ? monitor_high_total / high_count : 0;
    uint32_t high_min = high_count ? monitor_high_min : 0;
    uint32_t high_max = high_count ? monitor_high_max : 0;
    irq_restore(flag);
    sendf("i2s_shift_status state=%u writes=%u bitrate=%u"
          " avg_cycles=%u max_cycles=%u monitor_step=%u monitor_dir=%u"
          " step_rises=%u interval_count=%u interval_min=%u"
          " interval_avg=%u interval_max=%u high_count=%u high_min=%u"
          " high_avg=%u high_max=%u dir_changes=%u dir_value=%u",
          state, samples, 8000000, average, max_cycles,
          step_bit, dir_bit, rises, interval_count, interval_min,
          interval_average, interval_max, high_count, high_min, high_average,
          high_max, dir_changes, dir_value);
}
DECL_COMMAND(command_i2s_shift_get_status, "i2s_shift_get_status");

void
command_i2s_shift_monitor(uint32_t *args)
{
    uint8_t step_bit = args[0], dir_bit = args[1];
    if (step_bit >= ESP32_I2S_OUT_COUNT
        || dir_bit >= ESP32_I2S_OUT_COUNT)
        shutdown("Invalid I2S monitor bit");
    irqstatus_t flag = irq_save();
    monitor_step_bit = step_bit;
    monitor_dir_bit = dir_bit;
    i2s_monitor_reset();
    irq_restore(flag);
}
DECL_COMMAND(command_i2s_shift_monitor,
             "i2s_shift_monitor step_bit=%c dir_bit=%c");

void
command_i2s_shift_mirror(uint32_t *args)
{
    uint8_t source_bit = args[0], output_bit = args[1];
    uint8_t invert = args[2], enable = args[3];
    if (source_bit >= ESP32_I2S_OUT_COUNT
        || output_bit >= ESP32_I2S_OUT_COUNT || invert > 1 || enable > 1)
        shutdown("Invalid I2S mirror configuration");
    irqstatus_t flag = irq_save();
    if (enable) {
        mirror_source_bit = source_bit;
        mirror_invert = invert;
        // Publish the output last so the ISR cannot observe a partially
        // configured mirror.
        mirror_output_bit = output_bit;
    } else {
        mirror_output_bit = 0xff;
    }
    irq_restore(flag);
}
DECL_COMMAND(command_i2s_shift_mirror,
             "i2s_shift_mirror source_bit=%c output_bit=%c"
             " invert=%c enable=%c");

#endif // CONFIG_WANT_ESP32_I2S_SHIFT
