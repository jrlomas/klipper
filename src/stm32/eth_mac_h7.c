// Native STM32H723 RMII Ethernet MAC/DMA transport
//
// The H7 Ethernet block is not register-compatible with the F4/F7 MAC.  It
// uses four-word normal descriptors, explicit ring lengths and absolute tail
// pointers.  Descriptors and payloads are allocated from the repository's
// fixed DMA arena, which is linked into AXI SRAM and mapped non-cacheable by
// the MPU before D-cache is enabled.
//
// This implementation follows RM0468 section 63 and ST's STM32H7 HAL
// descriptor sequencing, but keeps the small nano_udp seam used by Helix.
// It is compile-qualified; electrical/link qualification awaits H723 hardware.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h"
#include "board/armcm_boot.h" // armcm_enable_irq
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // command_encoder
#include "generic/acq_ring.h"
#include "generic/armcm_timer.h" // udelay
#include "generic/dma_resource.h"
#include "generic/nano_udp.h"
#include "internal.h" // gpio_peripheral, ETH
#include "sched.h" // DECL_TASK
#if CONFIG_GATEWAY_RMII
#include "generic/udp_gateway.h"
#else
#include "generic/udp_console.h"
#endif

#if !CONFIG_MACH_STM32H723
#error "STM32H7 Ethernet backend currently supports STM32H723 only"
#endif

#define ETH_RX_RING 4
#define ETH_TX_RING 4
#define ETH_BUF_SZ  1524

// STM32H7 normal descriptors are exactly four words.  Do not append software
// fields: DMACCR.DSL remains zero, so the DMA advances by 16 bytes.
struct eth_h7_desc {
    volatile uint32_t desc0;
    volatile uint32_t desc1;
    volatile uint32_t desc2;
    volatile uint32_t desc3;
};

#define H7_RX_OWN       0x80000000u
#define H7_RX_IOC       0x40000000u
#define H7_RX_FD        0x20000000u
#define H7_RX_LD        0x10000000u
#define H7_RX_BUF1V     0x01000000u
#define H7_RX_ES        0x00008000u
#define H7_RX_PL_MASK   0x00007fffu

#define H7_TX_OWN       0x80000000u
#define H7_TX_FD        0x20000000u
#define H7_TX_LD        0x10000000u
#define H7_TX_IOC       0x80000000u
#define H7_TX_B1L_MASK  0x00003fffu
#define H7_TX_FL_MASK   0x00007fffu
#define H7_TX_ES        0x00008000u

static struct eth_h7_desc *rx_ring, *tx_ring;
static uint8_t *rx_buf, *tx_buf;
static struct acq_ring rx_ready;
static struct task_wake eth_wake;
static uint8_t eth_mac_addr[6], eth_psk[64];
static uint8_t rx_publish_idx, tx_idx;
static uint8_t eth_ready, eth_link_up;
static uint32_t eth_link_poll_next;
static uint32_t eth_rx_frames, eth_tx_frames, eth_rx_overruns;
static uint32_t eth_dma_errors, eth_tx_errors;

static void
eth_publish_ready(void)
{
    for (;;) {
        struct eth_h7_desc *d = &rx_ring[rx_publish_idx];
        if (d->desc3 & H7_RX_OWN)
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
    uint32_t status = ETH->DMACSR;
    ETH->DMACSR = status; // W1C all causes observed by this ISR
    if (status & ETH_DMACSR_FBE)
        eth_dma_errors++;
    if (status & ETH_DMACSR_RBU)
        eth_rx_overruns++;
    if (status & ETH_DMACSR_RI)
        eth_publish_ready();
    sched_wake_task(&eth_wake);
#if CONFIG_GATEWAY_RMII
    // The gateway may have retained a sealed datagram while the Tx ring was
    // full.  A Tx completion must wake it just like newly received data.
    if (status & ETH_DMACSR_TI)
        udp_gateway_note_rx();
#endif
}

/****************************************************************
 * MDIO and PHY management
 ****************************************************************/

// H723's clock setup runs HCLK at CONFIG_CLOCK_FREQ/2 (260 MHz normally).
#define H7_HCLK (CONFIG_CLOCK_FREQ / 2)
#if H7_HCLK >= 250000000
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV124
#elif H7_HCLK >= 150000000
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV102
#elif H7_HCLK >= 100000000
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV62
#elif H7_HCLK >= 60000000
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV42
#elif H7_HCLK >= 35000000
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV26
#else
#define ETH_MDIO_CR ETH_MACMDIOAR_CR_DIV16
#endif

static int
eth_mdio_wait(void)
{
    uint32_t guard = 1000000;
    while ((ETH->MACMDIOAR & ETH_MACMDIOAR_MB) && --guard)
        ;
    return guard ? 0 : -1;
}

static int
eth_mdio_read(uint8_t phy, uint8_t reg, uint16_t *result)
{
    ETH->MACMDIOAR = ((uint32_t)phy << ETH_MACMDIOAR_PA_Pos)
        | ((uint32_t)reg << ETH_MACMDIOAR_RDA_Pos) | ETH_MDIO_CR
        | ETH_MACMDIOAR_MOC_RD | ETH_MACMDIOAR_MB;
    if (eth_mdio_wait())
        return -1;
    *result = (uint16_t)ETH->MACMDIODR;
    return 0;
}

static int
eth_mdio_write(uint8_t phy, uint8_t reg, uint16_t value)
{
    ETH->MACMDIODR = value;
    ETH->MACMDIOAR = ((uint32_t)phy << ETH_MACMDIOAR_PA_Pos)
        | ((uint32_t)reg << ETH_MACMDIOAR_RDA_Pos) | ETH_MDIO_CR
        | ETH_MACMDIOAR_MOC_WR | ETH_MACMDIOAR_MB;
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
    if (eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_BMCR, PHY_BMCR_RESET))
        return -1;
    uint32_t guard = 1000000;
    do {
        if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMCR, &bmcr))
            return -1;
    } while ((bmcr & PHY_BMCR_RESET) && --guard);
    if (!guard)
        return -1;
    if (eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_ANAR,
                       0x0001 | PHY_AN_10H | PHY_AN_10F
                       | PHY_AN_100H | PHY_AN_100F)
        || eth_mdio_write(CONFIG_RMII_PHY_ADDR, PHY_BMCR,
                          PHY_BMCR_ANEN | PHY_BMCR_REAN))
        return -1;
    return 0;
}

static void
eth_phy_poll(void)
{
    uint16_t bmsr, dummy, anar, anlpar;
    if (eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMSR, &dummy)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_BMSR, &bmsr)
        || !(bmsr & PHY_BMSR_LINK) || !(bmsr & PHY_BMSR_ANOK)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ANAR, &anar)
        || eth_mdio_read(CONFIG_RMII_PHY_ADDR, PHY_ANLPAR, &anlpar)) {
        eth_link_up = 0;
        return;
    }
    uint16_t common = anar & anlpar;
    uint8_t speed100, full;
    if (common & PHY_AN_100F) {
        speed100 = full = 1;
    } else if (common & PHY_AN_100H) {
        speed100 = 1; full = 0;
    } else if (common & PHY_AN_10F) {
        speed100 = 0; full = 1;
    } else if (common & PHY_AN_10H) {
        speed100 = full = 0;
    } else {
        eth_link_up = 0;
        return;
    }
    uint32_t maccr = ETH->MACCR & ~(ETH_MACCR_FES | ETH_MACCR_DM);
    if (speed100)
        maccr |= ETH_MACCR_FES;
    if (full)
        maccr |= ETH_MACCR_DM;
    ETH->MACCR = maccr;
    eth_link_up = 1;
}

/****************************************************************
 * RMII and descriptor rings
 ****************************************************************/

static void
eth_rmii_pins(void)
{
    uint32_t af = GPIO_FUNCTION(11) | GPIO_HIGH_SPEED;
    static const uint32_t pins[] = {
        CONFIG_RMII_REF_CLK_PIN, CONFIG_RMII_MDIO_PIN,
        CONFIG_RMII_MDC_PIN, CONFIG_RMII_CRS_DV_PIN,
        CONFIG_RMII_RXD0_PIN, CONFIG_RMII_RXD1_PIN,
        CONFIG_RMII_TX_EN_PIN, CONFIG_RMII_TXD0_PIN,
        CONFIG_RMII_TXD1_PIN,
    };
    for (uint_fast8_t i = 0; i < ARRAY_SIZE(pins); i++)
        gpio_peripheral(pins[i], af, 0);
}

static void
eth_rx_rearm(uint8_t index)
{
    struct eth_h7_desc *d = &rx_ring[index];
    d->desc0 = (uint32_t)(uintptr_t)&rx_buf[index * ETH_BUF_SZ];
    d->desc1 = d->desc2 = 0;
    __DMB();
    d->desc3 = H7_RX_OWN | H7_RX_IOC | H7_RX_BUF1V;
}

static void
eth_ring_init(void)
{
    for (uint_fast8_t i = 0; i < ETH_RX_RING; i++)
        eth_rx_rearm(i);
    for (uint_fast8_t i = 0; i < ETH_TX_RING; i++) {
        tx_ring[i].desc0 = (uint32_t)(uintptr_t)&tx_buf[i * ETH_BUF_SZ];
        tx_ring[i].desc1 = tx_ring[i].desc2 = tx_ring[i].desc3 = 0;
    }
    rx_publish_idx = tx_idx = 0;
    acq_ring_init(&rx_ready, ETH_RX_RING);
}

static int
eth_mac_init(void)
{
    RCC->AHB1ENR |= RCC_AHB1ENR_ETH1MACEN | RCC_AHB1ENR_ETH1TXEN
                    | RCC_AHB1ENR_ETH1RXEN;
    (void)RCC->AHB1ENR;
    enable_pclock((uint32_t)SYSCFG);
    // EPIS=100 selects RMII on STM32H72x/H73x.
    SYSCFG->PMCR = (SYSCFG->PMCR & ~SYSCFG_PMCR_EPIS_SEL)
                   | SYSCFG_PMCR_EPIS_SEL_2;
    (void)SYSCFG->PMCR;

    eth_rmii_pins();
    eth_phy_hard_reset();

    ETH->DMAMR |= ETH_DMAMR_SWR;
    uint32_t guard = 1000000;
    while ((ETH->DMAMR & ETH_DMAMR_SWR) && --guard)
        ;
    if (!guard || eth_phy_start())
        return -1;

    ETH->MAC1USTCR = H7_HCLK / 1000000 - 1;
    ETH->MACA0HR = ((uint32_t)eth_mac_addr[5] << 8) | eth_mac_addr[4];
    ETH->MACA0LR = ((uint32_t)eth_mac_addr[3] << 24)
                   | ((uint32_t)eth_mac_addr[2] << 16)
                   | ((uint32_t)eth_mac_addr[1] << 8) | eth_mac_addr[0];
    ETH->MACPFR = 0; // perfect unicast plus broadcast
    ETH->MACCR = ETH_MACCR_ACS | ETH_MACCR_CST | ETH_MACCR_FES
                 | ETH_MACCR_DM;
    ETH->MTLTQOMR = ETH_MTLTQOMR_TSF;
    ETH->MTLRQOMR = ETH_MTLRQOMR_RSF;
    ETH->DMASBMR = ETH_DMASBMR_AAL | ETH_DMASBMR_FB;
    ETH->DMACCR = 0; // 16-byte descriptor stride (DSL=0)
    ETH->DMACTCR = ETH_DMACTCR_TPBL_32PBL;
    ETH->DMACRCR = ETH_DMACRCR_RPBL_32PBL | (ETH_BUF_SZ << 1);

    eth_ring_init();
    ETH->DMACTDRLR = ETH_TX_RING - 1;
    ETH->DMACRDRLR = ETH_RX_RING - 1;
    ETH->DMACTDLAR = (uint32_t)(uintptr_t)tx_ring;
    ETH->DMACRDLAR = (uint32_t)(uintptr_t)rx_ring;
    ETH->DMACTDTPR = (uint32_t)(uintptr_t)tx_ring;
    ETH->DMACRDTPR = (uint32_t)(uintptr_t)&rx_ring[ETH_RX_RING - 1];
    ETH->DMACIER = ETH_DMACIER_NIE | ETH_DMACIER_AIE
                   | ETH_DMACIER_RIE | ETH_DMACIER_TIE
                   | ETH_DMACIER_RBUE | ETH_DMACIER_FBEE;
    armcm_enable_irq(ETH_IRQHandler, ETH_IRQn, 2);

    ETH->DMACTCR |= ETH_DMACTCR_ST;
    ETH->DMACRCR |= ETH_DMACRCR_SR;
    ETH->DMACSR = ETH_DMACSR_TPS | ETH_DMACSR_RPS;
    ETH->MTLTQOMR |= ETH_MTLTQOMR_FTQ;
    ETH->MACCR |= ETH_MACCR_RE | ETH_MACCR_TE;
    eth_ready = 1;
    eth_phy_poll();
    return 0;
}

/****************************************************************
 * Raw-frame seam
 ****************************************************************/

static int
eth_mac_emit(const uint8_t *frame, uint32_t len)
{
    if (!eth_ready || !eth_link_up || len > ETH_BUF_SZ)
        return -1;
    struct eth_h7_desc *d = &tx_ring[tx_idx];
    if (d->desc3 & H7_TX_OWN)
        return -1;
    if (d->desc3 & H7_TX_ES)
        eth_tx_errors++;
    memcpy(&tx_buf[tx_idx * ETH_BUF_SZ], frame, len);
    d->desc0 = (uint32_t)(uintptr_t)&tx_buf[tx_idx * ETH_BUF_SZ];
    d->desc1 = 0;
    d->desc2 = H7_TX_IOC | (len & H7_TX_B1L_MASK);
    __DMB();
    d->desc3 = H7_TX_OWN | H7_TX_FD | H7_TX_LD | (len & H7_TX_FL_MASK);
    uint8_t next = (tx_idx + 1) % ETH_TX_RING;
    ETH->DMACTDTPR = (uint32_t)(uintptr_t)&tx_ring[next];
    tx_idx = next;
    eth_tx_frames++;
    return 0;
}

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
    }
    for (;;) {
        uint8_t index;
        irqstatus_t flag = irq_save();
        int ret = acq_ring_pop(&rx_ready, &index);
        irq_restore(flag);
        if (ret)
            break;
        struct eth_h7_desc *d = &rx_ring[index];
        __DMB();
        uint32_t status = d->desc3;
        uint32_t len = status & H7_RX_PL_MASK;
        if (!(status & (H7_RX_OWN | H7_RX_ES))
            && (status & (H7_RX_FD | H7_RX_LD)) == (H7_RX_FD | H7_RX_LD)
            && len <= ETH_BUF_SZ) {
            // MACCR.ACS/CST remove padding and FCS; PL is the frame length
            // delivered to the IP seam and therefore must not be reduced.
            nano_udp_input(&rx_buf[index * ETH_BUF_SZ], len);
            eth_rx_frames++;
        }
        eth_rx_rearm(index);
        __DMB();
        ETH->DMACRDTPR = (uint32_t)(uintptr_t)d;
        ETH->DMACSR = ETH_DMACSR_RBU;
        flag = irq_save();
        eth_publish_ready();
        irq_restore(flag);
    }
}
DECL_TASK(eth_mac_task);

static void
eth_mac_address_init(void)
{
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
    dma_pool_get_status(&pool);
    sendf("eth_mac_status ready=%c link=%c rx=%u tx=%u overruns=%u"
          " dma_errors=%u tx_errors=%u ready_highwater=%c"
          " dma_pool=%hu dma_used=%hu",
          eth_ready, eth_link_up, eth_rx_frames, eth_tx_frames,
          eth_rx_overruns, eth_dma_errors, eth_tx_errors, rx_ready.highwater,
          pool.size, pool.used);
}
DECL_COMMAND_FLAGS(command_eth_mac_get_status, HF_IN_SHUTDOWN,
                   "eth_mac_get_status");

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
    if (!psk_len)
        return;
#endif
    if (eth_dma_storage_init())
        return;
    eth_mac_address_init();
    nano_udp_setup(eth_mac_addr, CONFIG_RMII_IP, CONFIG_RMII_UDP_PORT,
                   eth_mac_emit,
#if CONFIG_GATEWAY_RMII
                   udp_gateway_note_rx
#else
                   udp_console_note_rx
#endif
                   );
    if (eth_mac_init() < 0)
        return;
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
