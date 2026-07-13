// The network modem task - the core-0 side of the "IDF as modem"
// architecture (FD-0001 doc 12 stage 3)
//
// In the modem architecture this is the only place the console data
// path touches IDF/lwIP: a core-0 task owns the UDP socket and
// shuttles *sealed* datagrams between the air and the shared-memory
// rings (shmem_ring.h).  It re-homes the socket half of the
// component architecture's udp_port.c; the protocol half (HMAC
// verification, sequencing, frame dispatch) runs on the bare klipper
// core behind the ring (shmem_console.c + the unchanged generic
// glue).  The modem cannot forge traffic core 1 will accept, and it
// only ever transmits to the peer core 1 has authenticated - the
// address blob travels with each rx record and comes back attached to
// each tx record after core 1 has authenticated it.
//
// The task alternates a 1ms-timeout recvfrom with a tx-ring drain,
// so board->host latency is bounded by ~1ms + the console's own 2ms
// batching; host->board delivery is bounded by the recvfrom wakeup
// itself.  (A cross-core doorbell interrupt could tighten the tx
// path later; the intention-queue design makes 1ms irrelevant.)
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "freertos/FreeRTOS.h" // xTaskCreatePinnedToCore
#include "freertos/task.h"
#include "esp_log.h" // ESP_LOGE
#include "esp_system.h" // esp_restart
#include "lwip/sockets.h" // socket
#include "generic/udp_datagram.h" // UDPDG_DATAGRAM_MAX
#include "internal.h" // esp32_modem_start
#include "shmem_ring.h" // esp32_shmem

static const char *TAG = "klipper_modem";

static int modem_sock = -1;

// The opaque address blob in each rx ring record is a sockaddr_in
_Static_assert(sizeof(struct sockaddr_in) <= SHMEM_ADDR_MAX
               , "sockaddr_in must fit the ring's address blob");

// Report (once) a fault parked on the bare core - the only
// diagnostics channel core 1's fatal-exception handler has
static void
modem_check_core1_fault(void)
{
    static uint8_t reported;
    if (reported || !__atomic_load_n(&esp32_core1_fault[0], __ATOMIC_ACQUIRE))
        return;
    reported = 1;
    ESP_LOGE(TAG, "core 1 FAULT parked: cause=0x%x epc=0x%x vaddr=0x%x"
             , (unsigned)esp32_core1_fault[1]
             , (unsigned)esp32_core1_fault[2]
             , (unsigned)esp32_core1_fault[3]);
}

static void
modem_task(void *arg)
{
    uint8_t buf[SHMEM_ADDR_MAX + UDPDG_DATAGRAM_MAX];
    for (;;) {
        // Air -> ring (1ms poll granularity via SO_RCVTIMEO)
        struct sockaddr_in src;
        socklen_t sl = sizeof(src);
        int ret = recvfrom(modem_sock, buf, UDPDG_DATAGRAM_MAX, 0
                           , (struct sockaddr *)&src, &sl);
        if (ret > 0) {
            uint8_t addr[SHMEM_ADDR_MAX];
            memset(addr, 0, sizeof(addr));
            memcpy(addr, &src, sizeof(src));
            // Ring full -> drop; the frame layer's ARQ recovers
            shmem_ring_push(&esp32_shmem.rx, addr, sizeof(addr), buf, ret);
        }

        // Ring -> air. Core 1 attaches the authenticated destination to
        // ordinary traffic, or the uncommitted candidate to ServerHello.
        while (shmem_ring_readable(&esp32_shmem.tx)) {
            int32_t len = shmem_ring_pop(&esp32_shmem.tx, buf, sizeof(buf));
            struct sockaddr_in peer;
            if (len > SHMEM_ADDR_MAX) {
                memcpy(&peer, buf, sizeof(peer));
                sendto(modem_sock, buf + SHMEM_ADDR_MAX,
                       len - SHMEM_ADDR_MAX, 0
                       , (const struct sockaddr *)&peer, sizeof(peer));
            }
        }

        modem_check_core1_fault();
        if (__atomic_load_n(&esp32_shmem.reset_request, __ATOMIC_ACQUIRE)) {
            ESP_LOGW(TAG, "core 1 requested chip reset");
            esp_restart();
        }
    }
}

int
esp32_modem_start(uint16_t port)
{
    modem_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (modem_sock < 0) {
        ESP_LOGE(TAG, "socket() failed");
        return -1;
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(modem_sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind(%d) failed", port);
        return -1;
    }
    // Bound the rx wait so the tx ring drains at >= 1kHz
    struct timeval tv = { .tv_sec = 0, .tv_usec = 1000 };
    setsockopt(modem_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    xTaskCreatePinnedToCore(modem_task, "klipper_modem", 4096, NULL
                            , 10, NULL, 0);
    ESP_LOGI(TAG, "modem shuttling datagrams on udp port %d", port);
    return 0;
}
