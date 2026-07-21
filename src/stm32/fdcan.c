// FDCAN support on stm32 chips
//
// Copyright (C) 2021-2025  Kevin O'Connor <kevin@koconnor.net>
// Copyright (C) 2019 Eug Krashtan <eug.krashtan@gmail.com>
// Copyright (C) 2020 Pontus Borg <glpontus@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_CONSTANT_STR
#include "generic/armcm_boot.h" // armcm_enable_irq
#include "generic/canbus.h" // canbus_notify_tx
#include "generic/canserial.h" // CANBUS_ID_ADMIN
#include "internal.h" // enable_pclock
#include "sched.h" // DECL_INIT


/****************************************************************
 * Pin configuration
 ****************************************************************/

#if CONFIG_STM32_CANBUS_PA11_PA12
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PA11,PA12");
 #define GPIO_Rx GPIO('A', 11)
 #define GPIO_Tx GPIO('A', 12)
#elif CONFIG_STM32_CANBUS_PA11_PB9
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PA11,PB9");
 #define GPIO_Rx GPIO('A', 11)
 #define GPIO_Tx GPIO('B', 9)
#elif CONFIG_STM32_CANBUS_PB8_PB9
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PB8,PB9");
 #define GPIO_Rx GPIO('B', 8)
 #define GPIO_Tx GPIO('B', 9)
#elif CONFIG_STM32_CANBUS_PD0_PD1
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PD0,PD1");
 #define GPIO_Rx GPIO('D', 0)
 #define GPIO_Tx GPIO('D', 1)
#elif CONFIG_STM32_CANBUS_PD12_PD13
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PD12,PD13");
 #define GPIO_Rx GPIO('D', 12)
 #define GPIO_Tx GPIO('D', 13)
#elif CONFIG_STM32_CANBUS_PB0_PB1
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PB0,PB1");
 #define GPIO_Rx GPIO('B', 0)
 #define GPIO_Tx GPIO('B', 1)
#elif CONFIG_STM32_CANBUS_PC2_PC3
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PC2,PC3");
 #define GPIO_Rx GPIO('C', 2)
 #define GPIO_Tx GPIO('C', 3)
#elif CONFIG_STM32_CANBUS_PB5_PB6
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PB5,PB6");
 #define GPIO_Rx GPIO('B', 5)
 #define GPIO_Tx GPIO('B', 6)
#elif CONFIG_STM32_CANBUS_PB12_PB13
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PB12,PB13");
 #define GPIO_Rx GPIO('B', 12)
 #define GPIO_Tx GPIO('B', 13)
#elif CONFIG_STM32_CANBUS_PH13_PH14
 DECL_CONSTANT_STR("RESERVE_PINS_CAN", "PH14,PH13");
 #define GPIO_Rx GPIO('H', 14)
 #define GPIO_Tx GPIO('H', 13)
#endif

#if !(CONFIG_STM32_CANBUS_PB0_PB1 || CONFIG_STM32_CANBUS_PC2_PC3 \
     || CONFIG_STM32_CANBUS_PB5_PB6 ||CONFIG_STM32_CANBUS_PB12_PB13)
 #define SOC_CAN FDCAN1
 #define MSG_RAM (((struct fdcan_ram_layout*)SRAMCAN_BASE)->fdcan1)
 #if CONFIG_MACH_STM32H7 || CONFIG_MACH_STM32G4
  #define CAN_IT0_IRQn  FDCAN1_IT0_IRQn
 #endif
#else
 #define SOC_CAN FDCAN2
 #define MSG_RAM (((struct fdcan_ram_layout*)SRAMCAN_BASE)->fdcan2)
 #if CONFIG_MACH_STM32H7 || CONFIG_MACH_STM32G4
  #define CAN_IT0_IRQn  FDCAN2_IT0_IRQn
 #endif
#endif

#if CONFIG_MACH_STM32G0
 #define CAN_IT0_IRQn  TIM16_FDCAN_IT0_IRQn
 #define CAN_FUNCTION  GPIO_FUNCTION(3) // Alternative function mapping number
#endif

#if CONFIG_MACH_STM32H7 || CONFIG_MACH_STM32G4
 #define CAN_FUNCTION  GPIO_FUNCTION(9) // Alternative function mapping number
#endif


/****************************************************************
 * Message ram layout
 ****************************************************************/

struct fdcan_fifo {
    uint32_t id_section;
    uint32_t dlc_section;
    uint32_t data[64 / 4];
};

#define FDCAN_XTD (1<<30)
#define FDCAN_RTR (1<<29)
#define FDCAN_ESI (1U<<31)
#define FDCAN_EFC (1U<<23)
#define FDCAN_FDF (1U<<21)
#define FDCAN_BRS (1U<<20)

struct fdcan_msg_ram {
    uint32_t FLS[28]; // Filter list standard
    uint32_t FLE[16]; // Filter list extended
    struct fdcan_fifo RXF0[3];
    struct fdcan_fifo RXF1[3];
    uint32_t TEF[6]; // Tx event FIFO
    struct fdcan_fifo TXFIFO[3];
};

struct fdcan_ram_layout {
    struct fdcan_msg_ram fdcan1;
    struct fdcan_msg_ram fdcan2;
};

// Bosch M_CAN message RAM only supports aligned 32-bit accesses on STM32.
// Byte-oriented memcpy stores can replicate the final byte across each word.
static void
fdcan_ram_write(uint32_t *dst, const uint8_t *src, uint32_t len,
                uint32_t wire_len)
{
    for (uint32_t offset = 0; offset < wire_len; offset += 4) {
        uint32_t word = 0;
        for (uint_fast8_t byte = 0; byte < 4; byte++) {
            uint32_t pos = offset + byte;
            if (pos < len)
                word |= (uint32_t)src[pos] << (byte * 8);
        }
        dst[offset / 4] = word;
    }
}

static void
fdcan_ram_read(uint8_t *dst, const uint32_t *src, uint32_t len)
{
    for (uint32_t offset = 0; offset < len; offset += 4) {
        uint32_t word = src[offset / 4];
        for (uint_fast8_t byte = 0; byte < 4; byte++) {
            uint32_t pos = offset + byte;
            if (pos < len)
                dst[pos] = word >> (byte * 8);
        }
    }
}


/****************************************************************
 * CANbus code
 ****************************************************************/

#define FDCAN_IE_TC        (FDCAN_IE_TCE | FDCAN_IE_TCFE | FDCAN_IE_TFEE)

// Hardware automatic retransmission remains enabled for transient arbitration
// and line errors.  A per-buffer deadline bounds it: once the deadline passes,
// cancel the stale frame and let the command protocol's sequence/ACK layer
// decide whether the command block is still useful and retransmit it.
static struct {
    struct timer timer;
    uint32_t expires[3];
    uint32_t pending;
    uint32_t stale_cancels;
    uint8_t timer_armed;
} TxRetry;

static uint_fast8_t
can_tx_retry_event(struct timer *timer)
{
    uint32_t now = timer_read_time(), pending_hw = SOC_CAN->TXBRP;
    uint32_t pending = TxRetry.pending & pending_hw;
    uint32_t cancel = 0, next = 0;
    for (uint_fast8_t i = 0; i < ARRAY_SIZE(TxRetry.expires); i++) {
        uint32_t bit = 1U << i;
        if (!(pending & bit))
            continue;
        uint32_t expires = TxRetry.expires[i];
        if (!timer_is_before(now, expires))
            cancel |= bit;
        else if (!next || timer_is_before(expires, next))
            next = expires;
    }
    if (cancel) {
        SOC_CAN->TXBCR = cancel;
        TxRetry.pending &= ~cancel;
        TxRetry.stale_cancels += __builtin_popcount(cancel);
        canbus_notify_tx();
    }
    if (next) {
        timer->waketime = next;
        return SF_RESCHEDULE;
    }
    TxRetry.timer_armed = 0;
    return SF_DONE;
}

static void
can_tx_retry_arm(uint32_t index, uint32_t id)
{
    uint32_t timeout = (id == CANBUS_ID_ADMIN || id == CANBUS_ID_ADMIN_RESP
                        || id == CANBUS_ID_TIME_SYNC
                        || id == CANBUS_ID_TIME_FOLLOWUP)
                       ? CONFIG_CANBUS_ADMIN_TX_RETRY_US
                       : CONFIG_CANBUS_TX_RETRY_US;
    uint32_t expires = timer_read_time() + timer_from_us(timeout);
    TxRetry.expires[index] = expires;
    TxRetry.pending |= 1U << index;
    if (!TxRetry.timer_armed) {
        TxRetry.timer_armed = 1;
        TxRetry.timer.func = can_tx_retry_event;
        TxRetry.timer.waketime = expires;
        sched_add_timer(&TxRetry.timer);
    } else if (timer_is_before(expires, TxRetry.timer.waketime)) {
        sched_del_timer(&TxRetry.timer);
        TxRetry.timer.waketime = expires;
        sched_add_timer(&TxRetry.timer);
    }
}

// Transmit a packet
int
canhw_send(struct canbus_msg *msg)
{
    uint32_t len = CANMSG_DATA_LEN(msg);
    if (msg->dlc > 64 || ((msg->flags & CANMSG_FLAG_FD) == 0 && len > 8)
        || ((msg->flags & CANMSG_FLAG_FD) && (msg->id & CANMSG_ID_RTR)))
        return 0;
#if !CONFIG_CANBUS_FD
    if (msg->flags & CANMSG_FLAG_FD)
        return 0;
#endif
    uint32_t txfqs = SOC_CAN->TXFQS;
    if (txfqs & FDCAN_TXFQS_TFQF)
        // No space in transmit fifo - wait for irq
        return -1;

    uint32_t w_index = ((txfqs & FDCAN_TXFQS_TFQPI) >> FDCAN_TXFQS_TFQPI_Pos);
    struct fdcan_fifo *txfifo = &MSG_RAM.TXFIFO[w_index];
    uint32_t ids;
    if (msg->id & CANMSG_ID_EFF)
        ids = (msg->id & 0x1fffffff) | FDCAN_XTD;
    else
        ids = (msg->id & 0x7ff) << 18;
    ids |= msg->id & CANMSG_ID_RTR ? FDCAN_RTR : 0;
    ids |= msg->flags & CANMSG_FLAG_ESI ? FDCAN_ESI : 0;
    txfifo->id_section = ids;
    uint32_t dlc = canbus_len_to_dlc(len);
    uint32_t ctl = dlc << 16;
    if (msg->flags & CANMSG_FLAG_FD)
        ctl |= FDCAN_FDF;
    if (msg->flags & CANMSG_FLAG_BRS)
        ctl |= FDCAN_BRS;
    if (msg->flags & CANMSG_FLAG_TX_EVENT)
        ctl |= FDCAN_EFC | ((uint32_t)msg->tx_tag << 24);
    txfifo->dlc_section = ctl;
    uint32_t wire_len = canbus_dlc_to_len(dlc);
    fdcan_ram_write(txfifo->data, msg->data, len, wire_len);
    barrier();
    SOC_CAN->TXBAR = ((uint32_t)1 << w_index);
    can_tx_retry_arm(w_index, msg->id & ~(CANMSG_ID_EFF | CANMSG_ID_RTR));
    return len;
}

enum {
    FDCAN_FILTER_FIFO0 = 1,
    FDCAN_FILTER_FIFO1 = 2,
};

static void
can_filter(uint32_t index, uint32_t id, uint32_t fifo)
{
    MSG_RAM.FLS[index] = ((0x2 << 30) // Classic filter
                          | (fifo << 27)
                          | (id << 16)
                          | 0x7FF); // mask all enabled
}

// Setup the receive packet filter
void
canhw_set_filter(uint32_t id)
{
    if (!CONFIG_CANBUS_FILTER)
        return;
    /* Request initialisation */
    SOC_CAN->CCCR |= FDCAN_CCCR_INIT;
    /* Wait the acknowledge */
    while (!(SOC_CAN->CCCR & FDCAN_CCCR_INIT))
        ;
    /* Enable configuration change */
    SOC_CAN->CCCR |= FDCAN_CCCR_CCE;

    // Keep the credit-controlled serial data stream in FIFO0.  Route the
    // independent admin, time-transfer, and conflict/control traffic through
    // FIFO1 so it cannot consume the data stream's three hardware slots while
    // a trajectory critical section temporarily defers this IRQ.
    can_filter(0, CANBUS_ID_ADMIN, FDCAN_FILTER_FIFO1);
    can_filter(1, CANBUS_ID_TIME_SYNC, FDCAN_FILTER_FIFO1);
    can_filter(2, CANBUS_ID_TIME_FOLLOWUP, FDCAN_FILTER_FIFO1);
    can_filter(3, id, FDCAN_FILTER_FIFO0);
    can_filter(4, id + 1, FDCAN_FILTER_FIFO1);

#if CONFIG_MACH_STM32G0 || CONFIG_MACH_STM32G4
    SOC_CAN->RXGFC = ((id ? 5 : 3) << FDCAN_RXGFC_LSS_Pos
                      | 0x02 << FDCAN_RXGFC_ANFS_Pos);
#elif CONFIG_MACH_STM32H7
    uint32_t flssa = (uint32_t)MSG_RAM.FLS - SRAMCAN_BASE;
    SOC_CAN->SIDFC = flssa | ((id ? 5 : 3) << FDCAN_SIDFC_LSS_Pos);
    SOC_CAN->GFC = 0x02 << FDCAN_GFC_ANFS_Pos;
#endif

    /* Leave the initialisation mode for the filter */
    barrier();
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;
}

static struct {
    uint32_t rx_error, tx_error;
    uint32_t rx_fifo_overruns, rx_protocol_errors, rx_fifo_highwater;
    uint32_t rx_fifo0_overruns, rx_fifo1_overruns;
    uint32_t rx_fifo0_highwater, rx_fifo1_highwater;
    uint32_t rx_service_max_delay_ticks;
} CAN_Errors;

static void
fdcan_account_protocol_error(uint32_t lec)
{
    if (lec >= 3 && lec <= 5)
        CAN_Errors.tx_error++;
    else {
        CAN_Errors.rx_error++;
        CAN_Errors.rx_protocol_errors++;
    }
}

static uint32_t
fdcan_timestamp_to_clock(uint16_t timestamp)
{
    uint32_t local_now = timer_read_time();
    uint16_t timestamp_now = SOC_CAN->TSCV;
    uint16_t elapsed = timestamp_now - timestamp;
    uint32_t local_elapsed = ((uint64_t)elapsed * CONFIG_CLOCK_FREQ
                              + CONFIG_CANBUS_FREQUENCY / 2)
                             / CONFIG_CANBUS_FREQUENCY;
    return local_now - local_elapsed;
}

#if CONFIG_CANBUS_FD
static uint32_t PreparedDBTP, PreparedDataBitrate;
static uint8_t PreparedBRS;
#endif

// Report interface status
void
canhw_get_status(struct canbus_status *status)
{
    irqstatus_t flag = irq_save();
    uint32_t psr = SOC_CAN->PSR, lec = psr & FDCAN_PSR_LEC_Msk;
    if (lec && lec != 7) {
        // Reading PSR clears it - so update state here
        fdcan_account_protocol_error(lec);
    }
    uint32_t rx_error = CAN_Errors.rx_error, tx_error = CAN_Errors.tx_error;
    uint32_t fifo_overruns = CAN_Errors.rx_fifo_overruns;
    uint32_t protocol_errors = CAN_Errors.rx_protocol_errors;
    uint32_t fifo_highwater = CAN_Errors.rx_fifo_highwater;
    uint32_t fifo0_overruns = CAN_Errors.rx_fifo0_overruns;
    uint32_t fifo1_overruns = CAN_Errors.rx_fifo1_overruns;
    uint32_t fifo0_highwater = CAN_Errors.rx_fifo0_highwater;
    uint32_t fifo1_highwater = CAN_Errors.rx_fifo1_highwater;
    uint32_t service_max_delay = CAN_Errors.rx_service_max_delay_ticks;
    irq_restore(flag);

    status->rx_error = rx_error;
    status->tx_error = tx_error;
    status->tx_retries = TxRetry.stale_cancels;
    status->rx_fifo_overruns = fifo_overruns;
    status->rx_protocol_errors = protocol_errors;
    status->rx_fifo_highwater = fifo_highwater;
    status->rx_fifo0_overruns = fifo0_overruns;
    status->rx_fifo1_overruns = fifo1_overruns;
    status->rx_fifo0_highwater = fifo0_highwater;
    status->rx_fifo1_highwater = fifo1_highwater;
    status->rx_service_max_delay_ticks = service_max_delay;
    if (psr & FDCAN_PSR_BO)
        status->bus_state = CANBUS_STATE_OFF;
    else if (psr & FDCAN_PSR_EP)
        status->bus_state = CANBUS_STATE_PASSIVE;
    else if (psr & FDCAN_PSR_EW)
        status->bus_state = CANBUS_STATE_WARN;
    else
        status->bus_state = 0;
}

static void
fdcan_read_rx_element(struct fdcan_fifo *rx, struct canbus_msg *msg)
{
    uint32_t ids = rx->id_section;
    if (ids & FDCAN_XTD)
        msg->id = (ids & 0x1fffffff) | CANMSG_ID_EFF;
    else
        msg->id = (ids >> 18) & 0x7ff;
    msg->id |= ids & FDCAN_RTR ? CANMSG_ID_RTR : 0;
    uint32_t ctl = rx->dlc_section;
    msg->hw_clock = fdcan_timestamp_to_clock(ctl & 0xffff);
    msg->flags |= CANMSG_FLAG_HW_TIMESTAMP;
    msg->dlc = canbus_dlc_to_len((ctl >> 16) & 0x0f);
    if (ctl & FDCAN_FDF)
        msg->flags |= CANMSG_FLAG_FD;
    if (ctl & FDCAN_BRS)
        msg->flags |= CANMSG_FLAG_BRS;
    if (ids & FDCAN_ESI)
        msg->flags |= CANMSG_FLAG_ESI;
    fdcan_ram_read(msg->data, rx->data, msg->dlc);

    // The hardware timestamp marks start of frame, so this includes the wire
    // time as well as interrupt deferral and receive-copy time.  It is still a
    // conservative and directly comparable bound on receive service latency.
    uint32_t service_delay = timer_read_time() - msg->hw_clock;
    if (service_delay > CAN_Errors.rx_service_max_delay_ticks)
        CAN_Errors.rx_service_max_delay_ticks = service_delay;
}

static void
fdcan_drain_fifo0(void)
{
    for (uint_fast8_t drained = 0;
         drained < ARRAY_SIZE(MSG_RAM.RXF0); drained++) {
        uint32_t status = SOC_CAN->RXF0S;
        uint32_t fill = ((status & FDCAN_RXF0S_F0FL_Msk)
                         >> FDCAN_RXF0S_F0FL_Pos);
        if (fill > CAN_Errors.rx_fifo0_highwater)
            CAN_Errors.rx_fifo0_highwater = fill;
        if (fill > CAN_Errors.rx_fifo_highwater)
            CAN_Errors.rx_fifo_highwater = fill;
        if (!fill)
            break;
        uint32_t idx = ((status & FDCAN_RXF0S_F0GI_Msk)
                        >> FDCAN_RXF0S_F0GI_Pos);
        struct canbus_msg msg = {};
        fdcan_read_rx_element(&MSG_RAM.RXF0[idx], &msg);
        barrier();
        SOC_CAN->RXF0A = idx;
        canbus_process_data(&msg);
    }
}

static void
fdcan_drain_fifo1(void)
{
    for (uint_fast8_t drained = 0;
         drained < ARRAY_SIZE(MSG_RAM.RXF1); drained++) {
        uint32_t status = SOC_CAN->RXF1S;
        uint32_t fill = ((status & FDCAN_RXF1S_F1FL_Msk)
                         >> FDCAN_RXF1S_F1FL_Pos);
        if (fill > CAN_Errors.rx_fifo1_highwater)
            CAN_Errors.rx_fifo1_highwater = fill;
        if (fill > CAN_Errors.rx_fifo_highwater)
            CAN_Errors.rx_fifo_highwater = fill;
        if (!fill)
            break;
        uint32_t idx = ((status & FDCAN_RXF1S_F1GI_Msk)
                        >> FDCAN_RXF1S_F1GI_Pos);
        struct canbus_msg msg = {};
        fdcan_read_rx_element(&MSG_RAM.RXF1[idx], &msg);
        barrier();
        SOC_CAN->RXF1A = idx;
        canbus_process_data(&msg);
    }
}

// This function handles CAN global interrupts
void
CAN_IRQHandler(void)
{
    uint32_t ir = SOC_CAN->IR;

    if (ir & (FDCAN_IR_RF0N | FDCAN_IR_RF0L)) {
        // RF0N is an event flag, not a promise of one queued element. A
        // trajectory critical section may defer this IRQ while all three
        // hardware FIFO slots fill. Clear the event first, then drain the
        // bounded hardware FIFO so an accumulated tail is not stranded until
        // another edge (and eventually overwritten). New arrivals after the
        // clear reassert RF0N and receive another bounded service pass.
        SOC_CAN->IR = FDCAN_IR_RF0N | FDCAN_IR_RF0L;
        if (ir & FDCAN_IR_RF0L) {
            CAN_Errors.rx_error++;
            CAN_Errors.rx_fifo_overruns++;
            CAN_Errors.rx_fifo0_overruns++;
        }
        fdcan_drain_fifo0();
    }
    if (ir & (FDCAN_IR_RF1N | FDCAN_IR_RF1L)) {
        SOC_CAN->IR = FDCAN_IR_RF1N | FDCAN_IR_RF1L;
        if (ir & FDCAN_IR_RF1L) {
            CAN_Errors.rx_error++;
            CAN_Errors.rx_fifo_overruns++;
            CAN_Errors.rx_fifo1_overruns++;
        }
        fdcan_drain_fifo1();
    }
    if (ir & FDCAN_IE_TC) {
        // Tx
        SOC_CAN->IR = FDCAN_IE_TC;
        TxRetry.pending &= ~(SOC_CAN->TXBTO | SOC_CAN->TXBCF);
        canbus_notify_tx();
    }
    if (ir & (FDCAN_IR_TEFN | FDCAN_IR_TEFL)) {
        SOC_CAN->IR = FDCAN_IR_TEFN | FDCAN_IR_TEFL;
        if (ir & FDCAN_IR_TEFL)
            CAN_Errors.tx_error++;
        for (;;) {
            uint32_t txefs = SOC_CAN->TXEFS;
            if (!(txefs & FDCAN_TXEFS_EFFL_Msk))
                break;
            uint32_t index = ((txefs & FDCAN_TXEFS_EFGI_Msk)
                              >> FDCAN_TXEFS_EFGI_Pos);
            uint32_t event = MSG_RAM.TEF[index * 2 + 1];
            uint8_t tag = event >> 24;
            uint32_t local_clock = fdcan_timestamp_to_clock(event & 0xffff);
            SOC_CAN->TXEFA = index;
            canbus_notify_tx_timestamp(tag, local_clock);
        }
    }
    if (ir & (FDCAN_IR_PED | FDCAN_IR_PEA)) {
        // Bus error
        uint32_t psr = SOC_CAN->PSR;
        SOC_CAN->IR = FDCAN_IR_PED | FDCAN_IR_PEA;
        uint32_t lec = psr & FDCAN_PSR_LEC_Msk;
        if (lec && lec != 7) {
            fdcan_account_protocol_error(lec);
            canbus_notify_protocol_error();
        }
    }
    if (ir & FDCAN_IR_BO) {
        // Hardware error confinement, rather than an arbitrary transient
        // error count, is the fail-closed boundary.
        SOC_CAN->IR = FDCAN_IR_BO;
        canbus_notify_bus_off();
    }
}

static inline uint32_t
make_btr(uint32_t sjw,       // Sync jump width, ... hmm
         uint32_t time_seg1, // time segment before sample point, 1 .. 16
         uint32_t time_seg2, // time segment after sample point, 1 .. 8
         uint32_t brp)       // Baud rate prescaler, 1 .. 1024
{
    return (((uint32_t)(sjw-1)) << FDCAN_NBTP_NSJW_Pos
            | ((uint32_t)(time_seg1-1)) << FDCAN_NBTP_NTSEG1_Pos
            | ((uint32_t)(time_seg2-1)) << FDCAN_NBTP_NTSEG2_Pos
            | ((uint32_t)(brp - 1)) << FDCAN_NBTP_NBRP_Pos);
}

static int
compute_timing(uint32_t pclock, uint32_t bitrate, uint32_t max_brp,
               uint32_t max_tseg1, uint32_t max_tseg2,
               uint32_t target_sample_permille, uint32_t *best_brp,
               uint32_t *best_tseg1, uint32_t *best_tseg2)
{
    if (!bitrate || pclock % bitrate)
        return -1;
    uint32_t bit_clocks = pclock / bitrate;
    uint32_t found = 0, best_error = 1001;
    for (uint32_t brp = 1; brp <= max_brp; brp++) {
        if (bit_clocks % brp)
            continue;
        uint32_t tq = bit_clocks / brp;
        if (tq < 3 || tq > 1 + max_tseg1 + max_tseg2)
            continue;
        uint32_t tseg2 = (tq * (1000 - target_sample_permille) + 500) / 1000;
        if (tseg2 < 1)
            tseg2 = 1;
        if (tseg2 > max_tseg2)
            tseg2 = max_tseg2;
        uint32_t tseg1 = tq - 1 - tseg2;
        if (tseg1 < 1 || tseg1 > max_tseg1)
            continue;
        uint32_t sample = ((1 + tseg1) * 1000 + tq / 2) / tq;
        uint32_t error = (sample > target_sample_permille
                          ? sample - target_sample_permille
                          : target_sample_permille - sample);
        if (!found || error < best_error) {
            found = 1;
            best_error = error;
            *best_brp = brp;
            *best_tseg1 = tseg1;
            *best_tseg2 = tseg2;
        }
    }
    return found ? 0 : -1;
}

static int
try_compute_btr(uint32_t pclock, uint32_t bitrate, uint32_t *btr)
{
    uint32_t brp = 1, tseg1 = 1, tseg2 = 1;
    if (compute_timing(pclock, bitrate, 512, 256, 128, 875,
                       &brp, &tseg1, &tseg2))
        return -1;
    uint32_t sjw = tseg2 > 4 ? 4 : tseg2;
    *btr = make_btr(sjw, tseg1, tseg2, brp);
    return 0;
}

static uint32_t
compute_btr(uint32_t pclock, uint32_t bitrate)
{
    uint32_t btr;
    if (try_compute_btr(pclock, bitrate, &btr))
        shutdown("CAN nominal bit timing is not exact");
    return btr;
}

#if CONFIG_CANBUS_FD
static uint32_t ActiveNominalBitrate = CONFIG_CANBUS_FREQUENCY;

static uint32_t
compute_dbtp(uint32_t pclock, uint32_t bitrate)
{
    uint32_t brp = 1, tseg1 = 1, tseg2 = 1;
    if (compute_timing(pclock, bitrate, 32, 32, 16, 800,
                       &brp, &tseg1, &tseg2))
        shutdown("CAN data bit timing is not exact");
    uint32_t sjw = tseg2 > 4 ? 4 : tseg2;
    return ((sjw - 1) << FDCAN_DBTP_DSJW_Pos
            | (tseg2 - 1) << FDCAN_DBTP_DTSEG2_Pos
            | (tseg1 - 1) << FDCAN_DBTP_DTSEG1_Pos
            | (brp - 1) << FDCAN_DBTP_DBRP_Pos);
}

static int
try_compute_dbtp(uint32_t pclock, uint32_t bitrate, uint32_t *dbtp)
{
    uint32_t brp = 1, tseg1 = 1, tseg2 = 1;
    if (compute_timing(pclock, bitrate, 32, 32, 16, 800,
                       &brp, &tseg1, &tseg2))
        return -1;
    uint32_t sjw = tseg2 > 4 ? 4 : tseg2;
    *dbtp = ((sjw - 1) << FDCAN_DBTP_DSJW_Pos
             | (tseg2 - 1) << FDCAN_DBTP_DTSEG2_Pos
             | (tseg1 - 1) << FDCAN_DBTP_DTSEG1_Pos
             | (brp - 1) << FDCAN_DBTP_DBRP_Pos);
    return 0;
}

uint32_t
canhw_get_fd_bitrate_mask(void)
{
    static const uint32_t rates[] = { 1000000, 2000000, 5000000, 8000000 };
    uint32_t mask = 0, pclock = get_pclock_frequency((uint32_t)SOC_CAN);
    for (uint_fast8_t i = 0; i < ARRAY_SIZE(rates); i++) {
        uint32_t ignored;
        if (rates[i] <= CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE
            && !try_compute_dbtp(pclock, rates[i], &ignored))
            mask |= 1U << i;
    }
    return mask;
}

int
canhw_set_nominal_bitrate(uint32_t bitrate)
{
    uint32_t pclock = get_pclock_frequency((uint32_t)SOC_CAN), nbtp;
    if (try_compute_btr(pclock, bitrate, &nbtp))
        return -1;
    SOC_CAN->CCCR |= FDCAN_CCCR_INIT;
    while (!(SOC_CAN->CCCR & FDCAN_CCCR_INIT))
        ;
    SOC_CAN->CCCR |= FDCAN_CCCR_CCE;
    SOC_CAN->NBTP = nbtp;
    barrier();
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;
    ActiveNominalBitrate = bitrate;
    return 0;
}

int
canhw_prepare_fd(uint32_t data_bitrate, uint8_t brs)
{
    uint32_t pclock = get_pclock_frequency((uint32_t)SOC_CAN), dbtp;
    if (data_bitrate > CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE
        || try_compute_dbtp(pclock, data_bitrate, &dbtp))
        return -1;
    if (!brs && data_bitrate != ActiveNominalBitrate)
        return -1;
    PreparedDBTP = dbtp;
    PreparedDataBitrate = data_bitrate;
    PreparedBRS = !!brs;
    return 0;
}

int
canhw_commit_fd(void)
{
    if (!PreparedDataBitrate)
        return -1;
    SOC_CAN->CCCR |= FDCAN_CCCR_INIT;
    while (!(SOC_CAN->CCCR & FDCAN_CCCR_INIT))
        ;
    SOC_CAN->CCCR |= FDCAN_CCCR_CCE;
    SOC_CAN->DBTP = PreparedDBTP;
    SOC_CAN->CCCR |= FDCAN_CCCR_FDOE;
    if (PreparedBRS)
        SOC_CAN->CCCR |= FDCAN_CCCR_BRSE;
    else
        SOC_CAN->CCCR &= ~FDCAN_CCCR_BRSE;
    barrier();
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;
    return 0;
}

void
canhw_abort_fd(void)
{
    PreparedDBTP = PreparedDataBitrate = 0;
    PreparedBRS = 0;
    // Suppress BRS emission and, if bus-off set INIT, explicitly begin the
    // M_CAN recovery sequence by leaving INIT again. FDOE remains enabled so
    // the controller can diagnose residual FD traffic while this node emits
    // only the Classical recovery floor.
    SOC_CAN->CCCR |= FDCAN_CCCR_INIT;
    while (!(SOC_CAN->CCCR & FDCAN_CCCR_INIT))
        ;
    SOC_CAN->CCCR |= FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_BRSE;
    barrier();
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;
}
#endif

void
can_init(void)
{
    enable_pclock((uint32_t)SOC_CAN);

    gpio_peripheral(GPIO_Rx, CAN_FUNCTION, 1);
    gpio_peripheral(GPIO_Tx, CAN_FUNCTION, 0);

    uint32_t pclock = get_pclock_frequency((uint32_t)SOC_CAN);

    uint32_t btr = compute_btr(pclock, CONFIG_CANBUS_FREQUENCY);
#if CONFIG_CANBUS_FD
    if (CONFIG_CANBUS_DATA_FREQUENCY
        > CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE)
        shutdown("CAN FD data rate exceeds transceiver capability");
    uint32_t dbtr = compute_dbtp(pclock, CONFIG_CANBUS_DATA_FREQUENCY);
#endif

    /*##-1- Configure the CAN #######################################*/

    /* Exit from sleep mode */
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CSR;
    /* Wait the acknowledge */
    while (SOC_CAN->CCCR & FDCAN_CCCR_CSA)
        ;
    /* Request initialisation */
    SOC_CAN->CCCR |= FDCAN_CCCR_INIT;
    /* Wait the acknowledge */
    while (!(SOC_CAN->CCCR & FDCAN_CCCR_INIT))
        ;
    /* Enable configuration change */
    SOC_CAN->CCCR |= FDCAN_CCCR_CCE;

    /* Disable protocol exception handling */
    SOC_CAN->CCCR |= FDCAN_CCCR_PXHD;

    SOC_CAN->NBTP = btr;
    // Internal timestamp counter: one tick per nominal CAN bit. Timestamp
    // conversion below anchors the 16-bit counter to the MCU timer while the
    // FIFO/Event entry is still safely inside its wrap interval.
    SOC_CAN->TSCC = 1 << FDCAN_TSCC_TSS_Pos;
#if CONFIG_CANBUS_FD
    SOC_CAN->DBTP = dbtr;
    SOC_CAN->CCCR |= FDCAN_CCCR_FDOE | FDCAN_CCCR_BRSE;
#endif

#if CONFIG_MACH_STM32H7
    /* Setup message RAM addresses */
    uint32_t f0sa = (uint32_t)MSG_RAM.RXF0 - SRAMCAN_BASE;
    SOC_CAN->RXF0C = f0sa | (ARRAY_SIZE(MSG_RAM.RXF0) << FDCAN_RXF0C_F0S_Pos);
    uint32_t f1sa = (uint32_t)MSG_RAM.RXF1 - SRAMCAN_BASE;
    SOC_CAN->RXF1C = f1sa | (ARRAY_SIZE(MSG_RAM.RXF1) << FDCAN_RXF1C_F1S_Pos);
    SOC_CAN->RXESC = (7 << FDCAN_RXESC_F1DS_Pos) | (7 << FDCAN_RXESC_F0DS_Pos);
    uint32_t tbsa = (uint32_t)MSG_RAM.TXFIFO - SRAMCAN_BASE;
    SOC_CAN->TXBC = (tbsa
                     | (ARRAY_SIZE(MSG_RAM.TXFIFO) << FDCAN_TXBC_TFQS_Pos));
    SOC_CAN->TXESC = 7 << FDCAN_TXESC_TBDS_Pos;
    uint32_t efsa = (uint32_t)MSG_RAM.TEF - SRAMCAN_BASE;
    SOC_CAN->TXEFC = efsa | (3 << FDCAN_TXEFC_EFS_Pos);
#else
    // G0/G4 use the fixed message-RAM placement configured by reset values,
    // but FIFO/queue selection remains programmable.  Keep ordered FIFO mode:
    // the carrier is a byte stream and relies on FIFO-empty refill wakes.
    SOC_CAN->TXBC &= ~FDCAN_TXBC_TFQM;
#endif

    // Wake the producer whenever any hardware slot completes or finishes a
    // cancellation.  TFE covers normal FIFO draining; these per-buffer gates
    // also guarantee progress after a bounded stale-frame cancellation.
    uint32_t tx_irq_mask = ((1U << ARRAY_SIZE(MSG_RAM.TXFIFO)) - 1);
    SOC_CAN->TXBTIE = tx_irq_mask;
    SOC_CAN->TXBCIE = tx_irq_mask;

    /* Leave the initialisation mode */
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;

    /*##-2- Configure the CAN Filter #######################################*/
    canhw_set_filter(0);

    /*##-3- Configure Interrupts #################################*/
    armcm_enable_irq(CAN_IRQHandler, CAN_IT0_IRQn, 1);
    SOC_CAN->ILE = FDCAN_ILE_EINT0;
    SOC_CAN->IE = (FDCAN_IE_RF0NE | FDCAN_IE_RF0LE
                   | FDCAN_IE_RF1NE | FDCAN_IE_RF1LE | FDCAN_IE_TC
                   | FDCAN_IE_PEDE
                   | FDCAN_IE_PEAE | FDCAN_IE_BOE
                   | FDCAN_IE_TEFNE | FDCAN_IE_TEFLE);
}
DECL_INIT(can_init);

DECL_CONSTANT("CANBUS_FD", CONFIG_CANBUS_FD);
#if CONFIG_CANBUS_FD
DECL_CONSTANT("CANBUS_DATA_FREQUENCY", CONFIG_CANBUS_DATA_FREQUENCY);
DECL_CONSTANT("CANBUS_TRANSCEIVER_MAX_DATA_RATE",
              CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE);
#endif
