// Native STM32F4/F7 RMII Ethernet MAC/DMA console
//
// FD-0001 doc 07 lists "RMII PHYs on STM32/ESP32" alongside W5500-class
// SPI parts as the wired-network options.  Unlike the W5500 (which runs
// the IP stack in silicon), the STM32's built-in MAC delivers only raw
// ethernet frames and needs a software IP/UDP stack above it.
//
// A full lwIP integration is deliberately not vendored.  This file provides
// the MAC/DMA half - configurable RMII pins and PHY reset, bounded MDIO,
// IEEE 802.3 auto-negotiation, link re-check, and descriptor rings - ending
// at a small documented seam:
//
//     rx frame  --> nano_udp_input()        (or lwip: ethernet_input)
//     tx frame  <-- eth_mac_emit()          (or lwip: low_level_output)
//
// The pluggable IP layer above that seam is generic/nano_udp.c, a
// minimal single-socket UDP/IP/ARP responder that IS functional and
// host-tested; swapping in lwIP is a matter of re-pointing the two seam
// calls.  The DMA descriptor OWN-bit handshakes are implemented; the
// Board-specific values are Kconfig inputs; the defaults match the common
// STM32F4 Nucleo-144 AF11 mapping.  Runtime electrical/timing validation
// still requires the selected board and PHY.
//
// COMPILE-CHECKED, NOT HARDWARE-VALIDATED.  See docs/Ethernet.md.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_MACH_STM32F4
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/irq.h" // irq_save
#include "board/gpio.h" // gpio_out_setup
#include "board/misc.h" // timer_read_time
#include "command.h" // command_encoder
#include "generic/armcm_timer.h" // udelay
#include "generic/acq_ring.h"
#include "generic/dma_resource.h"
#include "sched.h" // DECL_TASK
#include "internal.h" // gpio_peripheral, ETH
#include "generic/nano_udp.h" // nano_udp_input
#if CONFIG_GATEWAY_RMII
#include "generic/udp_gateway.h"
#else
#include "generic/udp_console.h" // udp_console_init
#endif

static uint8_t eth_mac_addr[6];
static uint8_t eth_psk[64];

#if CONFIG_MACH_STM32F4 || CONFIG_MACH_STM32F7

/****************************************************************
 * DMA descriptor rings
 ****************************************************************/

// Normal (non-enhanced) descriptor: 4 words.  Bit meanings from the
// reference manual DMA descriptor section.
struct eth_desc {
    volatile uint32_t status;   // RDES0/TDES0
    volatile uint32_t control;  // RDES1/TDES1 (buffer sizes / control)
    volatile uint32_t buf1;     // RDES2/TDES2
    volatile uint32_t buf2;     // RDES3/TDES3 (next in ring, chained)
};

#define ETH_DESC_OWN      0x80000000u // DMA owns the descriptor
#define ETH_RDES1_RCH     0x00004000u // receive end-of-ring chained
#define ETH_TDES0_TCH     0x00100000u // transmit second addr chained
#define ETH_TDES0_FS      0x10000000u // first segment
#define ETH_TDES0_LS      0x20000000u // last segment
#define ETH_TDES0_ES      0x00008000u // transmit error summary
#define ETH_RDES0_FL_MASK 0x3FFF0000u // frame length field
#define ETH_RDES0_FL_SHIFT 16
#define ETH_RDES0_ES      0x00008000u // error summary

#define ETH_RX_RING 4
#define ETH_TX_RING 2
#define ETH_BUF_SZ  1524 // one MTU + headroom, 4-byte aligned

static struct eth_desc *rx_ring, *tx_ring;
static uint8_t *rx_buf, *tx_buf;
static struct acq_ring rx_ready;
static struct task_wake eth_wake;
static uint8_t rx_publish_idx, tx_idx;
static uint8_t eth_ready, eth_link_up;
static uint8_t eth_link_speed100, eth_link_full;
static uint8_t eth_init_error;
static uint16_t eth_phy_id1, eth_phy_id2;
static uint32_t eth_link_poll_next;
static uint32_t eth_network_ms;
static uint32_t eth_rx_frames, eth_tx_frames, eth_rx_overruns;
static uint32_t eth_rx_errors, eth_tx_errors, eth_tx_busy;
static uint32_t eth_tx_underflows;
static uint32_t eth_dma_errors, eth_mdio_errors;
static uint32_t eth_irq_count, eth_link_transitions;

static void
eth_publish_ready(void)
{
    for (;;) {
        struct eth_desc *d = &rx_ring[rx_publish_idx];
        if (d->status & ETH_DESC_OWN)
            return;
        if (acq_ring_push(&rx_ready, rx_publish_idx)) {
            eth_rx_overruns++;
            return;
        }
        rx_publish_idx = (rx_publish_idx + 1) % ETH_RX_RING;
    }
}

void
ETH_IRQHandler(void)
{
    uint32_t status = ETH->DMASR;
    eth_irq_count++;
    ETH->DMASR = status;
    if (status & ETH_DMASR_FBES)
        eth_dma_errors++;
    if (status & ETH_DMASR_TUS)
        eth_tx_underflows++;
    if (status & ETH_DMASR_RBUS)
        eth_rx_overruns++;
    if (status & ETH_DMASR_RS)
        eth_publish_ready();
    sched_wake_task(&eth_wake);
#if CONFIG_GATEWAY_RMII
    if (status & ETH_DMASR_TS)
        udp_gateway_note_rx();
#endif
}

/****************************************************************
 * MDIO / PHY management
 ****************************************************************/

#if CONFIG_CLOCK_FREQ > 168000000
#define ETH_MDIO_CR (5u << 2) // HCLK/124, 168-216MHz
#elif CONFIG_CLOCK_FREQ > 150000000
#define ETH_MDIO_CR (4u << 2) // HCLK/102, 150-168MHz
#elif CONFIG_CLOCK_FREQ > 100000000
#define ETH_MDIO_CR (1u << 2) // HCLK/62, 100-150MHz
#elif CONFIG_CLOCK_FREQ > 60000000
#define ETH_MDIO_CR (0u << 2) // HCLK/42, 60-100MHz
#elif CONFIG_CLOCK_FREQ > 35000000
#define ETH_MDIO_CR (3u << 2) // HCLK/26, 35-60MHz
#else
#define ETH_MDIO_CR (2u << 2) // HCLK/16, 20-35MHz
#endif

static int
eth_mdio_wait(void)
{
    uint32_t guard = 1000000;
    while ((ETH->MACMIIAR & ETH_MACMIIAR_MB) && --guard)
        ;
    if (guard)
        return 0;
    eth_mdio_errors++;
    return -1;
}

static int
eth_mdio_read(uint8_t phy, uint8_t reg, uint16_t *result)
{
    uint32_t v = ((uint32_t)phy << 11) | ((uint32_t)reg << 6)
                 | ETH_MDIO_CR | ETH_MACMIIAR_MB;
    ETH->MACMIIAR = v;
    if (eth_mdio_wait())
        return -1;
    *result = (uint16_t)ETH->MACMIIDR;
    return 0;
}

static int
eth_mdio_write(uint8_t phy, uint8_t reg, uint16_t val)
{
    ETH->MACMIIDR = val;
    uint32_t v = ((uint32_t)phy << 11) | ((uint32_t)reg << 6)
                 | ETH_MDIO_CR | ETH_MACMIIAR_MW | ETH_MACMIIAR_MB;
    ETH->MACMIIAR = v;
    return eth_mdio_wait();
}

#define PHY_BMCR       0x00
#define PHY_BMSR       0x01
#define PHY_ID1        0x02
#define PHY_ID2        0x03
#define PHY_ANAR       0x04
#define PHY_ANLPAR     0x05
#define PHY_BMCR_RESET 0x8000
#define PHY_BMCR_ANEN  0x1000
#define PHY_BMCR_100M  0x2000
#define PHY_BMCR_FULL  0x0100
#define PHY_BMCR_REAN  0x0200
#define PHY_BMSR_LINK  0x0004
#define PHY_BMSR_ANOK  0x0020
#define PHY_AN_10H     0x0020
#define PHY_AN_10F     0x0040
#define PHY_AN_100H    0x0080
#define PHY_AN_100F    0x0100

static void
eth_phy_hard_reset(void)
{
    if (CONFIG_RMII_PHY_RESET_PIN < 0)
        return;
#if CONFIG_RMII_PHY_RESET_ACTIVE_LOW
    const uint8_t active = 0;
#else
    const uint8_t active = 1;
#endif
    struct gpio_out reset = gpio_out_setup(CONFIG_RMII_PHY_RESET_PIN, active);
    udelay(1000);
    gpio_out_write(reset, !active);
    // IEEE PHYs commonly require <=15ms from reset release to MDIO access.
    udelay(15000);
}

static int
eth_phy_start(void)
{
    uint16_t id1, id2, bmcr;
    if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ID1, &id1)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ID2, &id2)
        || !id1 || id1 == 0xffff || !id2 || id2 == 0xffff)
        return -1;
    eth_phy_id1 = id1;
    eth_phy_id2 = id2;
    if (eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_BMCR, PHY_BMCR_RESET))
        return -1;
    uint32_t guard = 1000000;
    do {
        if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMCR, &bmcr))
            return -1;
    } while ((bmcr & PHY_BMCR_RESET) && --guard);
    if (!guard)
        return -1;

    // Advertise all RMII 10/100 modes and restart IEEE auto-negotiation.
    if (eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_ANAR,
                       0x0001 | PHY_AN_10H | PHY_AN_10F
                       | PHY_AN_100H | PHY_AN_100F)
        || eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_BMCR,
                          PHY_BMCR_ANEN | PHY_BMCR_REAN))
        return -1;
    return 0;
}

static void
eth_phy_set_down(void)
{
    if (eth_link_up)
        eth_link_transitions++;
    eth_link_up = 0;
    eth_link_speed100 = 0;
    eth_link_full = 0;
}

static void
eth_phy_poll(void)
{
    uint16_t bmsr, dummy;
    // BMSR link is latch-low; the second read is the current state.
    if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMSR, &dummy)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMSR, &bmsr)
        || !(bmsr & PHY_BMSR_LINK)) {
        eth_phy_set_down();
        return;
    }
    if (!(bmsr & PHY_BMSR_ANOK)) {
        eth_phy_set_down();
        return;
    }

    uint16_t anar, anlpar;
    if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ANAR, &anar)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ANLPAR, &anlpar)) {
        eth_phy_set_down();
        return;
    }
    uint16_t common = anar & anlpar;
    // IEEE auto-negotiation priority is 100F, 100H, 10F, then 10H.  Derive
    // both fields from the same selected mode; independently testing the
    // speed and duplex bit sets can manufacture a mode the peer did not
    // advertise (for example 100H plus 10F becoming 100F).
    uint8_t speed100, full;
    if (common & PHY_AN_100F) {
        speed100 = full = 1;
    } else if (common & PHY_AN_100H) {
        speed100 = 1;
        full = 0;
    } else if (common & PHY_AN_10F) {
        speed100 = 0;
        full = 1;
    } else if (common & PHY_AN_10H) {
        speed100 = full = 0;
    } else {
        eth_phy_set_down();
        return;
    }
    uint32_t maccr = ETH->MACCR & ~(ETH_MACCR_FES | ETH_MACCR_DM);
    if (speed100)
        maccr |= ETH_MACCR_FES;
    if (full)
        maccr |= ETH_MACCR_DM;
    ETH->MACCR = maccr;
    eth_link_speed100 = speed100;
    eth_link_full = full;
    if (!eth_link_up)
        eth_link_transitions++;
    eth_link_up = 1;
}

/****************************************************************
 * RMII bring-up
 ****************************************************************/

static void
eth_rmii_pins(void)
{
    uint32_t af = GPIO_FUNCTION(11) | GPIO_HIGH_SPEED; // AF11 = ETH
    static const uint32_t pins[] = {
        CONFIG_RMII_REF_CLK_PIN,
        CONFIG_RMII_MDIO_PIN,
        CONFIG_RMII_MDC_PIN,
        CONFIG_RMII_CRS_DV_PIN,
        CONFIG_RMII_RXD0_PIN,
        CONFIG_RMII_RXD1_PIN,
        CONFIG_RMII_TX_EN_PIN,
        CONFIG_RMII_TXD0_PIN,
        CONFIG_RMII_TXD1_PIN,
    };
    for (unsigned i = 0; i < sizeof(pins) / sizeof(pins[0]); i++)
        gpio_peripheral(pins[i], af, 0);
}

static void
eth_ring_init(void)
{
    for (int i = 0; i < ETH_RX_RING; i++) {
        rx_ring[i].status = ETH_DESC_OWN;
        rx_ring[i].control = ETH_RDES1_RCH | ETH_BUF_SZ;
        rx_ring[i].buf1 = (uint32_t)(uintptr_t)&rx_buf[i * ETH_BUF_SZ];
        rx_ring[i].buf2 = (uint32_t)(uintptr_t)&rx_ring[(i + 1) % ETH_RX_RING];
    }
    for (int i = 0; i < ETH_TX_RING; i++) {
        tx_ring[i].status = ETH_TDES0_TCH; // CPU owns; chained
        tx_ring[i].control = 0;
        tx_ring[i].buf1 = (uint32_t)(uintptr_t)&tx_buf[i * ETH_BUF_SZ];
        tx_ring[i].buf2 = (uint32_t)(uintptr_t)&tx_ring[(i + 1) % ETH_TX_RING];
    }
    rx_publish_idx = tx_idx = 0;
    acq_ring_init(&rx_ready, ETH_RX_RING);
}

static int
eth_mac_init(void)
{
    // Clocks: MAC + MAC-TX + MAC-RX on AHB1
    RCC->AHB1ENR |= (RCC_AHB1ENR_ETHMACEN | RCC_AHB1ENR_ETHMACTXEN
                     | RCC_AHB1ENR_ETHMACRXEN);
    // Select RMII in SYSCFG (SYSCFG clock is enabled by the platform)
    RCC->APB2ENR |= RCC_APB2ENR_SYSCFGEN;
    SYSCFG->PMC |= SYSCFG_PMC_MII_RMII_SEL;

    eth_rmii_pins();
    eth_phy_hard_reset();

    // Reset the DMA and wait for it to clear
    ETH->DMABMR |= ETH_DMABMR_SR;
    uint32_t guard = 1000000;
    while ((ETH->DMABMR & ETH_DMABMR_SR) && --guard)
        ;
    if (!guard)
        return -1;

    if (eth_phy_start())
        return -1;

    // Program the station MAC address
    ETH->MACA0HR = ((uint32_t)eth_mac_addr[5] << 8) | eth_mac_addr[4];
    ETH->MACA0LR = ((uint32_t)eth_mac_addr[3] << 24)
                   | ((uint32_t)eth_mac_addr[2] << 16)
                   | ((uint32_t)eth_mac_addr[1] << 8) | eth_mac_addr[0];

    eth_ring_init();
    ETH->DMARDLAR = (uint32_t)(uintptr_t)rx_ring;
    ETH->DMATDLAR = (uint32_t)(uintptr_t)tx_ring;
    ETH->DMAIER = ETH_DMAIER_NISE | ETH_DMAIER_AISE
                  | ETH_DMAIER_RIE | ETH_DMAIER_TIE
                  | ETH_DMAIER_RBUIE | ETH_DMAIER_FBEIE;
    armcm_enable_irq(ETH_IRQHandler, ETH_IRQn, 2);

    // Start at the most common mode; eth_phy_poll() applies the negotiated
    // result as soon as link completes and again after reconnects.
    ETH->MACCR |= (ETH_MACCR_RE | ETH_MACCR_TE | ETH_MACCR_FES
                   | ETH_MACCR_DM);
    // DMA: start transmit + receive, store-and-forward both ways
    ETH->DMAOMR |= (ETH_DMAOMR_ST | ETH_DMAOMR_SR | ETH_DMAOMR_TSF
                    | ETH_DMAOMR_RSF);
    eth_ready = 1;
    eth_phy_poll();
    return 0;
}

/****************************************************************
 * Seam: raw frame in/out
 ****************************************************************/

// The MAC transmit hook handed to nano_udp (or lwIP's low_level_output)
static int
eth_mac_emit(const uint8_t *frame, uint32_t len)
{
    if (!eth_ready || !eth_link_up)
        return -1;
    struct eth_desc *d = &tx_ring[tx_idx];
    if (d->status & ETH_DESC_OWN) {
        eth_tx_busy++;
        return -1; // ring full - checked callers retain/retry
    }
    if (d->status & ETH_TDES0_ES)
        eth_tx_errors++;
    if (len > ETH_BUF_SZ)
        return -1;
    memcpy(&tx_buf[tx_idx * ETH_BUF_SZ], frame, len);
    d->control = len & 0x1FFF;
    __DMB(); // publish buffer + descriptor fields before DMA ownership
    d->status = ETH_TDES0_TCH | ETH_TDES0_FS | ETH_TDES0_LS | ETH_DESC_OWN;
    // A DMB orders the descriptor writes, but it does not guarantee that the
    // OWN write has reached the non-cacheable DMA arena before the peripheral
    // poll-demand write below.  On Cortex-M7 that can leave the first frame
    // pending until a later transmit happens to kick the DMA again.
    __DSB();
    eth_tx_frames++;
    tx_idx = (tx_idx + 1) % ETH_TX_RING;
    // Kick the transmit DMA out of any suspended state
    ETH->DMASR = ETH_DMASR_TBUS;
    ETH->DMATPDR = 0;
    __DSB();
    return 0;
}

// Poll the rx ring for CPU-owned descriptors and pump frames through
// the IP-layer seam.  This is the functional heart of the seam; the
// board-specific bring-up above is what remains to validate on silicon.
void
eth_mac_task(void)
{
    (void)sched_check_wake(&eth_wake);
    if (!eth_ready)
        return;
    uint32_t now = timer_read_time();
    if (timer_is_before(eth_link_poll_next, now)) {
        eth_link_poll_next = now + timer_from_us(250000);
        eth_phy_poll();
        eth_network_ms += 250;
        nano_udp_poll(eth_network_ms);
    }
    for (;;) {
        uint8_t index;
        irqstatus_t flag = irq_save();
        int ret = acq_ring_pop(&rx_ready, &index);
        irq_restore(flag);
        if (ret)
            break;
        struct eth_desc *d = &rx_ring[index];
        __DMB(); // DMA has returned status and the associated frame buffer
        if (!(d->status & ETH_RDES0_ES)) {
            uint32_t flen = (d->status & ETH_RDES0_FL_MASK)
                            >> ETH_RDES0_FL_SHIFT;
            if (flen > 4)
                nano_udp_input((const uint8_t *)(uintptr_t)d->buf1
                               , flen - 4 /* strip FCS */);
            eth_rx_frames++;
        } else
            eth_rx_errors++;
        __DMB();
        d->status = ETH_DESC_OWN; // hand the descriptor back to the DMA
        // Clear RX-buffer-unavailable and resume if the DMA suspended
        ETH->DMASR = ETH_DMASR_RBUS;
        ETH->DMARPDR = 0;
        flag = irq_save();
        eth_publish_ready();
        irq_restore(flag);
    }
}
DECL_TASK(eth_mac_task);

static void
eth_mac_address_init(void)
{
    // Stable locally-administered unicast MAC derived from the 96-bit STM32
    // unique ID. It avoids a fleet-wide baked-in address without requiring
    // a separate provisioning channel for a value that is not secret.
    const uint32_t *uid = (const uint32_t *)UID_BASE;
    uint32_t a = uid[0] ^ uid[2] ^ (uid[1] << 7) ^ (uid[1] >> 25);
    uint32_t b = uid[1] ^ (uid[0] << 13) ^ (uid[2] >> 11);
    eth_mac_addr[0] = 0x02;
    eth_mac_addr[1] = a >> 24;
    eth_mac_addr[2] = a >> 16;
    eth_mac_addr[3] = a >> 8;
    eth_mac_addr[4] = a;
    eth_mac_addr[5] = b ^ (b >> 8) ^ (b >> 16) ^ (b >> 24);
}

static uint32_t
eth_load_psk(void)
{
    uint32_t len = sizeof(CONFIG_RMII_PSK) - 1;
    if (len > sizeof(eth_psk))
        len = sizeof(eth_psk);
    if (len)
        memcpy(eth_psk, CONFIG_RMII_PSK, len);
    return len;
}

static int
eth_dma_storage_init(void)
{
    const uint8_t owner = 0xfe;
    uint8_t desc_caps = DMA_POOL_DESCRIPTOR | DMA_POOL_DMA_REACHABLE
                        | DMA_POOL_NONCACHEABLE;
    uint8_t buf_caps = DMA_POOL_BUFFER | DMA_POOL_DMA_REACHABLE
                       | DMA_POOL_NONCACHEABLE;
    if (dma_claim(DMA_RESOURCE_ETH_MAC, 0, owner)
        || dma_claim(DMA_RESOURCE_ETH_DMA, 0, owner))
        return -1;
    rx_ring = dma_pool_alloc(sizeof(*rx_ring) * ETH_RX_RING, 32,
                             desc_caps, owner);
    tx_ring = dma_pool_alloc(sizeof(*tx_ring) * ETH_TX_RING, 32,
                             desc_caps, owner);
    rx_buf = dma_pool_alloc(ETH_RX_RING * ETH_BUF_SZ, 32, buf_caps, owner);
    tx_buf = dma_pool_alloc(ETH_TX_RING * ETH_BUF_SZ, 32, buf_caps, owner);
    return !rx_ring || !tx_ring || !rx_buf || !tx_buf ? -1 : 0;
}

void
command_eth_mac_get_status(uint32_t *args)
{
    (void)args;
    struct dma_pool_status pool;
    uint32_t udp_rx, udp_slot_drops;
    uint8_t udp_queue_depth, udp_queue_highwater;
    dma_pool_get_status(&pool);
    nano_udp_get_io_stats(&udp_rx, &udp_slot_drops);
    nano_udp_get_queue_stats(&udp_queue_depth, &udp_queue_highwater);
    sendf("eth_mac_status ready=%c init_error=%c link=%c speed100=%c"
          " full_duplex=%c phy_addr=%c phy_id1=%hu phy_id2=%hu"
          " transitions=%u irq=%u rx=%u rx_errors=%u tx=%u tx_errors=%u"
          " tx_busy=%u tx_underflows=%u"
          " udp_rx=%u udp_slot_drops=%u udp_queue_depth=%c"
          " udp_queue_highwater=%c overruns=%u"
          " dma_errors=%u mdio_errors=%u"
          " ready_highwater=%c dma_pool=%hu dma_used=%hu",
          eth_ready, eth_init_error, eth_link_up, eth_link_speed100,
          eth_link_full, CONFIG_RMII_PHY_ADDR, eth_phy_id1, eth_phy_id2,
          eth_link_transitions, eth_irq_count, eth_rx_frames, eth_rx_errors,
          eth_tx_frames, eth_tx_errors, eth_tx_busy, eth_tx_underflows,
          udp_rx, udp_slot_drops, udp_queue_depth, udp_queue_highwater,
          eth_rx_overruns, eth_dma_errors, eth_mdio_errors,
          rx_ready.highwater, pool.size, pool.used);
}
DECL_COMMAND_FLAGS(command_eth_mac_get_status, HF_IN_SHUTDOWN,
                   "eth_mac_get_status");

static void
eth_network_reply(uint8_t result, uint8_t state)
{
    struct helix_network_params params;
    uint32_t epoch, generation, rejected, malformed, naks, retries;
    uint8_t dhcp_state;
    nano_udp_network_get_status(&params, &epoch, &generation, &dhcp_state,
                                &rejected, &malformed, &naks, &retries);
    sendf("eth_network_config result=%c state=%c mode=%c ip=%u netmask=%u"
          " gateway=%u port=%hu epoch=%u generation=%u dhcp_state=%c"
          " rejected=%u dhcp_malformed=%u dhcp_naks=%u dhcp_retries=%u",
          result, state, params.mode, params.ip,
          params.netmask, params.gateway, params.port, epoch, generation,
          dhcp_state, rejected, malformed, naks, retries);
}

void
command_eth_network_prepare(uint32_t *args)
{
    struct helix_network_params params = {
        .mode = args[1], .ip = args[2], .netmask = args[3],
        .gateway = args[4], .port = args[5],
    };
    int ret = nano_udp_network_prepare(args[0], &params);
    eth_network_reply(!!ret, ret ? 0 : 1);
}
DECL_COMMAND_FLAGS(command_eth_network_prepare, HF_IN_SHUTDOWN,
                   "eth_network_prepare epoch=%u mode=%c ip=%u netmask=%u"
                   " gateway=%u port=%hu");

void
command_eth_network_commit(uint32_t *args)
{
    int ret = nano_udp_network_commit(args[0]);
    eth_network_reply(!!ret, ret ? 0 : 2);
}
DECL_COMMAND_FLAGS(command_eth_network_commit, HF_IN_SHUTDOWN,
                   "eth_network_commit epoch=%u");

void
command_eth_network_abort(uint32_t *args)
{
    nano_udp_network_abort(args[0]);
    eth_network_reply(0, 0);
}
DECL_COMMAND_FLAGS(command_eth_network_abort, HF_IN_SHUTDOWN,
                   "eth_network_abort epoch=%u");

void
command_eth_network_get_status(uint32_t *args)
{
    (void)args;
    eth_network_reply(0, 0);
}
DECL_COMMAND_FLAGS(command_eth_network_get_status, HF_IN_SHUTDOWN,
                   "eth_network_get_status");

void
console_sendf(const struct command_encoder *ce, va_list args)
{
#if CONFIG_GATEWAY_RMII
    udp_gateway_sendf(ce, args);
#else
    udp_console_sendf(ce, args);
#endif
}

void *
console_receive_buffer(void)
{
#if CONFIG_GATEWAY_RMII
    return udp_gateway_get_rx_buf();
#else
    return udp_console_get_rx_buf();
#endif
}

void
eth_mac_setup(void)
{
    uint32_t psk_len = eth_load_psk();
#if !CONFIG_RMII_TRUST_NETWORK
    if (!psk_len) {
        // Fail closed: this transport is the console, so there is nowhere
        // safe to report a missing mandatory network credential.
        eth_init_error = 1;
        return;
    }
#endif
    if (eth_dma_storage_init()) {
        eth_init_error = 2;
        return;
    }
    eth_mac_address_init();
    nano_udp_setup(eth_mac_addr, CONFIG_RMII_IP, CONFIG_RMII_UDP_PORT,
                   eth_mac_emit,
#if CONFIG_GATEWAY_RMII
                   udp_gateway_note_rx
#else
                   udp_console_note_rx
#endif
                   );
    if (eth_mac_init() < 0) {
        eth_init_error = 3;
        return; // link down; nothing to report it on (this is the console)
    }
#if CONFIG_RMII_FEC_PAIR && !CONFIG_GATEWAY_RMII
    udp_console_set_fec_k(2);
#endif
#if CONFIG_GATEWAY_RMII
    udp_gateway_init(&nano_udp_ops, NULL, eth_psk, psk_len);
#else
    udp_console_init(&nano_udp_ops, NULL, eth_psk, psk_len);
#endif
}
DECL_INIT(eth_mac_setup);

#else // !(F4 || F7)

// WANT_ETHERNET_RMII is only selectable on the F4/F7 MAC implemented above.
#error "native RMII console selected on an unsupported STM32 MAC"

#endif
