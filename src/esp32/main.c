// Main starting point for the ESP32 port
//
// Two selectable architectures (FD-0001 doc 12; Kconfig "Klipper
// firmware" -> architecture):
//
//  * "component" (stage 1): app_main() runs on core 0 and brings up
//    NVS, WiFi and the UDP console socket there, alongside the
//    WiFi/lwIP tasks (pinned to core 0 via sdkconfig.defaults).  The
//    klipper scheduler runs in a dedicated high-priority FreeRTOS
//    task pinned to core 1, which also allocates the klipper
//    hardware-timer interrupt on core 1.
//
//  * "modem" (stage 3): the IDF app is built unicore; core 0 is
//    reduced to a network coprocessor (NVS, WiFi, and the modem task
//    shuttling sealed datagrams through the shared-memory ring),
//    and core 1 is manually unstalled into bare-metal sched_main()
//    with no RTOS and no IDF calls (appcpu_boot.c).  WiFi comes up
//    *before* the bare core so the PHY-calibration NVS write - the
//    last flash write - never races core 1's flash-cache use.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "sdkconfig.h" // CONFIG_KLIPPER_UDP_PORT
#include "freertos/FreeRTOS.h" // xTaskCreatePinnedToCore
#include "freertos/task.h" // vTaskDelay
#include "esp_log.h" // ESP_LOGE
#include "nvs.h" // nvs_open
#include "nvs_flash.h" // nvs_flash_init
#include "board/misc.h" // console_sendf
#include "command.h" // DECL_CONSTANT_STR
#include "generic/udp_console.h" // udp_console_sendf
#include "internal.h" // esp32_wifi_start
#include "sched.h" // sched_main
#if KLIPPER_ARCH_MODEM
#include "shmem_ring.h" // esp32_shmem
#endif

DECL_CONSTANT_STR("MCU", "esp32");

static const char *TAG = "klipper";

// The console is the datagram transport glue (see generic/
// udp_console.c) over the lwIP socket (udp_port.c, component arch)
// or the shared-memory ring (shmem_console.c, modem arch)
void
console_sendf(const struct command_encoder *ce, va_list args)
{
    udp_console_sendf(ce, args);
}

void *
console_receive_buffer(void)
{
    return udp_console_get_rx_buf();
}

#if !KLIPPER_ARCH_MODEM
// klipper scheduler task - pinned to core 1 (component arch)
static void
klipper_task(void *arg)
{
    board_set_main_task(xTaskGetCurrentTaskHandle());
    esp32_timer_setup();
    sched_main();
}
#endif

/****************************************************************
 * PSK provisioning
 ****************************************************************/

// The pre-shared key is looked up in NVS (namespace "klipper", key
// "udp_psk" - blob or string), falling back to the build-time
// CONFIG_KLIPPER_PSK.  Running without a key requires the explicit
// CONFIG_KLIPPER_TRUST_NETWORK confession.
static uint8_t psk_buf[64];
static uint32_t psk_len;

static void
load_psk(void)
{
    nvs_handle_t h;
    if (nvs_open("klipper", NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(psk_buf);
        if (nvs_get_blob(h, "udp_psk", psk_buf, &len) == ESP_OK && len) {
            psk_len = len;
        } else {
            len = sizeof(psk_buf);
            if (nvs_get_str(h, "udp_psk", (char *)psk_buf, &len) == ESP_OK
                && len > 1)
                psk_len = len - 1; // trailing NUL
        }
        nvs_close(h);
    }
    if (!psk_len && sizeof(CONFIG_KLIPPER_PSK) > 1) {
        psk_len = sizeof(CONFIG_KLIPPER_PSK) - 1;
        if (psk_len > sizeof(psk_buf))
            psk_len = sizeof(psk_buf);
        memcpy(psk_buf, CONFIG_KLIPPER_PSK, psk_len);
    }
}

/****************************************************************
 * Startup
 ****************************************************************/

void
app_main(void)
{
    // NVS (WiFi calibration data + PSK storage)
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES
        || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    load_psk();
    if (!psk_len && !KLIPPER_TRUST_NETWORK) {
        // Authentication is mandatory on network transports
        for (;;) {
            ESP_LOGE(TAG, "no PSK provisioned (NVS klipper/udp_psk or"
                     " CONFIG_KLIPPER_PSK) and CONFIG_KLIPPER_TRUST_NETWORK"
                     " not set - refusing to start");
            vTaskDelay(pdMS_TO_TICKS(5000));
        }
    }
    if (KLIPPER_TRUST_NETWORK && !psk_len)
        ESP_LOGW(TAG, "running UNAUTHENTICATED (trust_network confession)");

    // Network bringup on core 0.  In the modem architecture this
    // must complete before core 1 boots: esp_wifi_start()'s first
    // run writes PHY calibration data to NVS (flash), and a flash
    // write disables the cache the bare core executes from.
    esp32_wifi_start();

#if KLIPPER_ARCH_MODEM
    // Stage the core-1 boot parameters in the shared area, start
    // the datagram shuttle, then release the bare core
    memcpy(esp32_shmem.psk, psk_buf, psk_len);
    esp32_shmem.psk_len = psk_len;
    esp32_shmem.trust_network = KLIPPER_TRUST_NETWORK;
    esp32_adc_modem_init();
    if (esp32_modem_start(CONFIG_KLIPPER_UDP_PORT) < 0)
        return;
    if (esp32_appcpu_start() < 0)
        return;
#else
    if (esp32_udp_port_setup(CONFIG_KLIPPER_UDP_PORT, psk_buf, psk_len) < 0)
        return;

    // Motion/scheduler on core 1
    xTaskCreatePinnedToCore(klipper_task, "klipper", 8192, NULL
                            , configMAX_PRIORITIES - 3, NULL, 1);
#endif
}
