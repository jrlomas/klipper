// Native STM32 RMII Ethernet MAC/DMA bring-up skeleton (design seam)
//
// FD-0001 doc 07 lists "RMII PHYs on STM32/ESP32" alongside W5500-class
// SPI parts as the wired-network options.  Unlike the W5500 (which runs
// the IP stack in silicon), the STM32's built-in MAC delivers only raw
// ethernet frames and needs a software IP/UDP stack above it.
//
// SCOPE (honest): a full lwIP integration is large and cannot be built
// or tested in this environment, so it is NOT vendored here.  What this
// file provides is the MAC/DMA half - RMII pin/clock bring-up and the
// descriptor-ring plumbing - terminating in a single, documented seam:
//
//     rx frame  --> nano_udp_input()        (or lwip: ethernet_input)
//     tx frame  <-- eth_mac_emit()          (or lwip: low_level_output)
//
// The pluggable IP layer above that seam is generic/nano_udp.c, a
// minimal single-socket UDP/IP/ARP responder that IS functional and
// host-tested; swapping in lwIP is a matter of re-pointing the two seam
// calls.  The DMA descriptor OWN-bit handshakes are implemented; the
// board-specific pieces that cannot be validated blind - the exact RMII
// pin map, the PHY address and its auto-negotiation, and the IP config
// source - are marked "TODO(board)".
//
// COMPILE-CHECKED, NOT HARDWARE-VALIDATED.  See docs/Ethernet.md.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_MACH_STM32F4
#include "command.h" // shutdown
#include "sched.h" // DECL_TASK
#include "internal.h" // gpio_peripheral, ETH
#include "generic/nano_udp.h" // nano_udp_input
#include "generic/udp_console.h" // udp_console_init

// TODO(board): these belong in Kconfig once a concrete RMII board is
// targeted; hard-coded here so the skeleton is self-contained.
#define ETH_STATIC_IP   0xC0A800FEu // 192.168.0.254
#define ETH_LISTEN_PORT 1234
#define ETH_PHY_ADDR    0
static const uint8_t eth_mac_addr[6] = { 0x02, 0x00, 0xC0, 0xA8, 0x00, 0xFE };

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
#define ETH_RDES0_FL_MASK 0x3FFF0000u // frame length field
#define ETH_RDES0_FL_SHIFT 16
#define ETH_RDES0_ES      0x00008000u // error summary

#define ETH_RX_RING 4
#define ETH_TX_RING 2
#define ETH_BUF_SZ  1524 // one MTU + headroom, 4-byte aligned

static struct eth_desc rx_ring[ETH_RX_RING] __attribute__((aligned(16)));
static struct eth_desc tx_ring[ETH_TX_RING] __attribute__((aligned(16)));
static uint8_t rx_buf[ETH_RX_RING][ETH_BUF_SZ] __attribute__((aligned(4)));
static uint8_t tx_buf[ETH_TX_RING][ETH_BUF_SZ] __attribute__((aligned(4)));
static uint8_t rx_idx, tx_idx;

/****************************************************************
 * MDIO / PHY management
 ****************************************************************/

static uint16_t
eth_mdio_read(uint8_t phy, uint8_t reg)
{
    // CR (clock range) field selects HCLK/x for the MDC; the exact
    // divider is HCLK-dependent - TODO(board) if HCLK is out of the
    // 100-150MHz band this default assumes.
    uint32_t v = ((uint32_t)phy << 11) | ((uint32_t)reg << 6)
                 | (4u << 2) /* CR */ | 0x1 /* MB: busy */;
    ETH->MACMIIAR = v;
    while (ETH->MACMIIAR & 0x1)
        ;
    return (uint16_t)ETH->MACMIIDR;
}

static void
eth_mdio_write(uint8_t phy, uint8_t reg, uint16_t val)
{
    ETH->MACMIIDR = val;
    uint32_t v = ((uint32_t)phy << 11) | ((uint32_t)reg << 6)
                 | (4u << 2) | 0x2 /* MW: write */ | 0x1 /* MB */;
    ETH->MACMIIAR = v;
    while (ETH->MACMIIAR & 0x1)
        ;
}

/****************************************************************
 * RMII bring-up
 ****************************************************************/

// TODO(board): default RMII pin map (Nucleo-144 F4/F7 style).  A real
// board binding turns this into a documented pin table.
static void
eth_rmii_pins(void)
{
    uint32_t af = GPIO_FUNCTION(11) | GPIO_HIGH_SPEED; // AF11 = ETH
    static const uint32_t pins[] = {
        GPIO('A', 1),  // RMII_REF_CLK
        GPIO('A', 2),  // RMII_MDIO
        GPIO('C', 1),  // RMII_MDC
        GPIO('A', 7),  // RMII_CRS_DV
        GPIO('C', 4),  // RMII_RXD0
        GPIO('C', 5),  // RMII_RXD1
        GPIO('B', 11), // RMII_TX_EN
        GPIO('B', 12), // RMII_TXD0
        GPIO('B', 13), // RMII_TXD1
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
        rx_ring[i].buf1 = (uint32_t)(uintptr_t)rx_buf[i];
        rx_ring[i].buf2 = (uint32_t)(uintptr_t)&rx_ring[(i + 1) % ETH_RX_RING];
    }
    for (int i = 0; i < ETH_TX_RING; i++) {
        tx_ring[i].status = ETH_TDES0_TCH; // CPU owns; chained
        tx_ring[i].control = 0;
        tx_ring[i].buf1 = (uint32_t)(uintptr_t)tx_buf[i];
        tx_ring[i].buf2 = (uint32_t)(uintptr_t)&tx_ring[(i + 1) % ETH_TX_RING];
    }
    rx_idx = tx_idx = 0;
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

    // Reset the DMA and wait for it to clear
    ETH->DMABMR |= ETH_DMABMR_SR;
    uint32_t guard = 1000000;
    while ((ETH->DMABMR & ETH_DMABMR_SR) && --guard)
        ;
    if (!guard)
        return -1;

    // TODO(board): PHY reset + auto-negotiation wait.  The PHY address
    // and register semantics are vendor-specific; here we only kick a
    // soft reset and assume 100M full-duplex once RMII is selected.
    eth_mdio_write(ETH_PHY_ADDR, 0x00, 0x8000); // BMCR reset
    guard = 1000000;
    while ((eth_mdio_read(ETH_PHY_ADDR, 0x00) & 0x8000) && --guard)
        ;

    // Program the station MAC address
    ETH->MACA0HR = ((uint32_t)eth_mac_addr[5] << 8) | eth_mac_addr[4];
    ETH->MACA0LR = ((uint32_t)eth_mac_addr[3] << 24)
                   | ((uint32_t)eth_mac_addr[2] << 16)
                   | ((uint32_t)eth_mac_addr[1] << 8) | eth_mac_addr[0];

    eth_ring_init();
    ETH->DMARDLAR = (uint32_t)(uintptr_t)rx_ring;
    ETH->DMATDLAR = (uint32_t)(uintptr_t)tx_ring;

    // MAC: receiver + transmitter enable, 100Mbit, full duplex
    ETH->MACCR |= (ETH_MACCR_RE | ETH_MACCR_TE | ETH_MACCR_FES
                   | ETH_MACCR_DM);
    // DMA: start transmit + receive, store-and-forward both ways
    ETH->DMAOMR |= (ETH_DMAOMR_ST | ETH_DMAOMR_SR | ETH_DMAOMR_TSF
                    | ETH_DMAOMR_RSF);
    return 0;
}

/****************************************************************
 * Seam: raw frame in/out
 ****************************************************************/

// The MAC transmit hook handed to nano_udp (or lwIP's low_level_output)
static void
eth_mac_emit(const uint8_t *frame, uint32_t len)
{
    struct eth_desc *d = &tx_ring[tx_idx];
    if (d->status & ETH_DESC_OWN)
        return; // ring full - drop; the frame layer's ARQ recovers
    if (len > ETH_BUF_SZ)
        return;
    memcpy(tx_buf[tx_idx], frame, len);
    d->control = len & 0x1FFF;
    d->status = ETH_TDES0_TCH | ETH_TDES0_FS | ETH_TDES0_LS | ETH_DESC_OWN;
    tx_idx = (tx_idx + 1) % ETH_TX_RING;
    // Kick the transmit DMA out of any suspended state
    ETH->DMASR = ETH_DMASR_TBUS;
    ETH->DMATPDR = 0;
}

// Poll the rx ring for CPU-owned descriptors and pump frames through
// the IP-layer seam.  This is the functional heart of the seam; the
// board-specific bring-up above is what remains to validate on silicon.
void
eth_mac_task(void)
{
    for (;;) {
        struct eth_desc *d = &rx_ring[rx_idx];
        if (d->status & ETH_DESC_OWN)
            break; // DMA still owns it - nothing ready
        if (!(d->status & ETH_RDES0_ES)) {
            uint32_t flen = (d->status & ETH_RDES0_FL_MASK)
                            >> ETH_RDES0_FL_SHIFT;
            if (flen > 4)
                nano_udp_input((const uint8_t *)(uintptr_t)d->buf1
                               , flen - 4 /* strip FCS */);
        }
        d->status = ETH_DESC_OWN; // hand the descriptor back to the DMA
        rx_idx = (rx_idx + 1) % ETH_RX_RING;
        // Clear RX-buffer-unavailable and resume if the DMA suspended
        ETH->DMASR = ETH_DMASR_RBUS;
        ETH->DMARPDR = 0;
    }
}
DECL_TASK(eth_mac_task);

void
eth_mac_setup(void)
{
    nano_udp_setup(eth_mac_addr, ETH_STATIC_IP, ETH_LISTEN_PORT
                   , eth_mac_emit);
    if (eth_mac_init() < 0)
        return; // link down; nothing to report it on (this is the console)
    // No PSK plumbing in the skeleton: bring the datagram layer up in
    // the explicit trust_network mode.  TODO(board): source a PSK the
    // way the esp32/w5500 paths do before any real deployment.
    udp_console_init(&nano_udp_ops, NULL, NULL, 0);
}
DECL_INIT(eth_mac_setup);

#else // !(F4 || F7)

// The RMII register layout differs on other families (notably the H7's
// redesigned MAC); the descriptor-ring seam above is written against the
// F4/F7 MAC.  On other targets this remains a documented stub so the
// Kconfig option still compiles.
#warning "eth_mac.c: RMII MAC body is STM32F4/F7 only; other families TODO"
void
eth_mac_setup(void)
{
}
DECL_INIT(eth_mac_setup);

#endif
