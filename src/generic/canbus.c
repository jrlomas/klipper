// Wrapper functions connecting canserial.c to low-level can hardware
//
// Copyright (C) 2022  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_CANBUS_FREQUENCY
#include "canbus.h" // canhw_send
#include "canserial.h" // canserial_notify_tx
#include "command.h" // DECL_CONSTANT

DECL_CONSTANT("CANBUS_FREQUENCY", CONFIG_CANBUS_FREQUENCY);

int
canbus_send(struct canbus_msg *msg)
{
    return canhw_send(msg);
}

void
canbus_set_filter(uint32_t id)
{
    canhw_set_filter(id);
}

void
canbus_notify_tx(void)
{
    canserial_notify_tx();
}

void
canbus_process_data(struct canbus_msg *msg)
{
    canserial_process_data(msg);
}

void
canbus_notify_tx_timestamp(uint8_t tag, uint32_t local_clock)
{
    (void)tag;
    (void)local_clock;
}

void
canbus_notify_tx_failed(uint8_t tag)
{
    (void)tag;
}

void
canbus_notify_tx_event_lost(void)
{
}

void
canbus_notify_protocol_error(void)
{
    // FDCAN already confines and retransmits physical protocol errors.  The
    // low-level driver accounts each observation for diagnostics; do not turn
    // a recoverable retransmission burst into a global firmware shutdown.
}

void
canbus_notify_bus_off(void)
{
    canserial_notify_bus_off();
}
