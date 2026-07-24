// lwIP UDP socket binding for the datagram console on ESP32
//
// The generic glue in src/generic/udp_console.c does all protocol
// work; this file only moves datagrams: a blocking-recvfrom task
// pinned to core 0 (the WiFi/lwIP core) feeds a single-producer/
// single-consumer slot ring that the klipper task on core 1 drains
// through the udp_console_ops.recv callback.  Transmit crosses a second
// bounded SPSC ring to a core-0 task.  The motion/command core must never
// enter lwIP: WiFi driver backpressure can block sendto() while the radio
// and ICMP tasks on core 0 remain healthy.
// The peer address is only latched after a datagram passes HMAC
// authentication (ops.rx_accepted).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <errno.h> // errno
#include <string.h> // memcpy
#include "freertos/FreeRTOS.h" // xTaskCreatePinnedToCore
#include "freertos/task.h" // vTaskDelay
#include "esp_log.h" // ESP_LOGE
#include "lwip/sockets.h" // socket
#include "command.h" // DECL_COMMAND_FLAGS
#include "generic/udp_console.h" // udp_console_init
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX
#include "internal.h" // esp32_udp_port_setup

static const char *TAG = "klipper_udp";

static int udp_sock = -1;
static uint16_t udp_port;
static volatile uint8_t network_up;
static uint32_t socket_opens, socket_failures;
static uint32_t rx_packets, rx_ring_drops, recv_errors;
static uint32_t tx_packets, tx_ring_drops, tx_ring_highwater;
static uint32_t send_errors, send_transient_drops;

// Receive ring: written by the rx task (core 0), drained by the
// klipper task (core 1).  Slot data is fully written before the
// producer index advances.
#define RX_SLOTS 16
static struct rx_slot {
    uint16_t len;
    struct sockaddr_in src;
    uint8_t data[UDPDG_DATAGRAM_MAX];
} rx_ring[RX_SLOTS];
static uint32_t rx_head, rx_tail;

// Core 1 is the only producer and the core-0 TX task is the only consumer.
// A full queue drops the datagram; Helix ARQ will retransmit the underlying
// frame without ever stalling motion or command dispatch.
#define TX_SLOTS 16
static struct tx_slot {
    uint16_t len;
    struct sockaddr_in dst;
    uint8_t data[UDPDG_DATAGRAM_MAX];
} tx_ring[TX_SLOTS];
static uint32_t tx_head, tx_tail;
static TaskHandle_t udp_tx_task_handle;

// Source of the last datagram handed to the console, and the last
// source that passed authentication (only the latter is replied to)
static struct sockaddr_in rx_candidate, tx_peer;
static volatile uint8_t have_peer;

static uint32_t
ring_depth(uint32_t head, uint32_t tail, uint32_t slots)
{
    return head >= tail ? head - tail : slots - tail + head;
}

static void
note_tx_highwater(uint32_t depth)
{
    uint32_t old = __atomic_load_n(&tx_ring_highwater, __ATOMIC_RELAXED);
    while (depth > old
           && !__atomic_compare_exchange_n(
               &tx_ring_highwater, &old, depth, 0,
               __ATOMIC_RELAXED, __ATOMIC_RELAXED))
        ;
}

static void
udp_close_socket(void)
{
    int sock = __atomic_exchange_n(&udp_sock, -1, __ATOMIC_ACQ_REL);
    if (sock >= 0)
        close(sock);
}

static int
udp_open_socket(void)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        __atomic_add_fetch(&socket_failures, 1, __ATOMIC_RELAXED);
        return -1;
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(udp_port);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(sock);
        __atomic_add_fetch(&socket_failures, 1, __ATOMIC_RELAXED);
        return -1;
    }
    // Periodically return to the owner task so a WiFi disconnect can close
    // and recreate the socket.  ESP-IDF explicitly invalidates application
    // sockets on WIFI_EVENT_STA_DISCONNECTED.
    struct timeval tv = { .tv_sec = 0, .tv_usec = 100000 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    __atomic_store_n(&udp_sock, sock, __ATOMIC_RELEASE);
    uint32_t opens =
        __atomic_add_fetch(&socket_opens, 1, __ATOMIC_RELAXED);
    ESP_LOGI(TAG, "datagram socket open on udp port %u (open #%u)",
             (unsigned)udp_port, (unsigned)opens);
    return 0;
}

// Receive loop (core 0). The task owns socket create/close, so WiFi event
// callbacks only publish link state and never race a blocking recvfrom().
static void
udp_rx_task(void *arg)
{
    for (;;) {
        if (!__atomic_load_n(&network_up, __ATOMIC_ACQUIRE)) {
            udp_close_socket();
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        int sock = __atomic_load_n(&udp_sock, __ATOMIC_ACQUIRE);
        if (sock < 0) {
            if (udp_open_socket() < 0)
                vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        uint32_t head = rx_head; // producer-owned
        struct rx_slot *s = &rx_ring[head];
        socklen_t sl = sizeof(s->src);
        int ret = recvfrom(sock, s->data, sizeof(s->data), 0
                           , (struct sockaddr *)&s->src, &sl);
        if (ret <= 0) {
            if (ret < 0 && errno != EAGAIN && errno != EWOULDBLOCK
                && errno != ETIMEDOUT) {
                __atomic_add_fetch(&recv_errors, 1, __ATOMIC_RELAXED);
                udp_close_socket();
            }
            continue;
        }
        __atomic_add_fetch(&rx_packets, 1, __ATOMIC_RELAXED);
        uint32_t next = (head + 1) % RX_SLOTS;
        if (next == __atomic_load_n(&rx_tail, __ATOMIC_ACQUIRE)) {
            // Ring full - drop; the frame layer's ARQ recovers
            __atomic_add_fetch(&rx_ring_drops, 1, __ATOMIC_RELAXED);
            continue;
        }
        s->len = ret;
        // Publish the complete slot to core 1. The acquire in recv pairs
        // with this release so data and source precede the index update.
        __atomic_store_n(&rx_head, next, __ATOMIC_RELEASE);
        udp_console_note_rx();
        board_wake_main();
    }
}

static int32_t
udp_port_recv(void *ctx, uint8_t *buf, uint32_t cap)
{
    (void)ctx;
    uint32_t tail = rx_tail; // consumer-owned
    if (tail == __atomic_load_n(&rx_head, __ATOMIC_ACQUIRE))
        return 0;
    struct rx_slot *s = &rx_ring[tail];
    uint32_t len = s->len;
    if (len > cap)
        len = cap;
    memcpy(buf, s->data, len);
    rx_candidate = s->src;
    // Release the slot only after core 1 has copied it and its source.
    __atomic_store_n(&rx_tail, (tail + 1) % RX_SLOTS, __ATOMIC_RELEASE);
    return len;
}

static void
udp_port_rx_accepted(void *ctx)
{
    (void)ctx;
    tx_peer = rx_candidate;
    have_peer = 1;
}

static void
udp_queue_send(const struct sockaddr_in *dst, const uint8_t *data,
               uint32_t len)
{
    if (len > UDPDG_DATAGRAM_MAX) {
        __atomic_add_fetch(&send_errors, 1, __ATOMIC_RELAXED);
        return;
    }
    uint32_t head = tx_head;
    uint32_t next = (head + 1) % TX_SLOTS;
    uint32_t tail = __atomic_load_n(&tx_tail, __ATOMIC_ACQUIRE);
    if (next == tail) {
        __atomic_add_fetch(&tx_ring_drops, 1, __ATOMIC_RELAXED);
        return;
    }
    struct tx_slot *s = &tx_ring[head];
    s->len = len;
    s->dst = *dst;
    memcpy(s->data, data, len);
    __atomic_store_n(&tx_head, next, __ATOMIC_RELEASE);
    note_tx_highwater(ring_depth(next, tail, TX_SLOTS));
    TaskHandle_t task =
        __atomic_load_n(&udp_tx_task_handle, __ATOMIC_ACQUIRE);
    if (task)
        xTaskNotifyGive(task);
}

static void
udp_tx_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t tail = tx_tail;
        while (tail != __atomic_load_n(&tx_head, __ATOMIC_ACQUIRE)) {
            struct tx_slot *s = &tx_ring[tail];
            int sock = __atomic_load_n(&udp_sock, __ATOMIC_ACQUIRE);
            int ret = -1;
            int err = ENETDOWN;
            if (sock >= 0)
                ret = sendto(sock, s->data, s->len, MSG_DONTWAIT,
                             (const struct sockaddr *)&s->dst,
                             sizeof(s->dst));
            if (ret < 0) {
                if (sock >= 0)
                    err = errno;
                __atomic_add_fetch(&send_errors, 1, __ATOMIC_RELAXED);
                if (err == EAGAIN || err == EWOULDBLOCK || err == ENOMEM)
                    __atomic_add_fetch(&send_transient_drops, 1,
                                       __ATOMIC_RELAXED);
            } else {
                __atomic_add_fetch(&tx_packets, 1, __ATOMIC_RELAXED);
            }
            tail = (tail + 1) % TX_SLOTS;
            __atomic_store_n(&tx_tail, tail, __ATOMIC_RELEASE);
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
    }
}

static void
udp_port_send(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    if (have_peer)
        udp_queue_send(&tx_peer, data, len);
}

static void
udp_port_send_candidate(void *ctx, const uint8_t *data, uint32_t len)
{
    (void)ctx;
    udp_queue_send(&rx_candidate, data, len);
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
    udp_port = port;
#if CONFIG_KLIPPER_FEC_PAIR
    udp_console_set_fec_k(2);
#endif
#if CONFIG_KLIPPER_DATAGRAM_SESSION
    udp_console_set_session_tx_copies(CONFIG_KLIPPER_SESSION_TX_COPIES);
#endif
    udp_console_init(&esp32_udp_ops, NULL, psk, psk_len);
    // Keep this immediately below lwIP's priority (18). Espressif warns
    // that a network-using task must not outrank lwIP, while the previous
    // priority 10 allowed avoidable socket queue buildup under motion load.
    xTaskCreatePinnedToCore(udp_rx_task, "klipper_udp_rx", 4096, NULL
                            , 17, NULL, 0);
    xTaskCreatePinnedToCore(udp_tx_task, "klipper_udp_tx", 4096, NULL
                            , 17, &udp_tx_task_handle, 0);
    ESP_LOGI(TAG, "datagram console on udp port %d (auth=%s)", port
             , psk_len ? "on" : "TRUSTED NETWORK");
    return 0;
}

void
esp32_udp_port_network_changed(uint8_t up)
{
    __atomic_store_n(&network_up, !!up, __ATOMIC_RELEASE);
}

void
command_udp_port_get_status(uint32_t *args)
{
    (void)args;
    sendf("udp_port_status network_up=%u socket_up=%u socket_opens=%u"
          " socket_failures=%u rx_packets=%u ring_drops=%u"
          " recv_errors=%u tx_packets=%u send_errors=%u"
          " tx_ring_drops=%u tx_ring_highwater=%u"
          " send_transient_drops=%u",
          (uint32_t)__atomic_load_n(&network_up, __ATOMIC_ACQUIRE),
          (uint32_t)(__atomic_load_n(&udp_sock, __ATOMIC_ACQUIRE) >= 0),
          __atomic_load_n(&socket_opens, __ATOMIC_RELAXED),
          __atomic_load_n(&socket_failures, __ATOMIC_RELAXED),
          __atomic_load_n(&rx_packets, __ATOMIC_RELAXED),
          __atomic_load_n(&rx_ring_drops, __ATOMIC_RELAXED),
          __atomic_load_n(&recv_errors, __ATOMIC_RELAXED),
          __atomic_load_n(&tx_packets, __ATOMIC_RELAXED),
          __atomic_load_n(&send_errors, __ATOMIC_RELAXED),
          __atomic_load_n(&tx_ring_drops, __ATOMIC_RELAXED),
          __atomic_load_n(&tx_ring_highwater, __ATOMIC_RELAXED),
          __atomic_load_n(&send_transient_drops, __ATOMIC_RELAXED));
}
DECL_COMMAND_FLAGS(command_udp_port_get_status, HF_IN_SHUTDOWN,
                   "udp_port_get_status");
