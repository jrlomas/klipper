// ESP32 ADC1 continuous acquisition through the ESP-IDF DMA driver.
//
// IDF and the I2S0-backed continuous driver remain on core 0. Core 1 owns the
// generic acquisition state machine and consumes a bounded cross-core mailbox.
// Samples are averaged in the core-0 worker when the requested scan rate is
// below the classic ESP32 driver's 20k conversion/s minimum.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy, memset
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_adc/adc_continuous.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "soc/soc_caps.h"
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "adc_stream.h"
#include "board/misc.h" // timer_from_us
#include "command.h" // shutdown
#include "generic/acq_block.h" // ACQ_STATUS_*
#include "generic/dma_resource.h"
#include "internal.h" // architecture helpers
#include "sched.h" // sched_shutdown

#define ESP32_ADC_FRAME_BYTES 256
#define ESP32_ADC_POOL_BYTES 8192
#define ESP32_ADC_MIN_CONVERSIONS 20000u
#define ESP32_ADC_MAX_CONVERSIONS 2000000u

static struct adc_stream_backend_config pending_cfg;
static uint8_t pending_channels[ADC_STREAM_MAX_CHANNELS];
static uint16_t pending_osr;
static uint32_t pending_conversion_hz;
static uint32_t setup_sequence;
static uint32_t start_sequence;
static uint32_t ready_sequence[ADC_STREAM_BLOCK_COUNT];
static uint32_t fault_pending;
static uint8_t ready_mask;
static uint8_t free_mask;
static uint8_t run_requested;
static uint8_t stream_claimed;
static TaskHandle_t stream_task_handle;
static volatile uint8_t driver_overflow;
static volatile uint8_t calibration_scheme;
static volatile uint16_t calibration_zero_mv, calibration_full_mv;

static int8_t
pin_to_channel(uint32_t pin)
{
    if (pin >= 36 && pin <= 39)
        return pin - 36;
    if (pin >= 32 && pin <= 35)
        return pin - 28;
    return -1;
}

struct gpio_adc
board_adc_stream_setup_pin(uint32_t pin)
{
    int8_t channel = pin_to_channel(pin);
    if (channel < 0)
        shutdown("ESP32 continuous stream requires an ADC1 pin");
    return (struct gpio_adc){ .chan = channel };
}

uint8_t
esp32_adc_stream_is_claimed(void)
{
    return __atomic_load_n(&stream_claimed, __ATOMIC_ACQUIRE);
}

void
board_adc_stream_setup(const struct adc_stream_backend_config *cfg,
                       struct adc_stream_backend_info *info)
{
    if (cfg->hardware_oversample != 1 || cfg->hardware_shift)
        shutdown("ESP32 ADC uses software oversampling");
    if (dma_claim(DMA_RESOURCE_ESP32_ADC1, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_ESP32_I2S0, 0, cfg->owner)
        || dma_claim(DMA_RESOURCE_ESP32_ADC_POOL, 0, cfg->owner))
        shutdown("ESP32 ADC stream resource conflict");
    if (esp32_adc_legacy_is_active())
        shutdown("ESP32 ADC1 already claimed by legacy ADC input");
    if (!cfg->channel_count || cfg->channel_count > ADC_STREAM_MAX_CHANNELS)
        shutdown("ESP32 ADC stream channel limit");
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        uint8_t channel = cfg->pins[i].chan;
        if (channel > 7 || (i && channel <= cfg->pins[i - 1].chan))
            shutdown("ESP32 ADC stream channels must ascend");
        pending_channels[i] = channel;
    }

    uint64_t base = (uint64_t)CONFIG_CLOCK_FREQ * cfg->channel_count;
    uint32_t base_hz = (base + cfg->requested_period_ticks - 1)
                       / cfg->requested_period_ticks;
    if (!base_hz || base_hz > ESP32_ADC_MAX_CONVERSIONS)
        shutdown("ESP32 ADC stream period out of range");
    uint32_t osr = (ESP32_ADC_MIN_CONVERSIONS + base_hz - 1) / base_hz;
    if (!osr)
        osr = 1;
    if (osr > 1024)
        shutdown("ESP32 ADC stream oversampling ratio too large");
    uint64_t scaled = base * osr;
    uint32_t conversion_hz = (scaled + cfg->requested_period_ticks - 1)
                             / cfg->requested_period_ticks;
    if (conversion_hz < ESP32_ADC_MIN_CONVERSIONS)
        conversion_hz = ESP32_ADC_MIN_CONVERSIONS;
    if (conversion_hz > ESP32_ADC_MAX_CONVERSIONS)
        shutdown("ESP32 ADC stream exceeds conversion bandwidth");

    pending_cfg = *cfg;
    pending_osr = osr;
    pending_conversion_hz = conversion_hz;
    __atomic_store_n(&ready_mask, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&fault_pending, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&stream_claimed, 1, __ATOMIC_RELEASE);
    uint32_t sequence = __atomic_load_n(&setup_sequence, __ATOMIC_RELAXED) + 1;
    __atomic_store_n(&setup_sequence, sequence, __ATOMIC_RELEASE);

    info->period_numerator = scaled;
    info->period_denominator = conversion_hz;
    // IDF starts the I2S-backed engine asynchronously on core 0. Advertise a
    // conservative phase bound instead of pretending the scheduled core-1
    // start clock is the first conversion aperture.
    info->uncertainty_ticks = timer_from_us(2000)
                              + cfg->requested_period_ticks / 2;
    info->status = ACQ_STATUS_INFERRED_TIME;
    info->max_conversion_rate = ESP32_ADC_MAX_CONVERSIONS;
    info->capabilities = ADC_BACKEND_CAP_INFERRED_START
                         | ADC_BACKEND_CAP_SAMPLE_TAGS
                         | ADC_BACKEND_CAP_CALIBRATION;
    info->max_hardware_oversample = 1;
    info->resolution_bits = SOC_ADC_DIGI_MAX_BITWIDTH;
    info->adc_count = 1;
    info->watchdog_count = 0;
    info->timing_quality = 0;
}

void
board_adc_stream_start(void)
{
    uint32_t sequence = __atomic_load_n(&setup_sequence, __ATOMIC_ACQUIRE);
    __atomic_store_n(&start_sequence, sequence, __ATOMIC_RELEASE);
    __atomic_store_n(&run_requested, 1, __ATOMIC_RELEASE);
}

void
board_adc_stream_stop_from_isr(void)
{
    __atomic_store_n(&run_requested, 0, __ATOMIC_RELEASE);
    __atomic_store_n(&stream_claimed, 0, __ATOMIC_RELEASE);
}

void
board_adc_stream_stop(void)
{
    board_adc_stream_stop_from_isr();
}

void
board_adc_stream_block_released(uint8_t block_index)
{
    __atomic_fetch_or(&free_mask, 1u << block_index, __ATOMIC_RELEASE);
}

static void
publish_fault(uint32_t status)
{
    __atomic_fetch_or(&fault_pending,
                      status | ACQ_STATUS_DISCONTINUITY, __ATOMIC_RELEASE);
    __atomic_store_n(&run_requested, 0, __ATOMIC_RELEASE);
#if !KLIPPER_ARCH_MODEM
    board_wake_main();
#endif
}

static bool IRAM_ATTR
pool_overflow_callback(adc_continuous_handle_t handle,
                       const adc_continuous_evt_data_t *event, void *user_data)
{
    BaseType_t wake = pdFALSE;
    driver_overflow = 1;
    if (stream_task_handle)
        vTaskNotifyGiveFromISR(stream_task_handle, &wake);
    return wake == pdTRUE;
}

static int
start_driver(const struct adc_stream_backend_config *cfg,
             const uint8_t *channels, uint32_t conversion_hz,
             adc_continuous_handle_t *handle, adc_cali_handle_t *cali)
{
    adc_cali_line_fitting_config_t cal_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = SOC_ADC_DIGI_MAX_BITWIDTH,
        .default_vref = 1100,
    };
    int zero_mv = 0, full_mv = 0;
    if (adc_cali_create_scheme_line_fitting(&cal_cfg, cali) == ESP_OK
        && adc_cali_raw_to_voltage(*cali, 0, &zero_mv) == ESP_OK
        && adc_cali_raw_to_voltage(*cali, (1 << SOC_ADC_DIGI_MAX_BITWIDTH) - 1,
                                   &full_mv) == ESP_OK) {
        __atomic_store_n(&calibration_zero_mv, zero_mv, __ATOMIC_RELEASE);
        __atomic_store_n(&calibration_full_mv, full_mv, __ATOMIC_RELEASE);
        __atomic_store_n(&calibration_scheme, 1, __ATOMIC_RELEASE);
    } else if (*cali) {
        adc_cali_delete_scheme_line_fitting(*cali);
        *cali = NULL;
    }
    adc_continuous_handle_cfg_t handle_cfg = {
        .max_store_buf_size = ESP32_ADC_POOL_BYTES,
        .conv_frame_size = ESP32_ADC_FRAME_BYTES,
        .flags.flush_pool = 0,
    };
    if (adc_continuous_new_handle(&handle_cfg, handle) != ESP_OK)
        return -1;

    adc_digi_pattern_config_t pattern[ADC_STREAM_MAX_CHANNELS] = {0};
    for (uint8_t i = 0; i < cfg->channel_count; i++) {
        pattern[i].atten = ADC_ATTEN_DB_12;
        pattern[i].channel = channels[i];
        pattern[i].unit = ADC_UNIT_1;
        pattern[i].bit_width = SOC_ADC_DIGI_MAX_BITWIDTH;
    }
    adc_continuous_config_t continuous_cfg = {
        .pattern_num = cfg->channel_count,
        .adc_pattern = pattern,
        .sample_freq_hz = conversion_hz,
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,
        .format = ADC_DIGI_OUTPUT_FORMAT_TYPE1,
    };
    adc_continuous_evt_cbs_t callbacks = {
        .on_pool_ovf = pool_overflow_callback,
    };
    if (adc_continuous_config(*handle, &continuous_cfg) != ESP_OK
        || adc_continuous_register_event_callbacks(
            *handle, &callbacks, NULL) != ESP_OK
        || adc_continuous_start(*handle) != ESP_OK) {
        adc_continuous_deinit(*handle);
        *handle = NULL;
        if (*cali) {
            adc_cali_delete_scheme_line_fitting(*cali);
            *cali = NULL;
        }
        return -1;
    }
    return 0;
}

static int
reserve_block(uint8_t block)
{
    uint8_t bit = 1u << block;
    uint8_t old = __atomic_fetch_and(&free_mask, ~bit, __ATOMIC_ACQ_REL);
    return old & bit ? 0 : -1;
}

static void
adc_stream_task(void *arg)
{
    uint8_t frame[ESP32_ADC_FRAME_BYTES];
    stream_task_handle = xTaskGetCurrentTaskHandle();
    for (;;) {
        while (!__atomic_load_n(&run_requested, __ATOMIC_ACQUIRE))
            // Core 1 may be bare metal in modem mode, so it must not call a
            // FreeRTOS notification API. A one-tick poll keeps that boundary
            // intact and is included in the advertised start uncertainty.
            ulTaskNotifyTake(pdTRUE, 1);

        uint32_t local_sequence = __atomic_load_n(
            &start_sequence, __ATOMIC_ACQUIRE);
        struct adc_stream_backend_config cfg = pending_cfg;
        uint8_t channels[ADC_STREAM_MAX_CHANNELS];
        memcpy(channels, pending_channels, sizeof(channels));
        uint16_t osr = pending_osr;
        uint32_t conversion_hz = pending_conversion_hz;
        if (__atomic_load_n(&setup_sequence, __ATOMIC_ACQUIRE)
            != local_sequence)
            continue;

        adc_continuous_handle_t handle = NULL;
        adc_cali_handle_t cali = NULL;
        driver_overflow = 0;
        if (start_driver(&cfg, channels, conversion_hz, &handle, &cali)) {
            publish_fault(ACQ_STATUS_PERIPHERAL_ERROR);
            continue;
        }

        uint32_t sums[ADC_STREAM_MAX_CHANNELS] = {0};
        uint16_t local_block[ADC_STREAM_MAX_BLOCK_VALUES];
        uint16_t averaged_scans = 0;
        uint8_t channel_index = 0, block_index = 0, block_pos = 0;
        uint32_t block_sequence = 0;
        __atomic_store_n(&free_mask, 3, __ATOMIC_RELEASE);
        if (reserve_block(0)) {
            publish_fault(ACQ_STATUS_OVERRUN);
            goto stop;
        }

        while (__atomic_load_n(&run_requested, __ATOMIC_ACQUIRE)
               && __atomic_load_n(&start_sequence, __ATOMIC_ACQUIRE)
                  == local_sequence) {
            if (driver_overflow) {
                publish_fault(ACQ_STATUS_OVERRUN
                              | ACQ_STATUS_BACKEND_POOL_OVERFLOW);
                break;
            }
            uint32_t length = 0;
            esp_err_t ret = adc_continuous_read(
                handle, frame, sizeof(frame), &length, 20);
            if (ret == ESP_ERR_TIMEOUT)
                continue;
            if (ret != ESP_OK) {
                publish_fault(ACQ_STATUS_DMA_ERROR);
                break;
            }
            for (uint32_t offset = 0; offset < length;
                 offset += SOC_ADC_DIGI_RESULT_BYTES) {
                adc_digi_output_data_t *result =
                    (adc_digi_output_data_t *)&frame[offset];
                if (result->type1.channel != channels[channel_index]) {
                    publish_fault(ACQ_STATUS_SAMPLE_ERROR);
                    goto stop;
                }
                sums[channel_index] += result->type1.data;
                if (++channel_index < cfg.channel_count)
                    continue;
                channel_index = 0;
                if (++averaged_scans < osr)
                    continue;
                averaged_scans = 0;
                for (uint8_t i = 0; i < cfg.channel_count; i++) {
                    local_block[block_pos++] = (sums[i] + osr / 2) / osr;
                    sums[i] = 0;
                }
                if (block_pos != cfg.block_values)
                    continue;

                // Unlike the native backends, IDF DMA writes into its own
                // bounded pool. Hold this completed local block until the
                // consumer has returned the block that follows it. This
                // preserves the generic two-block ownership invariant while
                // the IDF pool safely absorbs short core-1 scheduling delays.
                uint8_t next_block = block_index ^ 1;
                while (reserve_block(next_block)) {
                    if (driver_overflow) {
                        publish_fault(ACQ_STATUS_OVERRUN
                                      | ACQ_STATUS_BACKEND_POOL_OVERFLOW);
                        goto stop;
                    }
                    if (!__atomic_load_n(&run_requested, __ATOMIC_ACQUIRE)
                        || __atomic_load_n(&start_sequence,
                                           __ATOMIC_ACQUIRE)
                           != local_sequence)
                        goto stop;
                    vTaskDelay(1);
                }
                // A new setup invalidates this local generation. Never copy
                // stale samples into the generic block pool after restart.
                if (__atomic_load_n(&setup_sequence, __ATOMIC_ACQUIRE)
                    != local_sequence)
                    goto stop;
                memcpy(&cfg.buffer[block_index * cfg.block_values],
                       local_block, block_pos * sizeof(uint16_t));
                ready_sequence[block_index] = block_sequence++;
                __atomic_fetch_or(&ready_mask, 1u << block_index,
                                  __ATOMIC_RELEASE);
#if !KLIPPER_ARCH_MODEM
                board_wake_main();
#endif
                block_index = next_block;
                block_pos = 0;
            }
        }

stop:
        adc_continuous_stop(handle);
        adc_continuous_deinit(handle);
        if (cali)
            adc_cali_delete_scheme_line_fitting(cali);
    }
}

void
command_adc_stream_get_calibration(uint32_t *args)
{
    sendf("adc_stream_calibration oid=%c scheme=%c zero_mv=%hu"
          " full_mv=%hu attenuation=%c",
          args[0], __atomic_load_n(&calibration_scheme, __ATOMIC_ACQUIRE),
          __atomic_load_n(&calibration_zero_mv, __ATOMIC_ACQUIRE),
          __atomic_load_n(&calibration_full_mv, __ATOMIC_ACQUIRE), 12);
}
DECL_COMMAND_FLAGS(command_adc_stream_get_calibration, HF_IN_SHUTDOWN,
                   "adc_stream_get_calibration oid=%c");

void
esp32_adc_stream_poll(void)
{
    uint8_t mask = __atomic_exchange_n(&ready_mask, 0, __ATOMIC_ACQ_REL);
    while (mask) {
        uint8_t block;
        if (mask == 3)
            block = ready_sequence[0] < ready_sequence[1] ? 0 : 1;
        else
            block = mask & 1 ? 0 : 1;
        mask &= ~(1u << block);
        if (adc_stream_block_complete(block, 0))
            break;
    }
    uint32_t fault = __atomic_exchange_n(
        &fault_pending, 0, __ATOMIC_ACQ_REL);
    if (fault)
        adc_stream_backend_fault(fault);
}

void
esp32_adc_stream_init(void)
{
    xTaskCreatePinnedToCore(adc_stream_task, "klipper_adc_dma", 4096, NULL,
                            tskIDLE_PRIORITY + 5, NULL, 0);
}
