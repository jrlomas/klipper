// Typed payload codec for Helix transport gateways.

#include <string.h>
#include "gateway_protocol.h"

static uint16_t
rd16(const uint8_t *p)
{
    return p[0] | (uint16_t)p[1] << 8;
}

static uint32_t
rd32(const uint8_t *p)
{
    return p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16
           | (uint32_t)p[3] << 24;
}

static void
wr16(uint8_t *p, uint16_t v)
{
    p[0] = v;
    p[1] = v >> 8;
}

static void
wr32(uint8_t *p, uint32_t v)
{
    p[0] = v;
    p[1] = v >> 8;
    p[2] = v >> 16;
    p[3] = v >> 24;
}

static uint64_t
rd64(const uint8_t *p)
{
    return rd32(p) | (uint64_t)rd32(p + 4) << 32;
}

static void
wr64(uint8_t *p, uint64_t v)
{
    wr32(p, v);
    wr32(p + 4, v >> 32);
}

int
helix_gateway_packet_encode(uint8_t *out, uint32_t cap,
                            const struct helix_gateway_packet *packet)
{
    if (!out || !packet || cap < HELIX_GATEWAY_HEADER_SIZE
        || packet->flags & ~(HELIX_GATEWAY_PACKET_RESET
                             | HELIX_GATEWAY_PACKET_ACK_ONLY))
        return -1;
    wr16(out, HELIX_GATEWAY_MAGIC);
    out[2] = HELIX_GATEWAY_VERSION;
    out[3] = packet->flags;
    wr32(out + 4, packet->epoch);
    wr32(out + 8, packet->sequence);
    wr16(out + 12, packet->record_count);
    wr16(out + 14, packet->payload_length);
    return HELIX_GATEWAY_HEADER_SIZE;
}

int
helix_gateway_packet_decode(struct helix_gateway_packet *packet,
                            const uint8_t *data, uint32_t length)
{
    if (!packet || !data || length < HELIX_GATEWAY_HEADER_SIZE
        || rd16(data) != HELIX_GATEWAY_MAGIC
        || data[2] != HELIX_GATEWAY_VERSION
        || data[3] & ~(HELIX_GATEWAY_PACKET_RESET
                       | HELIX_GATEWAY_PACKET_ACK_ONLY))
        return -1;
    uint16_t payload_length = rd16(data + 14);
    if (payload_length != length - HELIX_GATEWAY_HEADER_SIZE)
        return -1;
    packet->flags = data[3];
    packet->epoch = rd32(data + 4);
    packet->sequence = rd32(data + 8);
    packet->record_count = rd16(data + 12);
    packet->payload_length = payload_length;
    return HELIX_GATEWAY_HEADER_SIZE;
}

int
helix_gateway_record_encode(uint8_t *out, uint32_t cap,
                            const struct helix_gateway_record *record)
{
    if (!out || !record || record->service >= HELIX_GATEWAY_MAX_SERVICES
        || record->length > HELIX_GATEWAY_MAX_RECORD_DATA
        || record->flags & ~(HELIX_GATEWAY_RECORD_REPLY
                             | HELIX_GATEWAY_RECORD_ERROR
                             | HELIX_GATEWAY_RECORD_MORE
                             | HELIX_GATEWAY_RECORD_TIMESTAMP_VALID)
        || cap < (uint32_t)HELIX_GATEWAY_RECORD_HEADER_SIZE + record->length
        || (record->length && !record->data))
        return -1;
    out[0] = record->service;
    out[1] = record->opcode;
    wr16(out + 2, record->channel);
    wr16(out + 4, record->flags);
    wr16(out + 6, record->length);
    wr32(out + 8, record->cookie);
    if (record->length)
        memcpy(out + HELIX_GATEWAY_RECORD_HEADER_SIZE, record->data,
               record->length);
    return HELIX_GATEWAY_RECORD_HEADER_SIZE + record->length;
}

int
helix_gateway_record_decode(struct helix_gateway_record *record,
                            const uint8_t *data, uint32_t length)
{
    if (!record || !data || length < HELIX_GATEWAY_RECORD_HEADER_SIZE
        || data[0] >= HELIX_GATEWAY_MAX_SERVICES
        || rd16(data + 4) & ~(HELIX_GATEWAY_RECORD_REPLY
                              | HELIX_GATEWAY_RECORD_ERROR
                              | HELIX_GATEWAY_RECORD_MORE
                              | HELIX_GATEWAY_RECORD_TIMESTAMP_VALID))
        return -1;
    uint16_t record_length = rd16(data + 6);
    if (record_length > HELIX_GATEWAY_MAX_RECORD_DATA
        || record_length > length - HELIX_GATEWAY_RECORD_HEADER_SIZE)
        return -1;
    record->service = data[0];
    record->opcode = data[1];
    record->channel = rd16(data + 2);
    record->flags = rd16(data + 4);
    record->length = record_length;
    record->cookie = rd32(data + 8);
    record->data = data + HELIX_GATEWAY_RECORD_HEADER_SIZE;
    return HELIX_GATEWAY_RECORD_HEADER_SIZE + record_length;
}

int
helix_gateway_can_encode(uint8_t *out, uint32_t cap,
                         const struct helix_gateway_can_frame *frame)
{
    uint32_t can_id = frame ? frame->can_id : 0;
    uint8_t flags = frame ? frame->flags : 0;
    if (!out || !frame || frame->length > 64
        || flags & ~0x17u || ((flags & 0x06u) && !(flags & 0x01u))
        || (!(flags & 0x01u) && frame->length > 8)
        || (can_id & 0x20000000u)
        || (!(can_id & 0x80000000u)
            && (can_id & 0x1fffffffu) > 0x7ffu)
        || ((can_id & 0x40000000u) && (flags & 0x01u))
        || cap < (uint32_t)12 + frame->length
        || (frame->length && !frame->data))
        return -1;
    wr32(out, frame->can_id);
    wr32(out + 4, frame->hw_clock);
    out[8] = frame->length;
    out[9] = frame->flags;
    out[10] = out[11] = 0;
    if (frame->length)
        memcpy(out + 12, frame->data, frame->length);
    return 12 + frame->length;
}

int
helix_gateway_can_decode(struct helix_gateway_can_frame *frame,
                         const uint8_t *data, uint32_t length)
{
    if (!frame || !data || length < 12)
        return -1;
    uint32_t can_id = rd32(data);
    uint8_t flags = data[9];
    if (data[8] > 64
        || length != 12u + data[8] || data[10] || data[11]
        || flags & ~0x17u
        || ((flags & 0x06u) && !(flags & 0x01u))
        || (!(flags & 0x01u) && data[8] > 8)
        || (can_id & 0x20000000u)
        || (!(can_id & 0x80000000u)
            && (can_id & 0x1fffffffu) > 0x7ffu)
        || ((can_id & 0x40000000u) && (flags & 0x01u)))
        return -1;
    frame->can_id = can_id;
    frame->hw_clock = rd32(data + 4);
    frame->length = data[8];
    frame->flags = flags;
    frame->data = data + 12;
    return length;
}

int
helix_gateway_can_config_encode(uint8_t *out, uint32_t cap,
                                const struct helix_gateway_can_config *config)
{
    if (!out || !config || cap < 16
        || config->action > HELIX_GATEWAY_CAN_CONFIG_ABORT
        || (config->brs && !config->fd))
        return -1;
    out[0] = config->action;
    out[1] = (!!config->fd) | ((!!config->brs) << 1);
    out[2] = out[3] = 0;
    wr32(out + 4, config->epoch);
    wr32(out + 8, config->nominal_bitrate);
    wr32(out + 12, config->data_bitrate);
    return 16;
}

int
helix_gateway_can_config_decode(struct helix_gateway_can_config *config,
                                const uint8_t *data, uint32_t length)
{
    if (!config || !data || length != 16 || data[0] > 3 || data[1] > 3
        || data[2] || data[3] || ((data[1] & 2) && !(data[1] & 1)))
        return -1;
    config->action = data[0];
    config->fd = data[1] & 1;
    config->brs = !!(data[1] & 2);
    config->epoch = rd32(data + 4);
    config->nominal_bitrate = rd32(data + 8);
    config->data_bitrate = rd32(data + 12);
    return 16;
}

int
helix_gateway_delivery_encode(uint8_t *out, uint32_t cap,
                              const struct helix_gateway_delivery *delivery)
{
    if (!out || !delivery || cap < 16 || !delivery->state
        || delivery->state > HELIX_GATEWAY_DELIVERY_UNKNOWN)
        return -1;
    out[0] = delivery->state;
    out[1] = delivery->error;
    out[2] = out[3] = 0;
    wr32(out + 4, delivery->cookie);
    wr32(out + 8, delivery->hw_clock);
    wr32(out + 12, delivery->detail);
    return 16;
}

int
helix_gateway_delivery_decode(struct helix_gateway_delivery *delivery,
                              const uint8_t *data, uint32_t length)
{
    if (!delivery || !data || length != 16 || !data[0] || data[0] > 5
        || data[2] || data[3])
        return -1;
    delivery->state = data[0];
    delivery->error = data[1];
    delivery->cookie = rd32(data + 4);
    delivery->hw_clock = rd32(data + 8);
    delivery->detail = rd32(data + 12);
    return 16;
}

int
helix_gateway_ack_encode(uint8_t *out, uint32_t cap,
                         const struct helix_gateway_ack *ack)
{
    if (!out || !ack || cap < 12 || !(ack->mask & 1))
        return -1;
    wr32(out, ack->epoch);
    wr32(out + 4, ack->sequence);
    wr32(out + 8, ack->mask);
    return 12;
}

int
helix_gateway_ack_decode(struct helix_gateway_ack *ack,
                         const uint8_t *data, uint32_t length)
{
    if (!ack || !data || length != 12 || !(rd32(data + 8) & 1))
        return -1;
    ack->epoch = rd32(data);
    ack->sequence = rd32(data + 4);
    ack->mask = rd32(data + 8);
    return 12;
}

int
helix_gateway_time_encode(
    uint8_t *out, uint32_t cap,
    const struct helix_gateway_time_exchange *exchange)
{
    if (!out || !exchange || cap < 32
        || exchange->action > HELIX_GATEWAY_TIME_RESPONSE
        || !exchange->epoch || !exchange->t1
        || (exchange->action == HELIX_GATEWAY_TIME_REQUEST
            && (exchange->t2 || exchange->t3))
        || (exchange->action == HELIX_GATEWAY_TIME_RESPONSE
            && (!exchange->t2 || exchange->t3 < exchange->t2)))
        return -1;
    out[0] = exchange->action;
    out[1] = exchange->quality;
    out[2] = out[3] = 0;
    wr32(out + 4, exchange->epoch);
    wr64(out + 8, exchange->t1);
    wr64(out + 16, exchange->t2);
    wr64(out + 24, exchange->t3);
    return 32;
}

int
helix_gateway_time_decode(
    struct helix_gateway_time_exchange *exchange,
    const uint8_t *data, uint32_t length)
{
    if (!exchange || !data || length != 32 || data[0] > 1
        || data[2] || data[3])
        return -1;
    exchange->action = data[0];
    exchange->quality = data[1];
    exchange->epoch = rd32(data + 4);
    exchange->t1 = rd64(data + 8);
    exchange->t2 = rd64(data + 16);
    exchange->t3 = rd64(data + 24);
    uint8_t check[32];
    return helix_gateway_time_encode(check, sizeof(check), exchange) < 0
           ? -1 : 32;
}
