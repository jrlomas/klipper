// lwIP UDP socket binding for the datagram console on ESP32
//
// The generic glue in src/generic/udp_console.c does all protocol
// work; this file only moves datagrams: a blocking-recvfrom task
// pinned to core 0 (the WiFi/lwIP core) feeds a single-producer/
// single-consumer slot ring that the klipper task on core 1 drains
// through the udp_console_ops.recv callback.  Transmit is a direct
// sendto() from the klipper task (lwIP sockets are thread safe).
// The peer address is only latched after a datagram passes HMAC
// authentication (ops.rx_accepted).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "freertos/FreeRTOS.h" // xTaskCreatePinnedToCore
#include "freertos/task.h" // vTaskDelay
#include "esp_log.h" // ESP_LOGE
#include "lwip/sockets.h" // socket
#include "generic/udp_console.h" // udp_console_init
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX
#include "internal.h" // esp32_udp_port_setup

static const char *TAG = "klipper_udp";

static int udp_sock = -1;

// Receive ring: written by the rx task (core 0), drained by the
// klipper task (core 1).  Slot data is fully written before the
// producer index advances.
#define RX_SLOTS 6
static struct rx_slot {
    uint16_t len;
    struct sockaddr_in src;
    uint8_t data[UDPDG_DATAGRAM_MAX];
} rx_ring[RX_SLOTS];
static volatile uint8_t rx_head, rx_tail;

// Source of the last datagram handed to the console, and the last
// source that passed authentication (only the latter is replied to)
static struct sockaddr_in rx_candidate, tx_peer;
static volatile uint8_t have_peer;

// Blocking receive loop (core 0)
static void
udp_rx_task(void *arg)
{
    for (;;) {
        struct rx_slot *s = &rx_ring[rx_head];
        socklen_t sl = sizeof(s->src);
        int ret = recvfrom(udp_sock, s->data, sizeof(s->data), 0
                           , (struct sockaddr *)&s->src, &sl);
        if (ret <= 0) {
            vTaskDelay(1);
            continue;
        }
        uint8_t next = (rx_head + 1) % RX_SLOTS;
        if (next == rx_tail)
            // Ring full - drop; the frame layer's ARQ recovers
            continue;
        s->len = ret;
        rx_head = next;
        udp_console_note_rx();
        board_wake_main();
    }
}

static int32_t
udp_port_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    if (rx_tail == rx_head)
        return 0;
    struct rx_slot *s = &rx_ring[rx_tail];
    uint32_t len = s->len;
    if (len > cap)
        len = cap;
    memcpy(buf, s->data, len);
    rx_candidate = s->src;
    rx_tail = (rx_tail + 1) % RX_SLOTS;
    return len;
}

static void
udp_port_rx_accepted(void *ctx)
{
    tx_peer = rx_candidate;
    have_peer = 1;
}

static void
udp_port_send(void *ctx, const uint8_t *data, uint32_t len)
{
    if (!have_peer)
        return;
    sendto(udp_sock, data, len, 0, (const struct sockaddr *)&tx_peer
           , sizeof(tx_peer));
}

static void
udp_port_send_candidate(void *ctx, const uint8_t *data, uint32_t len)
{
    sendto(udp_sock, data, len, 0,
           (const struct sockaddr *)&rx_candidate, sizeof(rx_candidate));
}

static const struct udp_console_ops esp32_udp_ops = {
    .recv = udp_port_recv,
    .send = udp_port_send,
    .send_candidate = udp_port_send_candidate,
    .rx_accepted = udp_port_rx_accepted,
};

int
esp32_udp_port_setup(uint16_t port, const uint8_t *psk, uint32_t psk_len)
{
    udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (udp_sock < 0) {
        ESP_LOGE(TAG, "socket() failed");
        return -1;
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(udp_sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind(%d) failed", port);
        return -1;
    }
    udp_console_init(&esp32_udp_ops, NULL, psk, psk_len);
    xTaskCreatePinnedToCore(udp_rx_task, "klipper_udp_rx", 4096, NULL
                            , 10, NULL, 0);
    ESP_LOGI(TAG, "datagram console on udp port %d (auth=%s)", port
             , psk_len ? "on" : "TRUSTED NETWORK");
    return 0;
}
