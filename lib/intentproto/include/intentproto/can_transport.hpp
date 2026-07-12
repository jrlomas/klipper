#ifndef INTENTPROTO_CAN_TRANSPORT_HPP
#define INTENTPROTO_CAN_TRANSPORT_HPP

#include <stddef.h>
#include <stdint.h>

// CAN carrier for the intentproto framed byte stream (RFC 0001 doc 07).
//
// Klipper's CAN bus is a byte-stream carrier that sits *below* the
// CRC16/VLQ framing, exactly as UART and USB do: an outgoing protocol
// frame is split into <=8-byte CAN data frames and the receiver
// reassembles the byte stream and locates frames by the framing itself.
// Because intentproto reproduces the legacy framing, it rides CAN the
// same way legacy Klipper does -- this carrier only has to chunk on
// transmit and forward on receive (intentproto::rx() already accepts
// bytes in any chunking, so no receive reassembly buffer is needed).
//
// Node addressing mirrors Klipper's UUID admin handshake so an
// intentproto device is a drop-in CAN peer: the host queries unassigned
// nodes, the device answers with its UUID, the host assigns a 1-byte
// node id, and data then flows on the derived CAN identifiers.

namespace intentproto {

struct CanFrame {
    uint32_t id;
    uint8_t dlc;        // 0..8
    uint8_t data[8];
};

enum {
    // Klipper CAN wire constants (src/generic/canserial.c).
    CAN_ID_ADMIN = 0x3f0,
    CAN_ID_ADMIN_RESP = 0x3f1,
    CAN_UUID_LEN = 6,
    // Admin commands / responses.
    CAN_CMD_QUERY_UNASSIGNED = 0x00,
    CAN_CMD_SET_NODEID = 0x01,
    CAN_CMD_REQUEST_BOOTLOADER = 0x02,
    CAN_RESP_NEED_NODEID = 0x20,
};

// Data identifiers derive from the assigned node id exactly as Klipper
// does: host->device on assigned_id, device->host on assigned_id+1.
inline uint32_t can_nodeid_to_id(int nodeid) {
    return (uint32_t)(nodeid << 1) + 0x100;
}
inline int can_id_to_nodeid(uint32_t id) {
    return (int)((id - 0x100) >> 1);
}

struct CanCarrier {
    // Emit one CAN frame; return >=0 on success (the caller retries a
    // negative return, as Klipper's driver does when the mailbox is
    // momentarily full).
    int (*send)(const CanFrame& f, void* user);
    // Optional: update the hardware receive filter when the node id is
    // (re)assigned. May be null.
    void (*set_filter)(uint32_t rx_id, void* user);
    // Optional: honor an admin bootloader request. May be null.
    void (*request_bootloader)(void* user);
    void* user;
    uint8_t uuid[CAN_UUID_LEN];
    uint32_t assigned_id;   // 0 until the host assigns; also the rx id

    void init(const uint8_t uuid_in[CAN_UUID_LEN],
              int (*send_fn)(const CanFrame&, void*), void* user_in);

    // Plug this into intentproto::Config::write via can_write_thunk():
    // split a whole protocol frame into <=8-byte CAN data frames on the
    // device's tx id. A no-op (returns -1) until a node id is assigned.
    int write_frame(const uint8_t* data, size_t len);

    // Feed one received CAN frame. Admin frames drive node assignment;
    // data frames addressed to this node are forwarded to
    // intentproto::rx(). Frames for other ids are ignored.
    void on_can_frame(const CanFrame& f);

    // 0 until assigned. rx id == assigned_id, tx id == assigned_id + 1.
    uint32_t rx_id() const { return assigned_id; }
    int node_id() const {
        return assigned_id ? can_id_to_nodeid(assigned_id) : -1;
    }

private:
    void process_admin(const CanFrame& f);
    bool uuid_matches(const CanFrame& f) const;
};

// intentproto::Config::write-compatible thunk: bind a CanCarrier as the
// Config::user and set Config::write = can_write_thunk.
int can_write_thunk(const uint8_t* data, size_t len, void* user);

} // namespace intentproto

#endif // INTENTPROTO_CAN_TRANSPORT_HPP
