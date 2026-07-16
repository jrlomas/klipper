// FDCAN support on stm32 chips
//
// Copyright (C) 2021-2025  Kevin O'Connor <kevin@koconnor.net>
// Copyright (C) 2019 Eug Krashtan <eug.krashtan@gmail.com>
// Copyright (C) 2020 Pontus Borg <glpontus@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy, memset
#include "board/irq.h" // irq_save
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


/****************************************************************
 * CANbus code
 ****************************************************************/

#define FDCAN_IE_TC        (FDCAN_IE_TCE | FDCAN_IE_TCFE | FDCAN_IE_TFEE)

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
    memcpy(txfifo->data, msg->data, len);
    if (wire_len > len)
        memset((uint8_t*)txfifo->data + len, 0, wire_len - len);
    barrier();
    SOC_CAN->TXBAR = ((uint32_t)1 << w_index);
    return len;
}

static void
can_filter(uint32_t index, uint32_t id)
{
    MSG_RAM.FLS[index] = ((0x2 << 30) // Classic filter
                          | (0x1 << 27) // Store in Rx FIFO 0 if filter matches
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

    // Load filter
    can_filter(0, CANBUS_ID_ADMIN);
    can_filter(1, id);
    can_filter(2, id + 1);

#if CONFIG_MACH_STM32G0 || CONFIG_MACH_STM32G4
    SOC_CAN->RXGFC = ((id ? 3 : 1) << FDCAN_RXGFC_LSS_Pos
                      | 0x02 << FDCAN_RXGFC_ANFS_Pos);
#elif CONFIG_MACH_STM32H7
    uint32_t flssa = (uint32_t)MSG_RAM.FLS - SRAMCAN_BASE;
    SOC_CAN->SIDFC = flssa | ((id ? 3 : 1) << FDCAN_SIDFC_LSS_Pos);
    SOC_CAN->GFC = 0x02 << FDCAN_GFC_ANFS_Pos;
#endif

    /* Leave the initialisation mode for the filter */
    barrier();
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;
}

static struct {
    uint32_t rx_error, tx_error;
} CAN_Errors;

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
        if (lec >= 3 && lec <= 5)
            CAN_Errors.tx_error += 1;
        else
            CAN_Errors.rx_error += 1;
    }
    uint32_t rx_error = CAN_Errors.rx_error, tx_error = CAN_Errors.tx_error;
    irq_restore(flag);

    status->rx_error = rx_error;
    status->tx_error = tx_error;
    if (psr & FDCAN_PSR_BO)
        status->bus_state = CANBUS_STATE_OFF;
    else if (psr & FDCAN_PSR_EP)
        status->bus_state = CANBUS_STATE_PASSIVE;
    else if (psr & FDCAN_PSR_EW)
        status->bus_state = CANBUS_STATE_WARN;
    else
        status->bus_state = 0;
}

// This function handles CAN global interrupts
void
CAN_IRQHandler(void)
{
    uint32_t ir = SOC_CAN->IR;

    if (ir & FDCAN_IE_RF0NE) {
        SOC_CAN->IR = FDCAN_IE_RF0NE;

        uint32_t rxf0s = SOC_CAN->RXF0S;
        if (rxf0s & FDCAN_RXF0S_F0FL) {
            // Read and ack data packet
            uint32_t idx = (rxf0s & FDCAN_RXF0S_F0GI) >> FDCAN_RXF0S_F0GI_Pos;
            struct fdcan_fifo *rxf0 = &MSG_RAM.RXF0[idx];
            uint32_t ids = rxf0->id_section;
            struct canbus_msg msg = {};
            if (ids & FDCAN_XTD)
                msg.id = (ids & 0x1fffffff) | CANMSG_ID_EFF;
            else
                msg.id = (ids >> 18) & 0x7ff;
            msg.id |= ids & FDCAN_RTR ? CANMSG_ID_RTR : 0;
            uint32_t ctl = rxf0->dlc_section;
            msg.dlc = canbus_dlc_to_len((ctl >> 16) & 0x0f);
            if (ctl & FDCAN_FDF)
                msg.flags |= CANMSG_FLAG_FD;
            if (ctl & FDCAN_BRS)
                msg.flags |= CANMSG_FLAG_BRS;
            if (ids & FDCAN_ESI)
                msg.flags |= CANMSG_FLAG_ESI;
            memcpy(msg.data, rxf0->data, msg.dlc);
            barrier();
            SOC_CAN->RXF0A = idx;

            // Process packet
            canbus_process_data(&msg);
        }
    }
    if (ir & FDCAN_IE_TC) {
        // Tx
        SOC_CAN->IR = FDCAN_IE_TC;
        canbus_notify_tx();
    }
    if (ir & (FDCAN_IR_PED | FDCAN_IR_PEA)) {
        // Bus error
        uint32_t psr = SOC_CAN->PSR;
        SOC_CAN->IR = FDCAN_IR_PED | FDCAN_IR_PEA;
        uint32_t lec = psr & FDCAN_PSR_LEC_Msk;
        if (lec && lec != 7) {
            if (lec >= 3 && lec <= 5)
                CAN_Errors.tx_error += 1;
            else
                CAN_Errors.rx_error += 1;
        }
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

static uint32_t
compute_btr(uint32_t pclock, uint32_t bitrate)
{
    uint32_t brp = 1, tseg1 = 1, tseg2 = 1;
    if (compute_timing(pclock, bitrate, 512, 256, 128, 875,
                       &brp, &tseg1, &tseg2))
        shutdown("CAN nominal bit timing is not exact");
    uint32_t sjw = tseg2 > 4 ? 4 : tseg2;
    return make_btr(sjw, tseg1, tseg2, brp);
}

#if CONFIG_CANBUS_FD
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
canhw_prepare_fd(uint32_t data_bitrate, uint8_t brs)
{
    uint32_t pclock = get_pclock_frequency((uint32_t)SOC_CAN), dbtp;
    if (data_bitrate > CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE
        || try_compute_dbtp(pclock, data_bitrate, &dbtp))
        return -1;
    if (!brs && data_bitrate != CONFIG_CANBUS_FREQUENCY)
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
#if CONFIG_CANBUS_FD
    SOC_CAN->DBTP = dbtr;
    SOC_CAN->CCCR |= FDCAN_CCCR_FDOE | FDCAN_CCCR_BRSE;
#endif

#if CONFIG_MACH_STM32H7
    /* Setup message RAM addresses */
    uint32_t f0sa = (uint32_t)MSG_RAM.RXF0 - SRAMCAN_BASE;
    SOC_CAN->RXF0C = f0sa | (ARRAY_SIZE(MSG_RAM.RXF0) << FDCAN_RXF0C_F0S_Pos);
    SOC_CAN->RXESC = (7 << FDCAN_RXESC_F1DS_Pos) | (7 << FDCAN_RXESC_F0DS_Pos);
    uint32_t tbsa = (uint32_t)MSG_RAM.TXFIFO - SRAMCAN_BASE;
    SOC_CAN->TXBC = (tbsa
                     | (ARRAY_SIZE(MSG_RAM.TXFIFO) << FDCAN_TXBC_TFQS_Pos)
                     | FDCAN_TXBC_TFQM);
    SOC_CAN->TXESC = 7 << FDCAN_TXESC_TBDS_Pos;
#else
    // G0/G4 use the fixed message-RAM placement configured by reset values,
    // but queue selection remains programmable.
    SOC_CAN->TXBC |= FDCAN_TXBC_TFQM;
#endif

    /* Leave the initialisation mode */
    SOC_CAN->CCCR &= ~FDCAN_CCCR_CCE;
    SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT;

    /*##-2- Configure the CAN Filter #######################################*/
    canhw_set_filter(0);

    /*##-3- Configure Interrupts #################################*/
    armcm_enable_irq(CAN_IRQHandler, CAN_IT0_IRQn, 1);
    SOC_CAN->ILE = FDCAN_ILE_EINT0;
    SOC_CAN->IE = FDCAN_IE_RF0NE | FDCAN_IE_TC | FDCAN_IE_PEDE | FDCAN_IE_PEAE;
}
DECL_INIT(can_init);

DECL_CONSTANT("CANBUS_FD", CONFIG_CANBUS_FD);
#if CONFIG_CANBUS_FD
DECL_CONSTANT("CANBUS_DATA_FREQUENCY", CONFIG_CANBUS_DATA_FREQUENCY);
DECL_CONSTANT("CANBUS_TRANSCEIVER_MAX_DATA_RATE",
              CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE);
#endif
