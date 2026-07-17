// Generic handling of serial over CAN support
//
// Copyright (C) 2019 Eug Krashtan <eug.krashtan@gmail.com>
// Copyright (C) 2020 Pontus Borg <glpontus@gmail.com>
// Copyright (C) 2021-2025  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_HAVE_BOOTLOADER_REQUEST
#include "board/io.h" // readb
#include "board/irq.h" // irq_save
#include "board/misc.h" // console_sendf
#include "canbus.h" // canbus_send
#include "canserial.h" // canserial_notify_tx
#include "command.h" // DECL_CONSTANT
#include "fasthash.h" // fasthash64
#include "sched.h" // sched_wake_task
#include "timesync.h" // timesync_ingest_can_sample

#define CANBUS_UUID_LEN 6
#define CANBUS_BOARD_ID_MAX 16

// Global storage
static struct canbus_data {
    uint32_t assigned_id;
    uint8_t uuid[CANBUS_UUID_LEN];
    uint8_t board_id[CANBUS_BOARD_ID_MAX];
    uint8_t board_id_len, board_id_family, board_id_crc;

    // Tx data
    struct task_wake tx_wake;
    uint8_t transmit_pos, transmit_max;
    uint8_t carrier_mtu, fd_active, fd_brs;
    uint8_t staged_mtu, staged_brs, transport_state;
    uint32_t transport_epoch;
    uint32_t active_data_bitrate, staged_data_bitrate;

    // Hardware-timestamped two-step CAN machine-time transfer
    uint32_t time_epoch, time_local_clock, time_last_rx;
    uint32_t time_matched, time_missed, time_invalid;
    uint8_t time_seq, time_quality, time_pending;
    uint32_t fd_error_window_start;
    uint8_t fd_error_count, fd_error_hold;

    // Rx data
    struct task_wake rx_wake;
    uint8_t receive_pos;
    uint32_t admin_pull_pos, admin_push_pos;

    // Transfer buffers
    struct canbus_msg admin_queue[8];
    uint8_t transmit_buf[192];
    uint8_t receive_buf[192];
} CanData;


/****************************************************************
 * Data transmission over CAN
 ****************************************************************/

static uint_fast8_t
canserial_carrier_wire_len(uint_fast8_t payload_len)
{
    if (payload_len <= 8)
        return payload_len;
    static const uint8_t fd_lengths[] = { 12, 16, 20, 24, 32, 48, 64 };
    for (uint_fast8_t i = 0; i < ARRAY_SIZE(fd_lengths); i++)
        if (payload_len <= fd_lengths[i])
            return fd_lengths[i];
    return 64;
}

static int
canserial_frame_logical_len(uint8_t *data, uint32_t wire_len)
{
    uint32_t pos = 0;
    while (pos < wire_len) {
        if (data[pos] == 0) {
            for (uint32_t i = pos + 1; i < wire_len; i++)
                if (data[i])
                    return -1;
            break;
        }
        uint32_t record_len = data[pos] == MESSAGE_SYNC ? 1 : data[pos];
        if ((record_len != 1
             && (record_len < MESSAGE_MIN || record_len > MESSAGE_MAX))
            || record_len > wire_len - pos)
            return -1;
        pos += record_len;
    }
    return pos;
}

void
canserial_notify_tx(void)
{
    sched_wake_task(&CanData.tx_wake);
}

void
canserial_notify_protocol_error(void)
{
    if (!readb(&CanData.fd_active))
        return;
    uint32_t now = timer_read_time();
    if (now - CanData.fd_error_window_start > timer_from_us(10000)) {
        CanData.fd_error_window_start = now;
        CanData.fd_error_count = 0;
    }
    if (++CanData.fd_error_count >= 8) {
        CanData.fd_error_hold = 1;
        sched_wake_task(&CanData.rx_wake);
    }
}

void
canserial_tx_task(void)
{
    if (!sched_check_wake(&CanData.tx_wake))
        return;
    uint32_t id = CanData.assigned_id;
    if (!id) {
        CanData.transmit_pos = CanData.transmit_max = 0;
        return;
    }
    struct canbus_msg msg = {};
    msg.id = id + 1;
    uint32_t tpos = CanData.transmit_pos, tmax = CanData.transmit_max;
    for (;;) {
        uint32_t mtu = CanData.carrier_mtu ? CanData.carrier_mtu : 8;
        int avail = tmax - tpos;
        if (avail <= 0)
            break;
        uint_fast8_t now;
        if (CanData.fd_active) {
            // Retain Klipper's write batching: pack as many complete protocol
            // records as fit, but never split one across FD frames.
            now = 0;
            while (now < avail) {
                uint_fast8_t first = CanData.transmit_buf[tpos + now];
                uint_fast8_t record_len = first == MESSAGE_SYNC ? 1 : first;
                if ((record_len != 1
                     && (record_len < MESSAGE_MIN
                         || record_len > MESSAGE_MAX))
                    || record_len > avail - now) {
                    CanData.transmit_pos = CanData.transmit_max = 0;
                    shutdown("Invalid CAN-FD transmit record");
                }
                if (now && now + record_len > MESSAGE_MAX)
                    break;
                now += record_len;
            }
            memset(msg.data, 0, sizeof(msg.data));
            msg.dlc = canserial_carrier_wire_len(now);
            msg.flags = CANMSG_FLAG_FD;
            if (CanData.fd_brs)
                msg.flags |= CANMSG_FLAG_BRS;
            memcpy(msg.data, &CanData.transmit_buf[tpos], now);
        } else {
            now = avail > mtu ? mtu : avail;
            msg.dlc = now;
            msg.flags = 0;
            memcpy(msg.data, &CanData.transmit_buf[tpos], now);
        }
        int ret = canbus_send(&msg);
        if (ret <= 0)
            break;
        tpos += now;
    }
    CanData.transmit_pos = tpos;
}
DECL_TASK(canserial_tx_task);


/****************************************************************
 * Carrier profile control
 ****************************************************************/

static void
canserial_report_transport(void)
{
    sendf("canbus_transport state=%c active=%c mtu=%c brs=%c"
          " data_bitrate=%u epoch=%u"
          , CanData.transport_state, CanData.fd_active, CanData.carrier_mtu
          , CanData.fd_brs
          , CanData.active_data_bitrate
          , CanData.transport_epoch);
}

static int
canserial_valid_mtu(uint32_t mtu)
{
    return (mtu == 8 || mtu == 12 || mtu == 16 || mtu == 20 || mtu == 24
            || mtu == 32 || mtu == 48 || mtu == 64);
}

static int
canserial_prepare_fd(uint32_t data_bitrate, uint8_t brs)
{
#if CONFIG_CANBUS_FD
    return canhw_prepare_fd(data_bitrate, brs);
#else
    return -1;
#endif
}

static uint32_t
canserial_fd_bitrate_mask(void)
{
#if CONFIG_CANBUS_FD
    return canhw_get_fd_bitrate_mask();
#else
    return 0;
#endif
}

void
command_set_canbus_transport(uint32_t *args)
{
    uint32_t active = args[0], mtu = args[1], brs = args[2];
    uint32_t data_bitrate = args[3];
    if (!active) {
        CanData.fd_active = CanData.fd_brs = 0;
        CanData.carrier_mtu = 8;
        CanData.transport_state = 0;
    } else if (!CONFIG_CANBUS_FD || mtu <= 8 || !canserial_valid_mtu(mtu)
               || canserial_prepare_fd(data_bitrate, brs)) {
        shutdown("Invalid CAN FD carrier profile");
    } else {
#if CONFIG_CANBUS_FD
        if (canhw_commit_fd())
            shutdown("Invalid CAN FD carrier profile");
#endif
        CanData.carrier_mtu = mtu;
        CanData.fd_brs = !!brs;
        CanData.fd_active = 1;
        CanData.transport_state = 2;
        CanData.active_data_bitrate = data_bitrate;
    }
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_set_canbus_transport, HF_IN_SHUTDOWN,
                   "set_canbus_transport active=%c mtu=%c brs=%c"
                   " data_bitrate=%u");

void
command_prepare_canbus_transport(uint32_t *args)
{
    uint32_t mtu = args[0], brs = args[1], data_bitrate = args[2];
    uint32_t epoch = args[3];
    if (!CONFIG_CANBUS_FD || mtu <= 8 || !canserial_valid_mtu(mtu)
        || canserial_prepare_fd(data_bitrate, brs))
        shutdown("Unsupported CAN FD transport profile");
    CanData.staged_mtu = mtu;
    CanData.staged_brs = !!brs;
    CanData.staged_data_bitrate = data_bitrate;
    CanData.transport_epoch = epoch;
    CanData.transport_state = 1;
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_prepare_canbus_transport, HF_IN_SHUTDOWN,
                   "prepare_canbus_transport mtu=%c brs=%c"
                   " data_bitrate=%u epoch=%u");

void
command_commit_canbus_transport(uint32_t *args)
{
    uint32_t epoch = args[0];
    if (CanData.transport_state != 1 || epoch != CanData.transport_epoch)
        shutdown("CAN FD transport commit failed");
#if CONFIG_CANBUS_FD
    if (canhw_commit_fd())
        shutdown("CAN FD transport commit failed");
#endif
    // Carrier emission remains Classical until ENABLE. The controller now
    // accepts the staged FD profile and can receive the host's enable frame.
    CanData.transport_state = 1;
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_commit_canbus_transport, HF_IN_SHUTDOWN,
                   "commit_canbus_transport epoch=%u");

void
command_enable_canbus_transport(uint32_t *args)
{
    uint32_t epoch = args[0];
    if (CanData.transport_state != 1 || epoch != CanData.transport_epoch)
        shutdown("CAN FD transport epoch mismatch");
    CanData.carrier_mtu = CanData.staged_mtu;
    CanData.fd_brs = CanData.staged_brs;
    CanData.active_data_bitrate = CanData.staged_data_bitrate;
    CanData.fd_active = 1;
    CanData.transport_state = 2;
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_enable_canbus_transport, HF_IN_SHUTDOWN,
                   "enable_canbus_transport epoch=%u");

void
command_abort_canbus_transport(uint32_t *args)
{
    CanData.fd_active = CanData.fd_brs = 0;
    CanData.carrier_mtu = 8;
    CanData.staged_mtu = CanData.staged_brs = 0;
    CanData.staged_data_bitrate = 0;
    CanData.transport_state = 0;
    CanData.transport_epoch = args[0];
    CanData.active_data_bitrate = CONFIG_CANBUS_FREQUENCY;
#if CONFIG_CANBUS_FD
    canhw_abort_fd();
#endif
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_abort_canbus_transport, HF_IN_SHUTDOWN,
                   "abort_canbus_transport epoch=%u");

void
command_get_canbus_transport(uint32_t *args)
{
    canserial_report_transport();
}
DECL_COMMAND_FLAGS(command_get_canbus_transport, HF_IN_SHUTDOWN,
                   "get_canbus_transport");

void
command_get_canbus_capabilities(uint32_t *args)
{
    sendf("canbus_capabilities fd=%c bitrate_mask=%u max_payload=%c"
          " transceiver_max=%u"
          , CONFIG_CANBUS_FD, canserial_fd_bitrate_mask()
          , CONFIG_CANBUS_FD ? 64 : 8
          , CONFIG_CANBUS_FD ? CONFIG_CANBUS_TRANSCEIVER_MAX_DATA_RATE
                             : CONFIG_CANBUS_FREQUENCY);
}
DECL_COMMAND_FLAGS(command_get_canbus_capabilities, HF_IN_SHUTDOWN,
                   "get_canbus_capabilities");

// Encode and transmit a "response" message
void
console_sendf(const struct command_encoder *ce, va_list args)
{
    // Verify space for message
    uint32_t tpos = CanData.transmit_pos, tmax = CanData.transmit_max;
    if (tpos >= tmax)
        CanData.transmit_pos = CanData.transmit_max = tpos = tmax = 0;
    if (tmax + ce->max_size > sizeof(CanData.transmit_buf)) {
        if (tmax - tpos + ce->min_size > sizeof(CanData.transmit_buf))
            // Not enough space for message
            return;
        // Move buffer
        tmax -= tpos;
        memmove(&CanData.transmit_buf[0], &CanData.transmit_buf[tpos], tmax);
        CanData.transmit_pos = tpos = 0;
        CanData.transmit_max = tmax;
    }

    // Generate message
    uint32_t msglen = command_encode_and_frame(
        &CanData.transmit_buf[tmax], sizeof(CanData.transmit_buf) - tmax
        , ce, args);
    if (!msglen)
        return;

    // Start message transmit
    CanData.transmit_max = tmax + msglen;
    canserial_notify_tx();
}


/****************************************************************
 * CAN "admin" command handling
 ****************************************************************/

// Available commands and responses
#define CANBUS_CMD_QUERY_UNASSIGNED 0x00
#define CANBUS_CMD_SET_KLIPPER_NODEID 0x01
#define CANBUS_CMD_REQUEST_BOOTLOADER 0x02
#define CANBUS_CMD_QUERY_BOARD_ID 0x03
#define CANBUS_CMD_QUERY_ASSIGNED 0x04
#define CANBUS_RESP_NEED_NODEID 0x20
#define CANBUS_RESP_BOARD_ID 0x21
#define CANBUS_RESP_ASSIGNED_ID 0x22
#define CANBUS_RESP_SESSION_RESET 0x23

enum {
    CANBUS_FAMILY_GENERIC,
    CANBUS_FAMILY_STM32,
    CANBUS_FAMILY_RP2040,
    CANBUS_FAMILY_ATSAM,
    CANBUS_FAMILY_ATSAMD,
    CANBUS_FAMILY_LPC176X,
};

static uint8_t
can_board_id_crc(uint8_t family, const uint8_t *data, uint32_t len)
{
    uint8_t crc = 0x5a ^ family ^ len;
    for (uint32_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint_fast8_t bit = 0; bit < 8; bit++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x07 : crc << 1;
    }
    return crc;
}

// Helper to verify a UUID in a command matches this chip's UUID
static int
can_check_uuid(struct canbus_msg *msg)
{
    return (msg->dlc >= 7
            && memcmp(&msg->data[1], CanData.uuid, sizeof(CanData.uuid)) == 0);
}

// Helpers to encode/decode a CAN identifier to a 1-byte "nodeid"
static int
can_get_nodeid(void)
{
    if (!CanData.assigned_id)
        return 0;
    return (CanData.assigned_id - 0x100) >> 1;
}
static uint32_t
can_decode_nodeid(int nodeid)
{
    return (nodeid << 1) + 0x100;
}

static void
can_process_query_unassigned(struct canbus_msg *msg)
{
    if (CanData.assigned_id)
        return;
    struct canbus_msg send = {};
    send.id = CANBUS_ID_ADMIN_RESP;
    send.dlc = 8;
    send.data[0] = CANBUS_RESP_NEED_NODEID;
    memcpy(&send.data[1], CanData.uuid, sizeof(CanData.uuid));
    send.data[7] = CANBUS_CMD_SET_KLIPPER_NODEID;
    // Send with retry
    for (;;) {
        int ret = canbus_send(&send);
        if (ret >= 0)
            return;
    }
}

static void
can_process_query_assigned(struct canbus_msg *msg)
{
    if (!CanData.assigned_id)
        return;
    struct canbus_msg send = {};
    send.id = CANBUS_ID_ADMIN_RESP;
    send.dlc = 8;
    send.data[0] = CANBUS_RESP_ASSIGNED_ID;
    memcpy(&send.data[1], CanData.uuid, sizeof(CanData.uuid));
    send.data[7] = can_get_nodeid();
    // This read-only response lets a restarted/redundant host recover the
    // canonical board-id mapping without clearing a live node assignment.
    for (;;) {
        int ret = canbus_send(&send);
        if (ret >= 0)
            return;
    }
}

static void
can_id_conflict(void)
{
    CanData.assigned_id = 0;
    canbus_set_filter(CanData.assigned_id);
    shutdown("Another CAN node assigned this ID");
}

static void
can_reset_host_session(void)
{
    // SET_KLIPPER_NODEID is sent out-of-band before a host opens its framed
    // stream.  Make it a session-takeover barrier: bytes and replies from the
    // prior process cannot enter the new session, and every reconnect begins
    // on the mandatory Classical carrier before negotiating FD again.
    CanData.transmit_pos = CanData.transmit_max = 0;
    CanData.receive_pos = 0;
    CanData.fd_active = CanData.fd_brs = 0;
    CanData.carrier_mtu = 8;
    CanData.staged_mtu = CanData.staged_brs = 0;
    CanData.staged_data_bitrate = 0;
    CanData.transport_state = 0;
    CanData.active_data_bitrate = CONFIG_CANBUS_FREQUENCY;
#if CONFIG_CANBUS_FD
    canhw_abort_fd();
#endif
    command_reset_sequence();
}

static void
can_report_session_reset(void)
{
    struct canbus_msg send = {};
    send.id = CANBUS_ID_ADMIN_RESP;
    send.dlc = 8;
    send.data[0] = CANBUS_RESP_SESSION_RESET;
    memcpy(&send.data[1], CanData.uuid, sizeof(CanData.uuid));
    send.data[7] = can_get_nodeid();
    for (;;) {
        int ret = canbus_send(&send);
        if (ret >= 0)
            return;
    }
}

static void
can_process_set_klipper_nodeid(struct canbus_msg *msg)
{
    if (msg->dlc < 8)
        return;
    uint32_t newid = can_decode_nodeid(msg->data[7]);
    if (can_check_uuid(msg)) {
        if (newid != CanData.assigned_id) {
            CanData.assigned_id = newid;
            canbus_set_filter(CanData.assigned_id);
        }
        can_reset_host_session();
        can_report_session_reset();
    } else if (newid == CanData.assigned_id) {
        can_id_conflict();
    }
}

static void
can_process_request_bootloader(struct canbus_msg *msg)
{
    if (!CONFIG_HAVE_BOOTLOADER_REQUEST || !can_check_uuid(msg))
        return;
    bootloader_request();
}

static void
can_process_query_board_id(struct canbus_msg *msg)
{
    if (msg->dlc < 8 || !can_check_uuid(msg))
        return;
    uint32_t offset = msg->data[7];
    if (offset >= CanData.board_id_len)
        return;
    struct canbus_msg send = {};
    send.id = CANBUS_ID_ADMIN_RESP;
    send.dlc = 8;
    send.data[0] = CANBUS_RESP_BOARD_ID;
    send.data[1] = CanData.board_id_family;
    send.data[2] = CanData.board_id_len;
    send.data[3] = offset;
    uint32_t count = CanData.board_id_len - offset;
    if (count > 3)
        count = 3;
    memcpy(&send.data[4], &CanData.board_id[offset], count);
    send.data[7] = CanData.board_id_crc;
    // The response is Classical CAN and uses the same bounded queue retry as
    // legacy discovery. A duplicate legacy handle causes multiple replies;
    // the host treats that as a collision instead of guessing.
    for (;;) {
        int ret = canbus_send(&send);
        if (ret >= 0)
            return;
    }
}

// Handle an "admin" command
static void
can_process_admin(struct canbus_msg *msg)
{
    if (!msg->dlc)
        return;
    switch (msg->data[0]) {
    case CANBUS_CMD_QUERY_UNASSIGNED:
        can_process_query_unassigned(msg);
        break;
    case CANBUS_CMD_SET_KLIPPER_NODEID:
        can_process_set_klipper_nodeid(msg);
        break;
    case CANBUS_CMD_REQUEST_BOOTLOADER:
        can_process_request_bootloader(msg);
        break;
    case CANBUS_CMD_QUERY_BOARD_ID:
        can_process_query_board_id(msg);
        break;
    case CANBUS_CMD_QUERY_ASSIGNED:
        can_process_query_assigned(msg);
        break;
    }
}

static void
can_process_time(struct canbus_msg *msg)
{
    if (msg->dlc != 8 || msg->data[0] != CANBUS_TIME_MAGIC)
        return;
    uint8_t type = msg->data[1], seq = msg->data[2];
    if (type == CANBUS_TIME_SYNC) {
        if (!(msg->flags & CANMSG_FLAG_HW_TIMESTAMP)) {
            CanData.time_invalid++;
            return;
        }
        uint32_t epoch;
        memcpy(&epoch, &msg->data[4], sizeof(epoch));
        if (CanData.time_pending && CanData.time_seq != seq)
            CanData.time_missed++;
        if (epoch != CanData.time_epoch) {
            CanData.time_epoch = epoch;
            CanData.time_pending = 0;
        }
        CanData.time_seq = seq;
        CanData.time_quality = msg->data[3];
        CanData.time_local_clock = msg->hw_clock;
        CanData.time_last_rx = timer_read_time();
        CanData.time_pending = 1;
        return;
    }
    if (type != CANBUS_TIME_FOLLOWUP || !CanData.time_pending
        || CanData.time_seq != seq) {
        CanData.time_missed++;
        return;
    }
    uint32_t machine_clock;
    memcpy(&machine_clock, &msg->data[4], sizeof(machine_clock));
    timesync_ingest_can_sample(seq, machine_clock,
                               CanData.time_local_clock);
    CanData.time_pending = 0;
    CanData.time_matched++;
}


/****************************************************************
 * CAN packet reading
 ****************************************************************/

static void
canserial_notify_rx(void)
{
    sched_wake_task(&CanData.rx_wake);
}

DECL_CONSTANT("RECEIVE_WINDOW", ARRAY_SIZE(CanData.receive_buf));

// Handle incoming data (called from IRQ handler)
void
canserial_process_data(struct canbus_msg *msg)
{
    uint32_t id = msg->id;
    if (CanData.assigned_id && id == CanData.assigned_id) {
        uint32_t len = CANMSG_DATA_LEN(msg);
        if (msg->flags & CANMSG_FLAG_FD) {
            int record_len = canserial_frame_logical_len(msg->data, len);
            if (record_len <= 0) {
                canserial_notify_protocol_error();
                return;
            }
            int rpos = CanData.receive_pos;
            if (record_len > sizeof(CanData.receive_buf) - rpos)
                return;
            memcpy(&CanData.receive_buf[rpos], msg->data, record_len);
            CanData.receive_pos = rpos + record_len;
            canserial_notify_rx();
        } else {
            // Classical bootstrap and transition frames retain the original
            // raw byte-stream encoding.
            int rpos = CanData.receive_pos;
            if (len > sizeof(CanData.receive_buf) - rpos)
                return;
            memcpy(&CanData.receive_buf[rpos], msg->data, len);
            CanData.receive_pos = rpos + len;
            canserial_notify_rx();
        }
    } else if (id == CANBUS_ID_ADMIN || id == CANBUS_ID_TIME_SYNC
               || id == CANBUS_ID_TIME_FOLLOWUP
               || (CanData.assigned_id && id == CanData.assigned_id + 1)) {
        // Add to admin command queue
        uint32_t pushp = CanData.admin_push_pos;
        if (pushp >= CanData.admin_pull_pos + ARRAY_SIZE(CanData.admin_queue))
            // No space - drop message
            return;
        uint32_t pos = pushp % ARRAY_SIZE(CanData.admin_queue);
        memcpy(&CanData.admin_queue[pos], msg, sizeof(*msg));
        CanData.admin_push_pos = pushp + 1;
        canserial_notify_rx();
    }
}

// Remove from the receive buffer the given number of bytes
static void
console_pop_input(int len)
{
    int copied = 0;
    for (;;) {
        int rpos = readb(&CanData.receive_pos);
        int needcopy = rpos - len;
        if (needcopy) {
            memmove(&CanData.receive_buf[copied]
                    , &CanData.receive_buf[copied + len], needcopy - copied);
            copied = needcopy;
            canserial_notify_rx();
        }
        irqstatus_t flag = irq_save();
        if (rpos != readb(&CanData.receive_pos)) {
            // Raced with irq handler - retry
            irq_restore(flag);
            continue;
        }
        CanData.receive_pos = needcopy;
        irq_restore(flag);
        break;
    }
}

// Task to process incoming commands and admin messages
void
canserial_rx_task(void)
{
    if (!sched_check_wake(&CanData.rx_wake))
        return;

    if (CanData.fd_error_hold) {
        CanData.fd_error_hold = 0;
        CanData.fd_active = CanData.fd_brs = 0;
        CanData.carrier_mtu = 8;
        CanData.transport_state = 3;
#if CONFIG_CANBUS_FD
        canhw_abort_fd();
#endif
        shutdown("CAN FD protocol error burst");
    }

    // Process pending admin messages
    for (;;) {
        uint32_t pushp = readl(&CanData.admin_push_pos);
        uint32_t pullp = CanData.admin_pull_pos;
        if (pushp == pullp)
            break;
        uint32_t pos = pullp % ARRAY_SIZE(CanData.admin_queue);
        struct canbus_msg *msg = &CanData.admin_queue[pos];
        uint32_t id = msg->id;
        if (CanData.assigned_id && id == CanData.assigned_id + 1)
            can_id_conflict();
        else if (id == CANBUS_ID_ADMIN)
            can_process_admin(msg);
        else if (id == CANBUS_ID_TIME_SYNC
                 || id == CANBUS_ID_TIME_FOLLOWUP)
            can_process_time(msg);
        CanData.admin_pull_pos = pullp + 1;
    }

    // Check for a complete message block and process it
    uint_fast8_t rpos = readb(&CanData.receive_pos), pop_count;
    int ret = command_find_block(CanData.receive_buf, rpos, &pop_count);
    if (ret > 0)
        command_dispatch(CanData.receive_buf, pop_count);
    if (ret) {
        console_pop_input(pop_count);
        if (ret > 0)
            command_send_ack();
    }
}
DECL_TASK(canserial_rx_task);


/****************************************************************
 * Setup and shutdown
 ****************************************************************/

DECL_ENUMERATION("canbus_bus_state", "active", CANBUS_STATE_ACTIVE);
DECL_ENUMERATION("canbus_bus_state", "warn", CANBUS_STATE_WARN);
DECL_ENUMERATION("canbus_bus_state", "passive", CANBUS_STATE_PASSIVE);
DECL_ENUMERATION("canbus_bus_state", "off", CANBUS_STATE_OFF);

void
command_get_canbus_status(uint32_t *args)
{
    struct canbus_status status;
    memset(&status, 0, sizeof(status));
    canhw_get_status(&status);
    sendf("canbus_status rx_error=%u tx_error=%u tx_retries=%u"
          " canbus_bus_state=%u"
          , status.rx_error, status.tx_error, status.tx_retries
          , status.bus_state);
}
DECL_COMMAND_FLAGS(command_get_canbus_status, HF_IN_SHUTDOWN
                   , "get_canbus_status");

void
command_get_can_time_status(uint32_t *args)
{
    uint32_t age = timer_read_time() - CanData.time_last_rx;
    sendf("can_time_status epoch=%u pending=%c quality=%c matched=%u"
          " missed=%u invalid=%u age_ticks=%u"
          , CanData.time_epoch, CanData.time_pending, CanData.time_quality
          , CanData.time_matched, CanData.time_missed
          , CanData.time_invalid, age);
}
DECL_COMMAND_FLAGS(command_get_can_time_status, HF_IN_SHUTDOWN,
                   "get_can_time_status");

void
command_get_canbus_id(uint32_t *args)
{
    sendf("canbus_id canbus_uuid=%.*s canbus_nodeid=%u"
          , sizeof(CanData.uuid), CanData.uuid, can_get_nodeid());
}
DECL_COMMAND_FLAGS(command_get_canbus_id, HF_IN_SHUTDOWN, "get_canbus_id");

void
canserial_set_uuid(uint8_t *raw_uuid, uint32_t raw_uuid_len)
{
    uint64_t hash = fasthash64(raw_uuid, raw_uuid_len, 0xA16231A7);
    memcpy(CanData.uuid, &hash, sizeof(CanData.uuid));
    uint32_t board_id_len = raw_uuid_len;
    if (board_id_len > sizeof(CanData.board_id))
        board_id_len = sizeof(CanData.board_id);
    memcpy(CanData.board_id, raw_uuid, board_id_len);
    CanData.board_id_len = board_id_len;
#if CONFIG_MACH_STM32
    CanData.board_id_family = CANBUS_FAMILY_STM32;
#elif CONFIG_MACH_RPXXXX
    CanData.board_id_family = CANBUS_FAMILY_RP2040;
#elif CONFIG_MACH_ATSAM
    CanData.board_id_family = CANBUS_FAMILY_ATSAM;
#elif CONFIG_MACH_ATSAMD
    CanData.board_id_family = CANBUS_FAMILY_ATSAMD;
#elif CONFIG_MACH_LPC176X
    CanData.board_id_family = CANBUS_FAMILY_LPC176X;
#else
    CanData.board_id_family = CANBUS_FAMILY_GENERIC;
#endif
    CanData.board_id_crc = can_board_id_crc(
        CanData.board_id_family, CanData.board_id, board_id_len);
    CanData.carrier_mtu = 8;
    CanData.active_data_bitrate = CONFIG_CANBUS_FREQUENCY;
    canserial_notify_rx();
}

void
canserial_shutdown(void)
{
    canserial_notify_tx();
    canserial_notify_rx();
}
DECL_SHUTDOWN(canserial_shutdown);
