// Authenticated UDP binding for the typed Helix gateway protocol.

#include <string.h>
#include "autoconf.h"
#include "board/canbus.h"
#include "board/irq.h"
#include "board/io.h"
#include "board/misc.h"
#include "command.h"
#include "generic/can_gateway.h"
#include "gateway_protocol.h"
#include "gateway_runtime.h"
#include "sched.h"
#include "udp_datagram.h"
#include "udp_gateway.h"

#define GATEWAY_CAN_RX_DEPTH 256
#define GATEWAY_CAN_TX_DEPTH 64
#define GATEWAY_LINK_TX_DEPTH 32
#define GATEWAY_SESSION_HANDSHAKE_TIMEOUT_US 2000000

static const struct udp_console_ops *GatewayOps;
static void *GatewayOpsCtx;
static struct task_wake GatewayWake;
static struct helix_gateway_runtime GatewayRuntime;
static struct can_gateway_queue GatewayCanRx, GatewayCanTx;
static struct canbus_msg GatewayCanRxStorage[GATEWAY_CAN_RX_DEPTH];
static struct canbus_msg GatewayCanTxStorage[GATEWAY_CAN_TX_DEPTH];
static uint8_t GatewayRxDatagram[UDPDG_DATAGRAM_MAX];
static uint8_t GatewayTxDatagram[UDPDG_DATAGRAM_MAX];
static uint8_t GatewayWire[UDPDG_FRAMES_MAX];
static struct {
    uint8_t service, opcode;
    uint16_t channel, flags, length;
    uint32_t cookie;
    uint8_t data[HELIX_GATEWAY_MAX_RECORD_DATA];
} GatewayLinkTx[GATEWAY_LINK_TX_DEPTH];
static volatile uint32_t GatewayLinkTxPull, GatewayLinkTxPush;
static uint16_t GatewayPendingDatagramLength, GatewayPendingRecordCount;
static uint8_t GatewayConsoleRx[2 * UDPDG_FRAMES_MAX];
static uint32_t GatewayConsoleRxPos;
static uint32_t GatewayEpoch, GatewaySequence;
static uint32_t GatewayTxPackets, GatewayTxDrops, GatewayCanBusOff;
static uint32_t GatewayCanAdmitted, GatewayCanSubmitted;
static uint32_t GatewayCanCompleted, GatewayCanFailed, GatewayCanUnknown;
static struct helix_gateway_can_config GatewayPreparedProfile;
static struct helix_gateway_can_config GatewayActiveProfile;
static uint8_t GatewayProfilePrepared, GatewayFdActive, GatewayBrsActive;
static struct {
    struct helix_gateway_can_config request, response;
    uint32_t cookie;
    uint8_t valid, error;
} GatewayConfigCache[4];
static struct {
    uint8_t tag;
    uint32_t cookie;
} GatewaySubmitted[GATEWAY_CAN_TX_DEPTH];
static uint8_t GatewaySubmittedCount;
static uint8_t GatewayNextTag;
static struct {
    uint8_t tag;
    uint8_t failed;
    uint32_t local_clock;
} GatewayTxEvents[GATEWAY_CAN_TX_DEPTH];
static volatile uint32_t GatewayTxEventPull, GatewayTxEventPush;
static volatile uint8_t GatewayTxEventLost, GatewayBusOffPending;
#if CONFIG_WANT_DATAGRAM_SESSION
static uint32_t GatewayHandshakeDeadline;
#endif

static int gateway_emit(uint8_t service, uint8_t opcode, uint16_t channel,
                        uint16_t flags, uint32_t cookie,
                        const uint8_t *data, uint16_t length);

static int
gateway_emit_ack(void)
{
    struct helix_gateway_ack ack;
    uint8_t data[12];
    if (helix_gateway_runtime_get_ack(&GatewayRuntime, &ack)
        || helix_gateway_ack_encode(data, sizeof(data), &ack) < 0)
        return -1;
    return gateway_emit(HELIX_GATEWAY_SERVICE_CONTROL,
                        HELIX_GATEWAY_CONTROL_ACK, 0,
                        HELIX_GATEWAY_RECORD_REPLY, ack.sequence,
                        data, sizeof(data));
}

static uint32_t
gateway_link_free(void)
{
    return ARRAY_SIZE(GatewayLinkTx)
           - (GatewayLinkTxPush - GatewayLinkTxPull);
}

static int
gateway_emit_delivery(uint8_t state, uint8_t error, uint32_t cookie,
                      uint32_t hw_clock, uint32_t detail)
{
    uint8_t data[16];
    struct helix_gateway_delivery delivery = {
        .state = state, .error = error, .cookie = cookie,
        .hw_clock = hw_clock, .detail = detail,
    };
    if (helix_gateway_delivery_encode(data, sizeof(data), &delivery) < 0)
        return -1;
    return gateway_emit(HELIX_GATEWAY_SERVICE_CAN,
                        HELIX_GATEWAY_CAN_DELIVERY, 0,
                        hw_clock ? HELIX_GATEWAY_RECORD_TIMESTAMP_VALID : 0,
                        cookie, data, sizeof(data));
}

static int
gateway_can_config(const struct helix_gateway_record *record)
{
    struct helix_gateway_can_config config;
    if (helix_gateway_can_config_decode(&config, record->data,
                                        record->length) < 0)
        return -1;
    struct helix_gateway_can_config request = config;
    uint8_t action = config.action;
    if (GatewayConfigCache[action].valid
        && GatewayConfigCache[action].cookie == record->cookie) {
        struct helix_gateway_can_config *old =
            &GatewayConfigCache[action].request;
        int same = old->action == config.action && old->fd == config.fd
                   && old->brs == config.brs && old->epoch == config.epoch
                   && old->nominal_bitrate == config.nominal_bitrate
                   && old->data_bitrate == config.data_bitrate;
        uint8_t data[16];
        helix_gateway_can_config_encode(
            data, sizeof(data), &GatewayConfigCache[action].response);
        gateway_emit(HELIX_GATEWAY_SERVICE_CAN, HELIX_GATEWAY_CAN_CONFIG, 0,
                     HELIX_GATEWAY_RECORD_REPLY
                     | ((!same || GatewayConfigCache[action].error)
                        ? HELIX_GATEWAY_RECORD_ERROR : 0),
                     record->cookie, data, sizeof(data));
        helix_gateway_runtime_add_credits(&GatewayRuntime,
                                          HELIX_GATEWAY_SERVICE_CAN, 1);
        return 0;
    }
    int ret = 0;
    if (config.action == HELIX_GATEWAY_CAN_CONFIG_QUERY) {
        config.nominal_bitrate = (GatewayActiveProfile.nominal_bitrate
                                  ? GatewayActiveProfile.nominal_bitrate
                                  : CONFIG_CANBUS_FREQUENCY);
#if CONFIG_CANBUS_FD
        config.fd = GatewayFdActive;
        config.data_bitrate = (GatewayActiveProfile.data_bitrate
                               ? GatewayActiveProfile.data_bitrate
                               : CONFIG_CANBUS_FREQUENCY);
        config.brs = GatewayBrsActive;
#else
        config.data_bitrate = CONFIG_CANBUS_FREQUENCY;
        config.brs = 0;
#endif
    } else if (config.action == HELIX_GATEWAY_CAN_CONFIG_PREPARE) {
        if (can_gateway_queue_depth(&GatewayCanTx) || GatewaySubmittedCount
            || config.nominal_bitrate != CONFIG_CANBUS_FREQUENCY)
            ret = -1;
#if CONFIG_CANBUS_FD
        else if (config.fd
                 && canhw_prepare_fd(config.data_bitrate, config.brs))
            ret = -1;
#else
        else if (config.nominal_bitrate != CONFIG_CANBUS_FREQUENCY
                 || config.data_bitrate != CONFIG_CANBUS_FREQUENCY
                 || config.fd || config.brs)
            ret = -1;
#endif
        if (!ret) {
            GatewayPreparedProfile = config;
            GatewayProfilePrepared = 1;
        }
    } else if (config.action == HELIX_GATEWAY_CAN_CONFIG_COMMIT) {
        if (!GatewayProfilePrepared
            || GatewayPreparedProfile.epoch != config.epoch
            || GatewayPreparedProfile.nominal_bitrate
               != config.nominal_bitrate
            || GatewayPreparedProfile.data_bitrate != config.data_bitrate
            || GatewayPreparedProfile.fd != config.fd
            || GatewayPreparedProfile.brs != config.brs)
            ret = -1;
#if CONFIG_CANBUS_FD
        else if (GatewayPreparedProfile.fd) {
            if (canhw_commit_fd())
                ret = -1;
        } else {
            canhw_abort_fd();
        }
#endif
        if (!ret) {
            GatewayFdActive = GatewayPreparedProfile.fd;
            GatewayBrsActive = GatewayPreparedProfile.brs;
            GatewayActiveProfile = GatewayPreparedProfile;
            GatewayProfilePrepared = 0;
        }
    } else {
#if CONFIG_CANBUS_FD
        canhw_abort_fd();
#endif
        GatewayProfilePrepared = 0;
        GatewayFdActive = GatewayBrsActive = 0;
        memset(&GatewayActiveProfile, 0, sizeof(GatewayActiveProfile));
    }
    uint8_t data[16];
    GatewayConfigCache[action].request = request;
    GatewayConfigCache[action].response = config;
    GatewayConfigCache[action].cookie = record->cookie;
    GatewayConfigCache[action].valid = 1;
    GatewayConfigCache[action].error = !!ret;
    helix_gateway_can_config_encode(data, sizeof(data), &config);
    gateway_emit(HELIX_GATEWAY_SERVICE_CAN, HELIX_GATEWAY_CAN_CONFIG, 0,
                 HELIX_GATEWAY_RECORD_REPLY
                 | (ret ? HELIX_GATEWAY_RECORD_ERROR : 0),
                 record->cookie, data, sizeof(data));
    helix_gateway_runtime_add_credits(&GatewayRuntime,
                                      HELIX_GATEWAY_SERVICE_CAN, 1);
    // The transaction result is carried in the authenticated reply. The
    // record itself was consumed successfully even when the profile failed.
    return 0;
}

static int
gateway_control_submit(void *ctx, const struct helix_gateway_record *record)
{
    (void)ctx;
    if (record->opcode == HELIX_GATEWAY_CONTROL_ACK) {
        struct helix_gateway_ack ack;
        if (helix_gateway_ack_decode(&ack, record->data, record->length) < 0)
            return -1;
        // Gateway-originated data packets are intentionally not blindly
        // replayed.  The ACK still provides explicit loss/accounting evidence
        // and reserves the wire format for bounded idempotent replies.
        helix_gateway_runtime_add_credits(&GatewayRuntime,
                                          HELIX_GATEWAY_SERVICE_CONTROL, 1);
        return 0;
    }
    if (record->opcode != HELIX_GATEWAY_CONTROL_CONSOLE
        || GatewayConsoleRxPos + record->length > sizeof(GatewayConsoleRx))
        return -1;
    memcpy(GatewayConsoleRx + GatewayConsoleRxPos, record->data,
           record->length);
    GatewayConsoleRxPos += record->length;
    helix_gateway_runtime_add_credits(&GatewayRuntime,
                                      HELIX_GATEWAY_SERVICE_CONTROL, 1);
    return 0;
}

static int
gateway_can_submit(void *ctx, const struct helix_gateway_record *record)
{
    (void)ctx;
    if (record->opcode == HELIX_GATEWAY_CAN_CONFIG)
        return gateway_can_config(record);
    if (record->opcode != HELIX_GATEWAY_CAN_FRAME)
        return -1;
    struct helix_gateway_can_frame wire;
    if (helix_gateway_can_decode(&wire, record->data, record->length) < 0)
        return -1;
#if !CONFIG_CANBUS_FD
    if ((wire.flags & CANMSG_FLAG_FD) || wire.length > 8)
        return -1;
#endif
    if ((wire.flags & CANMSG_FLAG_FD) && !GatewayFdActive)
        return -1;
    if ((wire.flags & CANMSG_FLAG_BRS) && !GatewayBrsActive)
        return -1;
    if (!gateway_link_free())
        return -2;
    struct canbus_msg message = {};
    message.id = wire.can_id;
    message.dlc = wire.length;
    message.flags = wire.flags | CANMSG_FLAG_TX_EVENT;
    message.tx_tag = 0;
    // A host-originated frame has no RX timestamp. Preserve the full cookie
    // in this otherwise-unused field while the bounded TX queue owns it.
    message.hw_clock = record->cookie;
    memcpy(message.data, wire.data, wire.length);
    int ret = can_gateway_queue_push(&GatewayCanTx, &message);
    if (!ret) {
        if (gateway_emit_delivery(HELIX_GATEWAY_DELIVERY_ADMITTED, 0,
                                  record->cookie, 0,
                                  can_gateway_queue_depth(&GatewayCanTx)) < 0) {
            GatewayCanTx.push_pos--;
            GatewayCanTx.received--;
            return -2;
        }
        GatewayCanAdmitted++;
    }
    return ret;
}

int __attribute__((weak))
gateway_serial_write(uint16_t channel, const uint8_t *data, uint16_t length)
{
    (void)channel; (void)data; (void)length;
    return -1;
}

int __attribute__((weak))
gateway_serial_configure(uint16_t channel, const uint8_t *data,
                         uint16_t length)
{
    (void)channel; (void)data; (void)length;
    return -1;
}

int __attribute__((weak))
gateway_serial_break(uint16_t channel, uint32_t duration_us)
{
    (void)channel; (void)duration_us;
    return -1;
}

static int
gateway_serial_submit(void *ctx, const struct helix_gateway_record *record)
{
    (void)ctx;
    int ret = -1;
    if (record->opcode == HELIX_GATEWAY_SERIAL_DATA)
        ret = gateway_serial_write(record->channel, record->data,
                                   record->length);
    else if (record->opcode == HELIX_GATEWAY_SERIAL_CONFIG)
        ret = gateway_serial_configure(record->channel, record->data,
                                       record->length);
    if (record->opcode == HELIX_GATEWAY_SERIAL_BREAK
        && record->length == sizeof(uint32_t)) {
        uint32_t duration;
        memcpy(&duration, record->data, sizeof(duration));
        ret = gateway_serial_break(record->channel, duration);
    }
    if (ret >= 0)
        helix_gateway_runtime_add_credits(&GatewayRuntime,
                                          HELIX_GATEWAY_SERVICE_SERIAL, 1);
    return ret;
}

static void
gateway_control_reset(void *ctx, uint32_t epoch)
{
    (void)ctx; (void)epoch;
    GatewayConsoleRxPos = 0;
}

static void
gateway_can_reset(void *ctx, uint32_t epoch)
{
    (void)ctx; (void)epoch;
    irqstatus_t irqflag = irq_save();
    uint32_t queued = GatewayCanTx.push_pos - GatewayCanTx.pull_pos;
    GatewayCanTx.pull_pos = GatewayCanTx.push_pos;
    GatewayTxEventPull = GatewayTxEventPush;
    irq_restore(irqflag);
    GatewayCanUnknown += queued + GatewaySubmittedCount;
    GatewaySubmittedCount = 0;
    GatewayProfilePrepared = 0;
    GatewayFdActive = GatewayBrsActive = 0;
    memset(&GatewayActiveProfile, 0, sizeof(GatewayActiveProfile));
    memset(GatewayConfigCache, 0, sizeof(GatewayConfigCache));
#if CONFIG_CANBUS_FD
    canhw_abort_fd();
#endif
}

static const struct helix_gateway_service_ops GatewayControlOps = {
    .submit = gateway_control_submit,
    .reset = gateway_control_reset,
};
static const struct helix_gateway_service_ops GatewayCanOps = {
    .submit = gateway_can_submit,
    .reset = gateway_can_reset,
};
static const struct helix_gateway_service_ops GatewaySerialOps = {
    .submit = gateway_serial_submit,
};

static int
gateway_emit(uint8_t service, uint8_t opcode, uint16_t channel,
             uint16_t flags, uint32_t cookie,
             const uint8_t *data, uint16_t length)
{
    if (service >= HELIX_GATEWAY_MAX_SERVICES
        || length > HELIX_GATEWAY_MAX_RECORD_DATA || (length && !data))
        return -1;
    irqstatus_t irqflag = irq_save();
    uint32_t push = GatewayLinkTxPush;
    if (push - GatewayLinkTxPull >= ARRAY_SIZE(GatewayLinkTx)) {
        GatewayTxDrops++;
        irq_restore(irqflag);
        return -1;
    }
    uint32_t pos = push % ARRAY_SIZE(GatewayLinkTx);
    GatewayLinkTx[pos].service = service;
    GatewayLinkTx[pos].opcode = opcode;
    GatewayLinkTx[pos].channel = channel;
    GatewayLinkTx[pos].flags = flags;
    GatewayLinkTx[pos].length = length;
    GatewayLinkTx[pos].cookie = cookie;
    if (length)
        memcpy(GatewayLinkTx[pos].data, data, length);
    GatewayLinkTxPush = push + 1;
    irq_restore(irqflag);
    sched_wake_task(&GatewayWake);
    return 0;
}

static void
gateway_flush_link(void)
{
    if (!GatewayOps)
        return;
    if (!GatewayPendingDatagramLength) {
        uint32_t pull = GatewayLinkTxPull;
        uint32_t push = readl((void *)&GatewayLinkTxPush);
        uint32_t offset = HELIX_GATEWAY_HEADER_SIZE;
        uint16_t count = 0;
        uint8_t ack_only = 1;
        while (pull + count != push) {
            uint32_t pos = (pull + count) % ARRAY_SIZE(GatewayLinkTx);
            struct helix_gateway_record record = {
                .service = GatewayLinkTx[pos].service,
                .opcode = GatewayLinkTx[pos].opcode,
                .channel = GatewayLinkTx[pos].channel,
                .flags = GatewayLinkTx[pos].flags,
                .length = GatewayLinkTx[pos].length,
                .cookie = GatewayLinkTx[pos].cookie,
                .data = GatewayLinkTx[pos].data,
            };
            int used = helix_gateway_record_encode(
                GatewayWire + offset, sizeof(GatewayWire) - offset, &record);
            if (used < 0)
                break;
            if (record.service != HELIX_GATEWAY_SERVICE_CONTROL
                || record.opcode != HELIX_GATEWAY_CONTROL_ACK)
                ack_only = 0;
            offset += used;
            count++;
        }
        if (!count)
            return;
        struct helix_gateway_packet packet = {
            .flags = ack_only ? HELIX_GATEWAY_PACKET_ACK_ONLY : 0,
            .epoch = GatewayRuntime.have_owner ? GatewayRuntime.owner_epoch
                                               : GatewayEpoch,
            .sequence = GatewaySequence + 1, .record_count = count,
            .payload_length = offset - HELIX_GATEWAY_HEADER_SIZE,
        };
        if (helix_gateway_packet_encode(GatewayWire, sizeof(GatewayWire),
                                        &packet) < 0)
            return;
#if CONFIG_WANT_DATAGRAM_SESSION
        if (udpsess_established())
            GatewayPendingDatagramLength = udpsess_encode(
                GatewayTxDatagram, sizeof(GatewayTxDatagram),
                GatewayWire, offset);
        else
#endif
            GatewayPendingDatagramLength = udpdg_encode(
                GatewayTxDatagram, GatewayWire, offset);
        GatewayPendingRecordCount = count;
        if (!GatewayPendingDatagramLength)
            return;
    }
    int ret = 0;
    if (GatewayOps->send_checked)
        ret = GatewayOps->send_checked(GatewayOpsCtx, GatewayTxDatagram,
                                       GatewayPendingDatagramLength);
    else
        GatewayOps->send(GatewayOpsCtx, GatewayTxDatagram,
                         GatewayPendingDatagramLength);
    if (ret < 0)
        return;
    GatewayLinkTxPull += GatewayPendingRecordCount;
    GatewayPendingRecordCount = GatewayPendingDatagramLength = 0;
    GatewaySequence++;
    GatewayTxPackets++;
}

void
udp_gateway_sendf(const struct command_encoder *ce, va_list args)
{
    uint8_t frame[MESSAGE_MAX];
    uint_fast8_t length = command_encode_and_frame(frame, sizeof(frame),
                                                   ce, args);
    if (length)
        gateway_emit(HELIX_GATEWAY_SERVICE_CONTROL,
                     HELIX_GATEWAY_CONTROL_CONSOLE, 0, 0, 0, frame, length);
}

void *
udp_gateway_get_rx_buf(void)
{
    return GatewayConsoleRx;
}

int
udp_gateway_serial_rx(uint16_t channel, const uint8_t *data,
                      uint16_t length, uint32_t hw_clock)
{
    return gateway_emit(HELIX_GATEWAY_SERVICE_SERIAL,
                        HELIX_GATEWAY_SERIAL_DATA, channel,
                        hw_clock ? HELIX_GATEWAY_RECORD_TIMESTAMP_VALID : 0,
                        hw_clock, data, length);
}

void
udp_gateway_note_rx(void)
{
    sched_wake_task(&GatewayWake);
}

static void
gateway_dispatch_console(void)
{
    uint32_t length = GatewayConsoleRxPos;
    while (length) {
        uint_fast8_t pop_count;
        uint_fast8_t message_length = length > MESSAGE_MAX
                                      ? MESSAGE_MAX : length;
        int_fast8_t ret = command_find_and_dispatch(
            GatewayConsoleRx, message_length, &pop_count);
        if (!ret)
            break;
        length -= pop_count;
        if (length)
            memmove(GatewayConsoleRx, GatewayConsoleRx + pop_count, length);
    }
    GatewayConsoleRxPos = length;
}

static void
gateway_drain_can_tx(void)
{
    for (;;) {
        struct canbus_msg *message = can_gateway_queue_peek(&GatewayCanTx);
        if (!message || GatewaySubmittedCount >= GATEWAY_CAN_TX_DEPTH
            || !gateway_link_free())
            return;
        uint_fast16_t attempts;
        uint8_t tag = GatewayNextTag;
        for (attempts = 0; attempts < 256; attempts++, tag++) {
            uint_fast8_t i, used = 0;
            for (i = 0; i < GatewaySubmittedCount; i++)
                if (GatewaySubmitted[i].tag == tag) {
                    used = 1;
                    break;
                }
            if (!used)
                break;
        }
        if (attempts == 256)
            return;
        message->tx_tag = tag;
        if (canhw_send(message) < 0)
            return;
        GatewayNextTag = tag + 1;
        uint8_t index = GatewaySubmittedCount++;
        GatewaySubmitted[index].tag = tag;
        GatewaySubmitted[index].cookie = message->hw_clock;
        GatewayCanSubmitted++;
        gateway_emit_delivery(HELIX_GATEWAY_DELIVERY_SUBMITTED, 0,
                              message->hw_clock, 0, 0);
        can_gateway_queue_pop(&GatewayCanTx);
        helix_gateway_runtime_add_credits(&GatewayRuntime,
                                          HELIX_GATEWAY_SERVICE_CAN, 1);
    }
}

static void
gateway_drain_can_rx(void)
{
    uint8_t data[76];
    for (;;) {
        struct canbus_msg *message = can_gateway_queue_peek(&GatewayCanRx);
        if (!message)
            return;
        struct helix_gateway_can_frame frame = {
            .can_id = message->id, .hw_clock = message->hw_clock,
            .length = message->dlc, .flags = message->flags,
            .data = message->data,
        };
        int length = helix_gateway_can_encode(data, sizeof(data), &frame);
        if (length < 0 || gateway_emit(HELIX_GATEWAY_SERVICE_CAN,
                                      HELIX_GATEWAY_CAN_FRAME, 0,
                                      message->hw_clock
                                      ? HELIX_GATEWAY_RECORD_TIMESTAMP_VALID
                                      : 0, 0, data, length) < 0)
            return;
        can_gateway_queue_pop(&GatewayCanRx);
    }
}

static int
gateway_resolve_tx_event(uint8_t tag, uint32_t local_clock, uint8_t failed)
{
    uint_fast8_t i;
    for (i = 0; i < GatewaySubmittedCount; i++) {
        if (GatewaySubmitted[i].tag != tag)
            continue;
        uint32_t cookie = GatewaySubmitted[i].cookie;
        if (failed) {
            if (gateway_emit_delivery(HELIX_GATEWAY_DELIVERY_FAILED, 1,
                                      cookie, 0, 0) < 0)
                return -1;
            GatewayCanFailed++;
        } else {
            if (gateway_emit_delivery(HELIX_GATEWAY_DELIVERY_COMPLETED, 0,
                                      cookie, local_clock, 0) < 0)
                return -1;
            GatewayCanCompleted++;
        }
        GatewaySubmittedCount--;
        GatewaySubmitted[i] = GatewaySubmitted[GatewaySubmittedCount];
        return 0;
    }
    return 0;
}

static int
gateway_mark_submitted_unknown(uint8_t error, uint32_t detail)
{
    while (GatewaySubmittedCount) {
        uint32_t cookie = GatewaySubmitted[GatewaySubmittedCount - 1].cookie;
        if (gateway_emit_delivery(HELIX_GATEWAY_DELIVERY_UNKNOWN, error,
                                  cookie, 0, detail) < 0)
            return -1;
        GatewaySubmittedCount--;
        GatewayCanUnknown++;
    }
    return 0;
}

static void
gateway_drain_tx_events(void)
{
    uint32_t pull = GatewayTxEventPull;
    while (pull != readl((void *)&GatewayTxEventPush)) {
        uint32_t pos = pull % ARRAY_SIZE(GatewayTxEvents);
        if (gateway_resolve_tx_event(GatewayTxEvents[pos].tag,
                                     GatewayTxEvents[pos].local_clock,
                                     GatewayTxEvents[pos].failed) < 0)
            return;
        GatewayTxEventPull = pull = pull + 1;
    }
    if (GatewayTxEventLost) {
        if (gateway_mark_submitted_unknown(2, 0) < 0)
            return;
        GatewayTxEventLost = 0;
    }
    if (GatewayBusOffPending) {
        if (gateway_mark_submitted_unknown(1, CANBUS_STATE_OFF) < 0)
            return;
        GatewayBusOffPending = 0;
    }
}

void
udp_gateway_task(void)
{
    if (!sched_check_wake(&GatewayWake) || !GatewayOps)
        return;
    for (;;) {
        int32_t got = GatewayOps->recv(GatewayOpsCtx, GatewayRxDatagram,
                                       sizeof(GatewayRxDatagram));
        if (got <= 0)
            break;
        const uint8_t *payload;
        int32_t length;
#if CONFIG_WANT_DATAGRAM_SESSION
        int kind = udpsess_msg_type(GatewayRxDatagram, got);
        if (kind && !udpsess_established()
            && udpdg_is_authenticated_static(GatewayRxDatagram, got))
            kind = 0;
        if (kind == 1 || kind == 3) {
            uint32_t now = timer_read_time();
            if (GatewayHandshakeDeadline
                && !timer_is_before(now, GatewayHandshakeDeadline)) {
                udpsess_reset_handshake();
                GatewayHandshakeDeadline = 0;
            }
            uint32_t reply = udpsess_on_handshake(
                GatewayRxDatagram, got, GatewayTxDatagram,
                sizeof(GatewayTxDatagram));
            if (udpsess_take_peer_adopted()) {
                GatewayHandshakeDeadline = 0;
                if (GatewayOps->rx_accepted)
                    GatewayOps->rx_accepted(GatewayOpsCtx);
            } else if (reply && !GatewayHandshakeDeadline) {
                GatewayHandshakeDeadline = now + timer_from_us(
                    GATEWAY_SESSION_HANDSHAKE_TIMEOUT_US);
            }
            if (reply && GatewayOps->send_candidate)
                GatewayOps->send_candidate(GatewayOpsCtx,
                                           GatewayTxDatagram, reply);
            continue;
        }
        if (kind == 2 && udpsess_established())
            length = udpsess_decode(GatewayRxDatagram, got, &payload);
        else if (udpsess_established())
            continue;
        else
#endif
            length = udpdg_decode(GatewayRxDatagram, got, &payload);
        if (length <= 0)
            continue;
        // Authentication has succeeded. Latch this peer before dispatch so
        // an admission/config reply to the first valid packet has a target.
        if (GatewayOps->rx_accepted)
            GatewayOps->rx_accepted(GatewayOpsCtx);
        struct helix_gateway_packet packet;
        if (helix_gateway_packet_decode(&packet, payload, length) < 0)
            continue;
        int dispatched = helix_gateway_runtime_dispatch(
            &GatewayRuntime, payload, length);
        if (dispatched >= 0
            && !(packet.flags & HELIX_GATEWAY_PACKET_ACK_ONLY))
            gateway_emit_ack();
    }
    gateway_dispatch_console();
    gateway_drain_tx_events();
    gateway_drain_can_tx();
    gateway_drain_can_rx();
    gateway_flush_link();
}
DECL_TASK(udp_gateway_task);

void
canbus_notify_tx(void)
{
    sched_wake_task(&GatewayWake);
}

void
canbus_notify_protocol_error(void)
{
}

void
canbus_notify_tx_timestamp(uint8_t tag, uint32_t local_clock)
{
    uint32_t push = GatewayTxEventPush;
    if (push - GatewayTxEventPull >= ARRAY_SIZE(GatewayTxEvents))
        GatewayTxEventLost = 1;
    else {
        uint32_t pos = push % ARRAY_SIZE(GatewayTxEvents);
        GatewayTxEvents[pos].tag = tag;
        GatewayTxEvents[pos].failed = 0;
        GatewayTxEvents[pos].local_clock = local_clock;
        GatewayTxEventPush = push + 1;
    }
    sched_wake_task(&GatewayWake);
}

void
canbus_notify_tx_failed(uint8_t tag)
{
    uint32_t push = GatewayTxEventPush;
    if (push - GatewayTxEventPull >= ARRAY_SIZE(GatewayTxEvents))
        GatewayTxEventLost = 1;
    else {
        uint32_t pos = push % ARRAY_SIZE(GatewayTxEvents);
        GatewayTxEvents[pos].tag = tag;
        GatewayTxEvents[pos].failed = 1;
        GatewayTxEvents[pos].local_clock = 0;
        GatewayTxEventPush = push + 1;
    }
    sched_wake_task(&GatewayWake);
}

void
canbus_notify_tx_event_lost(void)
{
    GatewayTxEventLost = 1;
    sched_wake_task(&GatewayWake);
}

void
canbus_notify_bus_off(void)
{
    GatewayCanBusOff++;
    GatewayBusOffPending = 1;
    sched_wake_task(&GatewayWake);
}

void
canbus_process_data(struct canbus_msg *message)
{
    can_gateway_queue_push(&GatewayCanRx, message);
    sched_wake_task(&GatewayWake);
}

void
command_get_gateway_status(uint32_t *args)
{
    (void)args;
    struct udpdg_stats datagram;
    udpdg_get_stats(&datagram);
    sendf("gateway_status epoch=%u tx_packets=%u tx_drops=%u malformed=%u"
          " stale=%u duplicates=%u credit_stalls=%u can_rx=%u can_forwarded=%u"
          " can_drops=%u can_depth=%u can_bus_off=%u admitted=%u"
          " submitted=%u completed=%u failed=%u unknown=%u"
          " submitted_now=%c can_tx_depth=%hu link_tx_depth=%hu"
          " link_pending=%c fd=%c brs=%c rx_lost=%u rx_reordered=%u"
          " auth_failures=%u",
          GatewayEpoch, GatewayTxPackets, GatewayTxDrops,
          GatewayRuntime.stats.malformed, GatewayRuntime.stats.stale_epochs,
          GatewayRuntime.stats.duplicates,
          GatewayRuntime.stats.credit_stalls, GatewayCanRx.received,
          GatewayCanRx.forwarded, GatewayCanRx.drops,
          can_gateway_queue_depth(&GatewayCanRx), GatewayCanBusOff,
          GatewayCanAdmitted, GatewayCanSubmitted, GatewayCanCompleted,
          GatewayCanFailed, GatewayCanUnknown, GatewaySubmittedCount,
          can_gateway_queue_depth(&GatewayCanTx),
          GatewayLinkTxPush - GatewayLinkTxPull,
          !!GatewayPendingDatagramLength, GatewayFdActive, GatewayBrsActive,
          datagram.rx_lost, datagram.rx_reordered,
          datagram.rx_auth_failures);
}
DECL_COMMAND_FLAGS(command_get_gateway_status, HF_IN_SHUTDOWN,
                   "get_gateway_status");

void
udp_gateway_init(const struct udp_console_ops *ops, void *ctx,
                 const uint8_t *psk, uint32_t psk_len)
{
    can_gateway_queue_init(&GatewayCanRx, GatewayCanRxStorage,
                           GATEWAY_CAN_RX_DEPTH);
    can_gateway_queue_init(&GatewayCanTx, GatewayCanTxStorage,
                           GATEWAY_CAN_TX_DEPTH);
    helix_gateway_runtime_init(&GatewayRuntime);
    helix_gateway_runtime_register(&GatewayRuntime,
        HELIX_GATEWAY_SERVICE_CONTROL, &GatewayControlOps, 0, 32);
    helix_gateway_runtime_register(&GatewayRuntime,
        HELIX_GATEWAY_SERVICE_CAN, &GatewayCanOps, 0, GATEWAY_CAN_TX_DEPTH);
    helix_gateway_runtime_register(&GatewayRuntime,
        HELIX_GATEWAY_SERVICE_SERIAL, &GatewaySerialOps, 0, 32);
    GatewayEpoch = timer_read_time() ^ 0x48475831u;
    if (!GatewayEpoch)
        GatewayEpoch = 1;
    udpdg_init(psk, psk_len, 0);
#if CONFIG_WANT_DATAGRAM_SESSION
    if (psk && psk_len) {
        static const char board_id[] = CONFIG_DATAGRAM_SESSION_ID;
        uint8_t nonce[16];
        uint_fast8_t i;
        for (i = 0; i < sizeof(nonce); i++) {
            uint32_t now = timer_read_time();
            nonce[i] = now ^ (now >> 8) ^ (now >> 16) ^ (now >> 24);
            for (volatile uint_fast8_t spin = 0;
                 spin < (uint_fast8_t)(now & 7) + 1; spin++)
                ;
        }
        udpsess_init(psk, psk_len, (const uint8_t *)board_id,
                     sizeof(board_id) - 1, nonce);
    }
#endif
    GatewayOps = ops;
    GatewayOpsCtx = ctx;
}
