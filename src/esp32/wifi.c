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
#include "esp_event.h" // esp_event_loop_create_default
#include "esp_log.h" // ESP_LOGI
#include "esp_netif.h" // esp_netif_init
#include "esp_system.h" // esp_reset_reason
#include "esp_wifi.h" // esp_wifi_init
#include "command.h" // DECL_COMMAND_FLAGS
#include "internal.h" // esp32_wifi_start

static const char *TAG = "klipper_wifi";
static uint32_t wifi_connected, wifi_got_ip;
static uint32_t wifi_connect_attempts, wifi_disconnects, wifi_got_ips;
static uint32_t wifi_last_disconnect_reason;

static void
network_changed(uint8_t up)
{
#if KLIPPER_ARCH_MODEM
    esp32_modem_network_changed(up);
#else
    esp32_udp_port_network_changed(up);
#endif
}

static void
wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        // esp32_wifi_start() connects explicitly only after applying and
        // verifying the no-power-save policy. Connecting from this event
        // races that policy setter and can begin association in the default
        // WIFI_PS_MIN_MODEM mode.
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_CONNECTED) {
        __atomic_store_n(&wifi_connected, 1, __ATOMIC_RELEASE);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *ev =
            (wifi_event_sta_disconnected_t *)data;
        uint32_t reason = ev ? ev->reason : 0;
        __atomic_store_n(&wifi_connected, 0, __ATOMIC_RELEASE);
        __atomic_store_n(&wifi_got_ip, 0, __ATOMIC_RELEASE);
        __atomic_add_fetch(&wifi_disconnects, 1, __ATOMIC_RELAXED);
        __atomic_store_n(&wifi_last_disconnect_reason, reason,
                         __ATOMIC_RELEASE);
        network_changed(0);
        // Reconnect forever - the mcu-side intention horizon and the
        // host's retransmit machinery ride out the outage
        ESP_LOGW(TAG, "disconnected (reason=%u), reconnecting",
                 (unsigned)reason);
        // Event handlers run on the system event task; do not block it.
        // IDF serializes connection attempts and emits another disconnect
        // event if this attempt fails, giving an indefinite retry loop.
        __atomic_add_fetch(&wifi_connect_attempts, 1, __ATOMIC_RELAXED);
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK)
            ESP_LOGW(TAG, "reconnect request failed: %s",
                     esp_err_to_name(err));
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        __atomic_store_n(&wifi_got_ip, 1, __ATOMIC_RELEASE);
        __atomic_add_fetch(&wifi_got_ips, 1, __ATOMIC_RELAXED);
        network_changed(1);
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
    // ESP-IDF equivalent of Arduino's WiFi.setSleep(false): keep modem
    // sleep off on a real-time command link. Apply this before association,
    // then read it back so a setter that returned success but did not change
    // the effective policy fails bring-up instead of silently adding latency.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    wifi_ps_type_t ps;
    ESP_ERROR_CHECK(esp_wifi_get_ps(&ps));
    ESP_ERROR_CHECK(ps == WIFI_PS_NONE ? ESP_OK : ESP_FAIL);
    ESP_ERROR_CHECK(esp_wifi_set_max_tx_power(
                        CONFIG_KLIPPER_WIFI_MAX_TX_POWER_QDBM));
    __atomic_add_fetch(&wifi_connect_attempts, 1, __ATOMIC_RELAXED);
    ESP_ERROR_CHECK(esp_wifi_connect());
}

#if !KLIPPER_ARCH_MODEM
void
command_wifi_get_status(uint32_t *args)
{
    (void)args;
    int8_t tx_power = CONFIG_KLIPPER_WIFI_MAX_TX_POWER_QDBM;
    int32_t rssi = -127;
    wifi_ps_type_t ps = WIFI_PS_MAX_MODEM;
    wifi_ap_record_t ap;
    uint32_t ps_valid = esp_wifi_get_ps(&ps) == ESP_OK;
    if (esp_wifi_get_max_tx_power(&tx_power) != ESP_OK)
        tx_power = CONFIG_KLIPPER_WIFI_MAX_TX_POWER_QDBM;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
        rssi = ap.rssi;
    sendf("wifi_status connected=%u got_ip=%u connect_attempts=%u"
          " disconnects=%u got_ips=%u last_reason=%u"
          " tx_power_qdbm=%i rssi=%i reset_reason=%u"
          " ps_mode=%u ps_valid=%u ampdu_rx=%u ampdu_tx=%u",
          __atomic_load_n(&wifi_connected, __ATOMIC_ACQUIRE),
          __atomic_load_n(&wifi_got_ip, __ATOMIC_ACQUIRE),
          __atomic_load_n(&wifi_connect_attempts, __ATOMIC_RELAXED),
          __atomic_load_n(&wifi_disconnects, __ATOMIC_RELAXED),
          __atomic_load_n(&wifi_got_ips, __ATOMIC_RELAXED),
          __atomic_load_n(&wifi_last_disconnect_reason, __ATOMIC_ACQUIRE),
          (int32_t)tx_power, rssi, (uint32_t)esp_reset_reason(),
          (uint32_t)ps, ps_valid,
          (uint32_t)WIFI_AMPDU_RX_ENABLED,
          (uint32_t)WIFI_AMPDU_TX_ENABLED);
}
DECL_COMMAND_FLAGS(command_wifi_get_status, HF_IN_SHUTDOWN,
                   "wifi_get_status");
#endif
