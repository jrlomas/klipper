// WiFi station bringup for the ESP32 port
//
// Runs on core 0 (app_main context); the WiFi and lwIP tasks are
// pinned to core 0 via sdkconfig.defaults, keeping the radio stack
// off the motion core (FD-0001 doc 07 core-pinning).  Credentials
// come from the IDF Kconfig (idf.py menuconfig -> "Klipper
// firmware").  An Ethernet (RMII) board would replace this file with
// esp_eth bringup; the datagram console binding is identical.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // strncpy
#include "sdkconfig.h" // CONFIG_KLIPPER_WIFI_SSID
#include "freertos/FreeRTOS.h" // pdMS_TO_TICKS
#include "freertos/task.h" // vTaskDelay
#include "esp_event.h" // esp_event_loop_create_default
#include "esp_log.h" // ESP_LOGI
#include "esp_netif.h" // esp_netif_init
#include "esp_wifi.h" // esp_wifi_init
#include "internal.h" // esp32_wifi_start

static const char *TAG = "klipper_wifi";

static void
wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        // Reconnect forever - the mcu-side intention horizon and the
        // host's retransmit machinery ride out the outage
        ESP_LOGW(TAG, "disconnected, reconnecting");
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&ev->ip_info.ip));
    }
}

void
esp32_wifi_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
                        WIFI_EVENT, ESP_EVENT_ANY_ID
                        , &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
                        IP_EVENT, IP_EVENT_STA_GOT_IP
                        , &wifi_event_handler, NULL, NULL));

    wifi_config_t wc;
    memset(&wc, 0, sizeof(wc));
    strncpy((char *)wc.sta.ssid, CONFIG_KLIPPER_WIFI_SSID
            , sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, CONFIG_KLIPPER_WIFI_PASSWORD
            , sizeof(wc.sta.password) - 1);
    wc.sta.threshold.authmode = (CONFIG_KLIPPER_WIFI_PASSWORD[0]
                                 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN);
    // Favor latency stability over power: no modem sleep on a motion
    // control link
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);
}
