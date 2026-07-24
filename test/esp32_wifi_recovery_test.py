#!/usr/bin/env python3
"""Source-contract regression for the ESP32 command-link recovery path.

ESP-IDF invalidates application sockets when a station disconnects.  The
Rodent print regression was caused by reconnecting WiFi while retaining that
dead UDP socket.  This test keeps the required event-to-socket lifecycle and
the low-latency radio policy visible in ordinary host CI; the ESP-IDF target
build remains the compile-time proof.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def main():
    wifi = read("src/esp32/wifi.c")
    udp = read("src/esp32/udp_port.c")
    modem = read("src/esp32/modem.c")
    rodent = read("src/esp32/sdkconfig.defaults.rodent")
    host = read("klippy/extras/helix_self_test.py")

    assert "WIFI_EVENT_STA_DISCONNECTED" in wifi
    assert "network_changed(0)" in wifi
    assert "IP_EVENT_STA_GOT_IP" in wifi
    assert "network_changed(1)" in wifi
    assert "ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE))" in wifi
    assert "ESP_ERROR_CHECK(esp_wifi_get_ps(&ps))" in wifi
    assert "ps == WIFI_PS_NONE ? ESP_OK : ESP_FAIL" in wifi
    assert "esp_wifi_set_max_tx_power" in wifi
    assert "CONFIG_KLIPPER_WIFI_MAX_TX_POWER_QDBM=34" in rodent
    assert "CONFIG_ESP_WIFI_AMPDU_RX_ENABLED=n" in rodent
    assert "CONFIG_ESP_WIFI_AMPDU_TX_ENABLED=n" in rodent
    assert "ps_mode=%u ps_valid=%u ampdu_rx=%u ampdu_tx=%u" in wifi

    # Both ESP32 architectures must discard and recreate their socket after
    # link loss; merely calling esp_wifi_connect() is not sufficient.
    for source, prefix in ((udp, "udp"), (modem, "modem")):
        assert "%s_close_socket" % (prefix,) in source
        assert "%s_open_socket" % (prefix,) in source
        assert "network_up" in source

    assert "#define RX_SLOTS 16" in udp
    assert '"klipper_udp_rx", 4096, NULL\n                            , 17' in udp
    assert "udp_port_get_status" in udp
    assert "wifi_get_status" in wifi
    assert "HELIX_WIFI_STATUS" in host

    print("PASS: ESP32 WiFi recovery recreates sockets and exposes evidence")


if __name__ == "__main__":
    main()
