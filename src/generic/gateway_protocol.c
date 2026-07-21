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

int
helix_gateway_packet_encode(uint8_t *out, uint32_t cap,
                            const struct helix_gateway_packet *packet)
{
    if (!out || !packet || cap < HELIX_GATEWAY_HEADER_SIZE)
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
        || data[2] != HELIX_GATEWAY_VERSION)
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
        || data[0] >= HELIX_GATEWAY_MAX_SERVICES)
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
    if (!out || !frame || frame->length > 64
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
    if (!frame || !data || length < 12 || data[8] > 64
        || length != 12u + data[8] || data[10] || data[11])
        return -1;
    frame->can_id = rd32(data);
    frame->hw_clock = rd32(data + 4);
    frame->length = data[8];
    frame->flags = data[9];
    frame->data = data + 12;
    return length;
}
