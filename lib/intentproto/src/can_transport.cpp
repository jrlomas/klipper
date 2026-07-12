// CAN carrier for the intentproto framed byte stream (RFC 0001 doc 07).
//
// See can_transport.hpp for the design. This file is transport-agnostic:
// the actual CAN hardware access is the caller's `send` hook, so the
// carrier host-compiles and unit-tests off silicon.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the MIT license.

#include "intentproto/can_transport.hpp"

#include <string.h> // memcmp, memcpy

#include "intentproto/proto.hpp" // intentproto::rx

namespace intentproto {

void
CanCarrier::init(const uint8_t uuid_in[CAN_UUID_LEN],
                 int (*send_fn)(const CanFrame&, void*), void* user_in)
{
    send = send_fn;
    set_filter = nullptr;
    request_bootloader = nullptr;
    user = user_in;
    assigned_id = 0;
    memcpy(uuid, uuid_in, CAN_UUID_LEN);
}

int
CanCarrier::write_frame(const uint8_t* data, size_t len)
{
    if (!assigned_id || !send)
        return -1;
    CanFrame f;
    f.id = assigned_id + 1;     // device -> host
    size_t off = 0;
    while (off < len) {
        size_t n = len - off;
        if (n > 8)
            n = 8;
        f.dlc = (uint8_t)n;
        memcpy(f.data, data + off, n);
        // Retry a momentarily-full mailbox, matching Klipper's driver.
        while (send(f, user) < 0)
            ;
        off += n;
    }
    return (int)len;
}

bool
CanCarrier::uuid_matches(const CanFrame& f) const
{
    return f.dlc >= 1 + CAN_UUID_LEN
        && memcmp(&f.data[1], uuid, CAN_UUID_LEN) == 0;
}

void
CanCarrier::process_admin(const CanFrame& f)
{
    if (!f.dlc)
        return;
    switch (f.data[0]) {
    case CAN_CMD_QUERY_UNASSIGNED: {
        if (assigned_id || !send)
            return;
        CanFrame r;
        r.id = CAN_ID_ADMIN_RESP;
        r.dlc = 8;
        r.data[0] = CAN_RESP_NEED_NODEID;
        memcpy(&r.data[1], uuid, CAN_UUID_LEN);
        r.data[7] = CAN_CMD_SET_NODEID;
        while (send(r, user) < 0)
            ;
        break;
    }
    case CAN_CMD_SET_NODEID: {
        if (f.dlc < 8)
            return;
        uint32_t newid = can_nodeid_to_id(f.data[7]);
        if (uuid_matches(f)) {
            if (newid != assigned_id) {
                assigned_id = newid;
                if (set_filter)
                    set_filter(assigned_id, user);
            }
        }
        // (A mismatched-UUID frame reclaiming our id is an id conflict;
        // the firmware transport detects that at the hardware-filter
        // level, so the library does not shut down here.)
        break;
    }
    case CAN_CMD_REQUEST_BOOTLOADER:
        if (request_bootloader && uuid_matches(f))
            request_bootloader(user);
        break;
    }
}

void
CanCarrier::on_can_frame(const CanFrame& f)
{
    if (f.id == CAN_ID_ADMIN) {
        process_admin(f);
        return;
    }
    if (assigned_id && f.id == assigned_id) {
        // Host -> device data: forward straight to the frame parser,
        // which reassembles across CAN-frame boundaries on its own.
        uint8_t n = f.dlc > 8 ? 8 : f.dlc;
        rx(f.data, n);
    }
}

int
can_write_thunk(const uint8_t* data, size_t len, void* user)
{
    return static_cast<CanCarrier*>(user)->write_frame(data, len);
}

} // namespace intentproto
