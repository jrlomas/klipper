// Transport-neutral bounded CAN gateway queue.

#include <string.h>
#include "board/io.h"
#include "can_gateway.h"

void
can_gateway_queue_init(struct can_gateway_queue *queue,
                       struct canbus_msg *storage, uint32_t capacity)
{
    memset(queue, 0, sizeof(*queue));
    queue->storage = storage;
    queue->capacity = capacity;
}

uint32_t
can_gateway_queue_depth(const struct can_gateway_queue *queue)
{
    return readl((void *)&queue->push_pos) - queue->pull_pos;
}

int
can_gateway_queue_push(struct can_gateway_queue *queue,
                       const struct canbus_msg *message)
{
    queue->received++;
    uint32_t push = queue->push_pos;
    uint32_t depth = push - queue->pull_pos;
    if (depth >= queue->capacity) {
        queue->drops++;
        return -1;
    }
    if (depth + 1 > queue->highwater)
        queue->highwater = depth + 1;
    memcpy(&queue->storage[push % queue->capacity], message, sizeof(*message));
    queue->push_pos = push + 1;
    return 0;
}

struct canbus_msg *
can_gateway_queue_peek(struct can_gateway_queue *queue)
{
    if (queue->pull_pos == readl((void *)&queue->push_pos))
        return 0;
    return &queue->storage[queue->pull_pos % queue->capacity];
}

void
can_gateway_queue_pop(struct can_gateway_queue *queue)
{
    queue->pull_pos++;
    queue->forwarded++;
}
