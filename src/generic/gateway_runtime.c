// Fixed-storage service dispatcher shared by USB and Ethernet gateways.

#include <string.h>
#include "gateway_runtime.h"

void
helix_gateway_runtime_init(struct helix_gateway_runtime *runtime)
{
    memset(runtime, 0, sizeof(*runtime));
}

int
helix_gateway_runtime_register(struct helix_gateway_runtime *runtime,
                               uint8_t service,
                               const struct helix_gateway_service_ops *ops,
                               void *ctx, uint16_t initial_credits)
{
    if (!runtime || !ops || !ops->submit
        || service >= HELIX_GATEWAY_MAX_SERVICES
        || runtime->services[service].ops)
        return -1;
    struct helix_gateway_service_slot *slot = &runtime->services[service];
    slot->ops = ops;
    slot->ctx = ctx;
    slot->rx_credits = slot->rx_credit_limit = initial_credits;
    return 0;
}

void
helix_gateway_runtime_set_owner(struct helix_gateway_runtime *runtime,
                                uint32_t epoch)
{
    runtime->owner_epoch = epoch;
    runtime->last_sequence = 0;
    runtime->have_sequence = 0;
    runtime->have_owner = 1;
    runtime->stats.takeovers++;
    uint_fast8_t i;
    for (i = 0; i < HELIX_GATEWAY_MAX_SERVICES; i++) {
        struct helix_gateway_service_slot *slot = &runtime->services[i];
        slot->rx_credits = slot->rx_credit_limit;
        if (slot->ops && slot->ops->reset)
            slot->ops->reset(slot->ctx, epoch);
    }
}

void
helix_gateway_runtime_add_credits(struct helix_gateway_runtime *runtime,
                                  uint8_t service, uint16_t credits)
{
    if (service >= HELIX_GATEWAY_MAX_SERVICES)
        return;
    struct helix_gateway_service_slot *slot = &runtime->services[service];
    uint32_t value = slot->rx_credits + credits;
    slot->rx_credits = value > slot->rx_credit_limit
                       ? slot->rx_credit_limit : value;
}

int
helix_gateway_runtime_dispatch(struct helix_gateway_runtime *runtime,
                               const uint8_t *data, uint32_t length)
{
    struct helix_gateway_packet packet;
    int offset = helix_gateway_packet_decode(&packet, data, length);
    if (offset < 0) {
        runtime->stats.malformed++;
        return -1;
    }
    if (!runtime->have_owner
        || ((packet.flags & HELIX_GATEWAY_PACKET_RESET)
            && packet.epoch != runtime->owner_epoch))
        helix_gateway_runtime_set_owner(runtime, packet.epoch);
    uint32_t delta = packet.sequence - runtime->last_sequence;
    if (packet.epoch != runtime->owner_epoch
        || (runtime->have_sequence
            && (!delta || delta > 0x7fffffffu))) {
        runtime->stats.stale_epochs++;
        return -1;
    }
    // Validate the complete envelope and reserve its credits before invoking
    // any service. A malformed trailing record must not partially actuate an
    // earlier CAN or serial record from the same authenticated packet.
    uint16_t needed[HELIX_GATEWAY_MAX_SERVICES] = {};
    uint_fast16_t count;
    int scan = offset;
    for (count = 0; count < packet.record_count; count++) {
        struct helix_gateway_record record;
        int used = helix_gateway_record_decode(&record, data + scan,
                                                length - scan);
        if (used < 0) {
            runtime->stats.malformed++;
            return -1;
        }
        struct helix_gateway_service_slot *slot =
            &runtime->services[record.service];
        if (!slot->ops) {
            runtime->stats.unknown_services++;
            return -1;
        }
        if (++needed[record.service] > slot->rx_credits) {
            runtime->stats.credit_stalls++;
            return -2;
        }
        scan += used;
    }
    if ((uint32_t)scan != length) {
        runtime->stats.malformed++;
        return -1;
    }
    runtime->last_sequence = packet.sequence;
    runtime->have_sequence = 1;
    runtime->stats.packets++;
    for (count = 0; count < packet.record_count; count++) {
        struct helix_gateway_record record;
        int used = helix_gateway_record_decode(&record, data + offset,
                                                length - offset);
        struct helix_gateway_service_slot *slot =
            &runtime->services[record.service];
        slot->rx_credits--;
        int ret = slot->ops->submit(slot->ctx, &record);
        if (ret < 0) {
            slot->rx_credits++;
            runtime->stats.service_errors++;
            return ret;
        }
        runtime->stats.records++;
        offset += used;
    }
    return count;
}
