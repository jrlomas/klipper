#ifndef __GENERIC_GATEWAY_RUNTIME_H
#define __GENERIC_GATEWAY_RUNTIME_H

#include <stdint.h>
#include "gateway_protocol.h"

struct helix_gateway_runtime;

struct helix_gateway_service_ops {
    int (*submit)(void *ctx, const struct helix_gateway_record *record);
    void (*reset)(void *ctx, uint32_t epoch);
};

struct helix_gateway_service_slot {
    const struct helix_gateway_service_ops *ops;
    void *ctx;
    uint16_t rx_credits;
    uint16_t rx_credit_limit;
};

struct helix_gateway_runtime_stats {
    uint32_t packets;
    uint32_t records;
    uint32_t stale_epochs;
    uint32_t malformed;
    uint32_t unknown_services;
    uint32_t credit_stalls;
    uint32_t service_errors;
    uint32_t takeovers;
};

struct helix_gateway_runtime {
    uint32_t owner_epoch;
    uint32_t last_sequence;
    uint8_t have_owner, have_sequence;
    struct helix_gateway_service_slot services[HELIX_GATEWAY_MAX_SERVICES];
    struct helix_gateway_runtime_stats stats;
};

void helix_gateway_runtime_init(struct helix_gateway_runtime *runtime);
int helix_gateway_runtime_register(
    struct helix_gateway_runtime *runtime, uint8_t service,
    const struct helix_gateway_service_ops *ops, void *ctx,
    uint16_t initial_credits);
void helix_gateway_runtime_set_owner(struct helix_gateway_runtime *runtime,
                                     uint32_t epoch);
void helix_gateway_runtime_add_credits(struct helix_gateway_runtime *runtime,
                                       uint8_t service, uint16_t credits);
int helix_gateway_runtime_dispatch(struct helix_gateway_runtime *runtime,
                                   const uint8_t *packet, uint32_t length);

#endif
