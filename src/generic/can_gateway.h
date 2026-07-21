#ifndef __GENERIC_CAN_GATEWAY_H
#define __GENERIC_CAN_GATEWAY_H

#include <stdint.h>
#include "canbus.h"

// Single-producer (CAN IRQ), single-consumer (gateway task) bounded queue.
// Storage is supplied by the transport so small MCUs are not forced to carry
// the large burst buffer used by a high-rate USB/Ethernet bridge.
struct can_gateway_queue {
    struct canbus_msg *storage;
    uint32_t capacity;
    volatile uint32_t pull_pos, push_pos;
    volatile uint32_t received, forwarded, drops;
    volatile uint16_t highwater;
};

void can_gateway_queue_init(struct can_gateway_queue *queue,
                            struct canbus_msg *storage, uint32_t capacity);
int can_gateway_queue_push(struct can_gateway_queue *queue,
                           const struct canbus_msg *message);
struct canbus_msg *can_gateway_queue_peek(struct can_gateway_queue *queue);
void can_gateway_queue_pop(struct can_gateway_queue *queue);
uint32_t can_gateway_queue_depth(const struct can_gateway_queue *queue);

#endif
