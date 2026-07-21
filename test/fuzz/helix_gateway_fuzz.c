// libFuzzer target for the authenticated gateway payload boundary.

#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "generic/gateway_protocol.h"
#include "generic/gateway_runtime.h"

static int
consume(void *ctx, const struct helix_gateway_record *record)
{
    uint32_t *sum = ctx;
    *sum += record->service + record->opcode + record->length;
    return 0;
}

int
LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    struct helix_gateway_packet packet;
    if (size > 4096 || helix_gateway_packet_decode(
            &packet, data, size) < 0)
        return 0;
    uint32_t sum = 0;
    static const struct helix_gateway_service_ops ops = {consume, 0};
    struct helix_gateway_runtime runtime;
    helix_gateway_runtime_init(&runtime);
    for (uint_fast8_t service = 0; service < HELIX_GATEWAY_MAX_SERVICES;
         service++)
        helix_gateway_runtime_register(&runtime, service, &ops, &sum, 1024);
    helix_gateway_runtime_dispatch(&runtime, data, size);

    uint32_t offset = HELIX_GATEWAY_HEADER_SIZE;
    for (uint_fast16_t i = 0; i < packet.record_count && offset < size; i++) {
        struct helix_gateway_record record;
        int used = helix_gateway_record_decode(&record, data + offset,
                                                size - offset);
        if (used < 0)
            break;
        if (record.service == HELIX_GATEWAY_SERVICE_CAN
            && record.opcode == HELIX_GATEWAY_CAN_FRAME) {
            struct helix_gateway_can_frame frame;
            helix_gateway_can_decode(&frame, record.data, record.length);
        } else if (record.service == HELIX_GATEWAY_SERVICE_CAN
                   && record.opcode == HELIX_GATEWAY_CAN_CONFIG) {
            struct helix_gateway_can_config config;
            helix_gateway_can_config_decode(&config, record.data,
                                             record.length);
        } else if (record.service == HELIX_GATEWAY_SERVICE_CONTROL
                   && record.opcode == HELIX_GATEWAY_CONTROL_ACK) {
            struct helix_gateway_ack ack;
            helix_gateway_ack_decode(&ack, record.data, record.length);
        } else if (record.service == HELIX_GATEWAY_SERVICE_CONTROL
                   && record.opcode == HELIX_GATEWAY_CONTROL_TIME_SYNC) {
            struct helix_gateway_time_exchange exchange;
            helix_gateway_time_decode(&exchange, record.data, record.length);
        }
        offset += used;
    }
    (void)sum;
    return 0;
}

#ifdef HELIX_FUZZ_STANDALONE
int
main(void)
{
    uint8_t seed[256] = {
        0x48, 0x47, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
    };
    uint32_t random = 0x48454c49u;
    for (uint32_t run = 0; run < 200000; run++) {
        uint8_t input[sizeof(seed)];
        memcpy(input, seed, sizeof(input));
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        size_t size = random % sizeof(input);
        uint_fast8_t mutations = 1 + (random >> 24) % 8;
        for (uint_fast8_t i = 0; i < mutations && size; i++) {
            random = random * 1664525u + 1013904223u;
            input[random % size] ^= 1u << ((random >> 16) & 7);
        }
        LLVMFuzzerTestOneInput(input, size);
    }
    return 0;
}
#endif
