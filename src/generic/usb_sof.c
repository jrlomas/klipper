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

struct usb_sof_discard {
    uint32_t count;
    uint16_t frame;
    uint8_t primask;
};

static struct usb_sof_sample usb_sof_ring[USB_SOF_RING_SIZE];
static struct usb_sof_discard usb_sof_discard_ring[USB_SOF_RING_SIZE];
static uint32_t usb_sof_count, usb_sof_discard_count;
static uint32_t usb_sof_discard_primask_count;

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
usb_sof_note_discard(uint16_t frame, uint8_t primask)
{
    uint32_t count = usb_sof_discard_count + 1;
    struct usb_sof_discard *sample = &usb_sof_discard_ring[
        (count - 1) % USB_SOF_RING_SIZE];
    sample->count = count;
    sample->frame = frame;
    sample->primask = primask;
    usb_sof_discard_count = count;
    usb_sof_discard_primask_count += !!primask;
}

void
command_usb_sof_enable(uint32_t *args)
{
    uint8_t enable = !!args[0];
    irq_disable();
    usb_sof_count = 0;
    usb_sof_discard_count = 0;
    usb_sof_discard_primask_count = 0;
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
    uint8_t discard_match = 0, discard_match_primask = 0;
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
    uint32_t discard_count = usb_sof_discard_count;
    uint_fast8_t discard_available = (
        discard_count < USB_SOF_RING_SIZE
        ? discard_count : USB_SOF_RING_SIZE);
    for (i = 0; i < discard_available; i++) {
        struct usb_sof_discard *sample = &usb_sof_discard_ring[
            (discard_count - i - 1) % USB_SOF_RING_SIZE];
        if (sample->frame == requested) {
            discard_match = 1;
            discard_match_primask = sample->primask;
            break;
        }
    }
    uint32_t discard_primask_count = usb_sof_discard_primask_count;
    irq_enable();
    sendf("usb_sof_state requested=%hu found=%c frame=%hu clock=%u"
          " count=%u capture_count=%u discard_count=%u"
          " discard_primask_count=%u"
          " discard_match=%c discard_match_primask=%c"
          , requested, found, result.frame, result.clock, result.count
          , count, discard_count, discard_primask_count
          , discard_match, discard_match_primask);
}
DECL_COMMAND(command_usb_sof_query, "usb_sof_query frame=%hu");
