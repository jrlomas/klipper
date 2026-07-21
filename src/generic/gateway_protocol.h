#ifndef __GENERIC_GATEWAY_PROTOCOL_H
#define __GENERIC_GATEWAY_PROTOCOL_H

// Helix authenticated-gateway payload format.  Authentication, replay
// protection, and packet sequencing belong to the surrounding intentproto
// datagram session; this layer multiplexes typed services within that packet.

#include <stdint.h>

#define HELIX_GATEWAY_MAGIC 0x4748u /* "HG" on the little-endian wire */
#define HELIX_GATEWAY_VERSION 1
#define HELIX_GATEWAY_HEADER_SIZE 16
#define HELIX_GATEWAY_RECORD_HEADER_SIZE 12
#define HELIX_GATEWAY_MAX_RECORD_DATA 128
#define HELIX_GATEWAY_MAX_SERVICES 8

enum helix_gateway_service {
    HELIX_GATEWAY_SERVICE_CONTROL = 0,
    HELIX_GATEWAY_SERVICE_CAN = 1,
    HELIX_GATEWAY_SERVICE_SERIAL = 2,
};

enum helix_gateway_packet_flags {
    HELIX_GATEWAY_PACKET_RESET = 1 << 0,
    HELIX_GATEWAY_PACKET_ACK_ONLY = 1 << 1,
};

enum helix_gateway_record_flags {
    HELIX_GATEWAY_RECORD_REPLY = 1 << 0,
    HELIX_GATEWAY_RECORD_ERROR = 1 << 1,
    HELIX_GATEWAY_RECORD_MORE = 1 << 2,
    HELIX_GATEWAY_RECORD_TIMESTAMP_VALID = 1 << 3,
};

enum helix_gateway_control_opcode {
    HELIX_GATEWAY_CONTROL_CONSOLE = 1,
    HELIX_GATEWAY_CONTROL_CREDIT = 2,
    HELIX_GATEWAY_CONTROL_STATUS = 3,
    HELIX_GATEWAY_CONTROL_TAKEOVER = 4,
    HELIX_GATEWAY_CONTROL_ACK = 5,
    HELIX_GATEWAY_CONTROL_TIME_SYNC = 6,
};

enum helix_gateway_can_opcode {
    HELIX_GATEWAY_CAN_FRAME = 1,
    HELIX_GATEWAY_CAN_CONFIG = 2,
    HELIX_GATEWAY_CAN_STATUS = 3,
    HELIX_GATEWAY_CAN_BUS_OFF = 4,
    HELIX_GATEWAY_CAN_DELIVERY = 5,
};

enum helix_gateway_can_config_action {
    HELIX_GATEWAY_CAN_CONFIG_QUERY = 0,
    HELIX_GATEWAY_CAN_CONFIG_PREPARE = 1,
    HELIX_GATEWAY_CAN_CONFIG_COMMIT = 2,
    HELIX_GATEWAY_CAN_CONFIG_ABORT = 3,
};

enum helix_gateway_delivery_state {
    HELIX_GATEWAY_DELIVERY_ADMITTED = 1,
    HELIX_GATEWAY_DELIVERY_SUBMITTED = 2,
    HELIX_GATEWAY_DELIVERY_COMPLETED = 3,
    HELIX_GATEWAY_DELIVERY_FAILED = 4,
    HELIX_GATEWAY_DELIVERY_UNKNOWN = 5,
};

enum helix_gateway_serial_opcode {
    HELIX_GATEWAY_SERIAL_DATA = 1,
    HELIX_GATEWAY_SERIAL_CONFIG = 2,
    HELIX_GATEWAY_SERIAL_STATUS = 3,
    HELIX_GATEWAY_SERIAL_BREAK = 4,
};

struct helix_gateway_packet {
    uint8_t flags;
    uint32_t epoch;
    uint32_t sequence;
    uint16_t record_count;
    uint16_t payload_length;
};

struct helix_gateway_record {
    uint8_t service;
    uint8_t opcode;
    uint16_t channel;
    uint16_t flags;
    uint16_t length;
    uint32_t cookie;
    const uint8_t *data;
};

struct helix_gateway_can_frame {
    uint32_t can_id;
    uint32_t hw_clock;
    uint8_t length;
    uint8_t flags;
    const uint8_t *data;
};

struct helix_gateway_can_config {
    uint8_t action;
    uint8_t fd;
    uint8_t brs;
    uint32_t epoch;
    uint32_t nominal_bitrate;
    uint32_t data_bitrate;
};

struct helix_gateway_delivery {
    uint8_t state;
    uint8_t error;
    uint32_t cookie;
    uint32_t hw_clock;
    uint32_t detail;
};

struct helix_gateway_ack {
    uint32_t epoch;
    uint32_t sequence;
    uint32_t mask;
};

enum helix_gateway_time_action {
    HELIX_GATEWAY_TIME_REQUEST = 0,
    HELIX_GATEWAY_TIME_RESPONSE = 1,
};

struct helix_gateway_time_exchange {
    uint8_t action;
    uint8_t quality;
    uint32_t epoch;
    uint64_t t1;
    uint64_t t2;
    uint64_t t3;
};

int helix_gateway_packet_encode(uint8_t *out, uint32_t cap,
                                const struct helix_gateway_packet *packet);
int helix_gateway_packet_decode(struct helix_gateway_packet *packet,
                                const uint8_t *data, uint32_t length);
int helix_gateway_record_encode(uint8_t *out, uint32_t cap,
                                const struct helix_gateway_record *record);
int helix_gateway_record_decode(struct helix_gateway_record *record,
                                const uint8_t *data, uint32_t length);
int helix_gateway_can_encode(uint8_t *out, uint32_t cap,
                             const struct helix_gateway_can_frame *frame);
int helix_gateway_can_decode(struct helix_gateway_can_frame *frame,
                             const uint8_t *data, uint32_t length);
int helix_gateway_can_config_encode(
    uint8_t *out, uint32_t cap,
    const struct helix_gateway_can_config *config);
int helix_gateway_can_config_decode(
    struct helix_gateway_can_config *config,
    const uint8_t *data, uint32_t length);
int helix_gateway_delivery_encode(
    uint8_t *out, uint32_t cap,
    const struct helix_gateway_delivery *delivery);
int helix_gateway_delivery_decode(
    struct helix_gateway_delivery *delivery,
    const uint8_t *data, uint32_t length);
int helix_gateway_ack_encode(uint8_t *out, uint32_t cap,
                             const struct helix_gateway_ack *ack);
int helix_gateway_ack_decode(struct helix_gateway_ack *ack,
                             const uint8_t *data, uint32_t length);
int helix_gateway_time_encode(
    uint8_t *out, uint32_t cap,
    const struct helix_gateway_time_exchange *exchange);
int helix_gateway_time_decode(
    struct helix_gateway_time_exchange *exchange,
    const uint8_t *data, uint32_t length);

#endif
