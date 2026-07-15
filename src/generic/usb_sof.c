// USB Start-of-Frame timestamp commissioning support
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/irq.h" // irq_disable
#include "command.h" // DECL_COMMAND
#include "usb_sof.h" // usb_sof_notify

#define USB_SOF_RING_SIZE 32
#define USB_SOF_LATEST 0xffff

struct usb_sof_sample {
    uint32_t clock;
    uint32_t count;
    uint16_t frame;
};

static struct usb_sof_sample usb_sof_ring[USB_SOF_RING_SIZE];
static uint32_t usb_sof_count;

void
usb_sof_notify(uint16_t frame, uint32_t clock)
{
    uint32_t count = usb_sof_count + 1;
    struct usb_sof_sample *sample = &usb_sof_ring[
        (count - 1) % USB_SOF_RING_SIZE];
    sample->clock = clock;
    sample->count = count;
    sample->frame = frame;
    usb_sof_count = count;
}

void
command_usb_sof_enable(uint32_t *args)
{
    uint8_t enable = !!args[0];
    irq_disable();
    usb_sof_count = 0;
    usb_sof_board_enable(enable);
    irq_enable();
}
DECL_COMMAND(command_usb_sof_enable, "usb_sof_enable enable=%c");

void
command_usb_sof_query(uint32_t *args)
{
    uint16_t requested = args[0];
    struct usb_sof_sample result = {};
    uint8_t found = 0;
    irq_disable();
    uint32_t count = usb_sof_count;
    uint_fast8_t available = count < USB_SOF_RING_SIZE
                             ? count : USB_SOF_RING_SIZE;
    uint_fast8_t i;
    for (i = 0; i < available; i++) {
        struct usb_sof_sample *sample = &usb_sof_ring[
            (count - i - 1) % USB_SOF_RING_SIZE];
        if (requested == USB_SOF_LATEST || sample->frame == requested) {
            result = *sample;
            found = 1;
            break;
        }
    }
    irq_enable();
    sendf("usb_sof_state requested=%hu found=%c frame=%hu clock=%u"
          " count=%u", requested, found, result.frame, result.clock,
          result.count);
}
DECL_COMMAND(command_usb_sof_query, "usb_sof_query frame=%hu");
