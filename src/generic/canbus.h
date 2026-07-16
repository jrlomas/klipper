#ifndef __CANBUS_H__
#define __CANBUS_H__

#include <stdint.h> // uint32_t

struct canbus_msg {
    uint32_t id;
    // Payload length in bytes. Hardware drivers translate this to/from DLC.
    uint8_t dlc;
    uint8_t flags;
    uint16_t tx_tag;
    union {
        uint8_t data[64];
        uint32_t data32[16];
    };
};

#define CANMSG_ID_RTR (1<<30)
#define CANMSG_ID_EFF (1<<31)

#define CANMSG_FLAG_FD       (1<<0)
#define CANMSG_FLAG_BRS      (1<<1)
#define CANMSG_FLAG_ESI      (1<<2)
#define CANMSG_FLAG_TX_EVENT (1<<3)

#define CANMSG_DATA_LEN(msg) ((msg)->dlc > 64 ? 64 : (msg)->dlc)

static inline uint8_t
canbus_dlc_to_len(uint8_t dlc)
{
    static const uint8_t lengths[16] = {
        0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64
    };
    return lengths[dlc & 0x0f];
}

static inline uint8_t
canbus_len_to_dlc(uint8_t len)
{
    if (len <= 8)
        return len;
    if (len <= 12)
        return 9;
    if (len <= 16)
        return 10;
    if (len <= 20)
        return 11;
    if (len <= 24)
        return 12;
    if (len <= 32)
        return 13;
    if (len <= 48)
        return 14;
    return 15;
}

struct canbus_status {
    uint32_t rx_error, tx_error, tx_retries;
    uint32_t bus_state;
};

enum {
    CANBUS_STATE_ACTIVE, CANBUS_STATE_WARN, CANBUS_STATE_PASSIVE,
    CANBUS_STATE_OFF,
};

// callbacks provided by board specific code
int canhw_send(struct canbus_msg *msg);
void canhw_set_filter(uint32_t id);
void canhw_get_status(struct canbus_status *status);
#if CONFIG_CANBUS_FD
uint32_t canhw_get_fd_bitrate_mask(void);
int canhw_prepare_fd(uint32_t data_bitrate, uint8_t brs);
int canhw_commit_fd(void);
void canhw_abort_fd(void);
#endif

// canbus.c
int canbus_send(struct canbus_msg *msg);
void canbus_set_filter(uint32_t id);
void canbus_notify_tx(void);
void canbus_process_data(struct canbus_msg *msg);

#endif // canbus.h
