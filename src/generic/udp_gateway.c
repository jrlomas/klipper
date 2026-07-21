// Authenticated UDP binding for the typed Helix gateway protocol.

#include <string.h>
#include "autoconf.h"
#include "board/canbus.h"
#include "board/irq.h"
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
static uint8_t GatewayConsoleRx[2 * UDPDG_FRAMES_MAX];
static uint32_t GatewayConsoleRxPos;
static uint32_t GatewayEpoch, GatewaySequence;
static uint32_t GatewayTxPackets, GatewayTxDrops, GatewayCanBusOff;

static int gateway_emit(uint8_t service, uint8_t opcode, uint16_t channel,
                        uint16_t flags, uint32_t cookie,
                        const uint8_t *data, uint16_t length);

static int
gateway_control_submit(void *ctx, const struct helix_gateway_record *record)
{
    (void)ctx;
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
    if (record->opcode != HELIX_GATEWAY_CAN_FRAME)
        return -1;
    struct helix_gateway_can_frame wire;
    if (helix_gateway_can_decode(&wire, record->data, record->length) < 0)
        return -1;
#if !CONFIG_CANBUS_FD
    if ((wire.flags & CANMSG_FLAG_FD) || wire.length > 8)
        return -1;
#endif
    struct canbus_msg message = {};
    message.id = wire.can_id;
    message.dlc = wire.length;
    message.flags = wire.flags;
    message.hw_clock = wire.hw_clock;
    memcpy(message.data, wire.data, wire.length);
    return can_gateway_queue_push(&GatewayCanTx, &message);
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

static const struct helix_gateway_service_ops GatewayControlOps = {
    .submit = gateway_control_submit,
};
static const struct helix_gateway_service_ops GatewayCanOps = {
    .submit = gateway_can_submit,
};
static const struct helix_gateway_service_ops GatewaySerialOps = {
    .submit = gateway_serial_submit,
};

static int
gateway_emit(uint8_t service, uint8_t opcode, uint16_t channel,
             uint16_t flags, uint32_t cookie,
             const uint8_t *data, uint16_t length)
{
    struct helix_gateway_record record = {
        .service = service, .opcode = opcode, .channel = channel,
        .flags = flags, .length = length, .cookie = cookie, .data = data,
    };
    int record_length = helix_gateway_record_encode(
        GatewayWire + HELIX_GATEWAY_HEADER_SIZE,
        sizeof(GatewayWire) - HELIX_GATEWAY_HEADER_SIZE, &record);
    if (record_length < 0)
        goto drop;
    struct helix_gateway_packet packet = {
        .epoch = GatewayRuntime.have_owner ? GatewayRuntime.owner_epoch
                                           : GatewayEpoch,
        .sequence = ++GatewaySequence,
        .record_count = 1, .payload_length = record_length,
    };
    if (helix_gateway_packet_encode(GatewayWire, sizeof(GatewayWire),
                                    &packet) < 0)
        goto drop;
    uint32_t payload_length = HELIX_GATEWAY_HEADER_SIZE + record_length;
    uint32_t datagram_length = udpdg_encode(GatewayTxDatagram, GatewayWire,
                                            payload_length);
    if (!datagram_length || !GatewayOps)
        goto drop;
    if (GatewayOps->send_checked) {
        if (GatewayOps->send_checked(GatewayOpsCtx, GatewayTxDatagram,
                                     datagram_length) < 0)
            goto drop;
    } else {
        GatewayOps->send(GatewayOpsCtx, GatewayTxDatagram, datagram_length);
    }
    GatewayTxPackets++;
    return 0;
drop:
    GatewayTxDrops++;
    return -1;
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
        if (!message || canhw_send(message) < 0)
            return;
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
        int32_t length = udpdg_decode(GatewayRxDatagram, got, &payload);
        if (length <= 0)
            continue;
        if (helix_gateway_runtime_dispatch(&GatewayRuntime, payload,
                                           length) >= 0
            && GatewayOps->rx_accepted)
            GatewayOps->rx_accepted(GatewayOpsCtx);
    }
    gateway_dispatch_console();
    gateway_drain_can_tx();
    gateway_drain_can_rx();
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
    (void)tag; (void)local_clock;
}

void
canbus_notify_bus_off(void)
{
    GatewayCanBusOff++;
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
    sendf("gateway_status epoch=%u tx_packets=%u tx_drops=%u malformed=%u"
          " stale=%u credit_stalls=%u can_rx=%u can_forwarded=%u"
          " can_drops=%u can_depth=%u can_bus_off=%u",
          GatewayEpoch, GatewayTxPackets, GatewayTxDrops,
          GatewayRuntime.stats.malformed, GatewayRuntime.stats.stale_epochs,
          GatewayRuntime.stats.credit_stalls, GatewayCanRx.received,
          GatewayCanRx.forwarded, GatewayCanRx.drops,
          can_gateway_queue_depth(&GatewayCanRx), GatewayCanBusOff);
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
    GatewayOps = ops;
    GatewayOpsCtx = ctx;
}
