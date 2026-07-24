#ifndef MSGBLOCK_H
#define MSGBLOCK_H

#include <stdint.h> // uint8_t
#include "list.h" // struct list_node

#define MESSAGE_MIN 5
#define MESSAGE_MAX 64
#define MESSAGE_HEADER_SIZE  2
#define MESSAGE_TRAILER_SIZE 3
#define MESSAGE_POS_LEN 0
#define MESSAGE_POS_SEQ 1
#define MESSAGE_TRAILER_CRC  3
#define MESSAGE_TRAILER_SYNC 1
#define MESSAGE_PAYLOAD_MAX (MESSAGE_MAX - MESSAGE_MIN)
#define MESSAGE_SEQ_MASK 0x0f
#define MESSAGE_DEST 0x10
#define MESSAGE_SYNC 0x7E

// Delivery semantics retained after commands are packed into protocol blocks.
// Urgent is the historical behavior. Buffered work may use a more patient
// transport RTO while its execution deadline remains safely in the future.
#define MESSAGE_RETRY_URGENT   0
#define MESSAGE_RETRY_BUFFERED 1

struct queue_message {
    int len;
    uint8_t msg[MESSAGE_MAX];
    union {
        // Filled when on a command queue
        struct {
            uint64_t min_clock, req_clock;
        };
        // Filled when in sent/receive queues
        struct {
            double sent_time, receive_time;
        };
    };
    uint64_t retry_clock;
    uint64_t notify_id;
    uint8_t retry_class;
    uint8_t retry_attempts;
    struct list_node node;
};

struct clock_estimate {
    uint64_t last_clock, conv_clock;
    double conv_time, est_freq;
};

uint16_t msgblock_crc16_ccitt(uint8_t *buf, uint8_t len);
int msgblock_check(uint8_t *need_sync, uint8_t *buf, int buf_len);
int msgblock_decode(uint32_t *data, int data_len, uint8_t *msg, int msg_len);
struct queue_message *message_alloc(void);
struct queue_message *message_fill(uint8_t *data, int len);
struct queue_message *message_alloc_and_encode(uint32_t *data, int len);
void message_free(struct queue_message *qm);
void message_queue_free(struct list_head *root);
uint64_t clock_from_clock32(struct clock_estimate *ce, uint32_t clock32);
double clock_to_time(struct clock_estimate *ce, uint64_t clock);
uint64_t clock_from_time(struct clock_estimate *ce, double time);
void clock_fill(struct clock_estimate *ce, double est_freq, double conv_time
                , uint64_t conv_clock, uint64_t last_clock);

#endif // msgblock.h
