// Klipper command console on the secondary CDC interface of a composite USB
// CAN bridge.
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memmove
#include "board/misc.h" // console_sendf
#include "board/pgm.h" // READP
#include "command.h" // command_encode_and_frame
#include "generic/usb_cdc.h" // usb_send_local_bulk_in
#include "generic/usb_cdc_ep.h" // USB_CDC_EP_BULK_IN_SIZE
#include "sched.h" // sched_wake_task

static struct task_wake local_bulk_in_wake;
static uint8_t transmit_buf[192], transmit_pos;

void
usb_notify_local_bulk_in(void)
{
    sched_wake_task(&local_bulk_in_wake);
}

void
usb_local_bulk_in_task(void)
{
    if (!sched_check_wake(&local_bulk_in_wake))
        return;
    uint_fast8_t tpos = transmit_pos, max_tpos = tpos;
    if (!tpos)
        return;
    if (max_tpos > USB_CDC_EP_BULK_IN_SIZE)
        max_tpos = USB_CDC_EP_BULK_IN_SIZE;
    else if (max_tpos == USB_CDC_EP_BULK_IN_SIZE)
        max_tpos = USB_CDC_EP_BULK_IN_SIZE - 1;
    int_fast8_t ret = usb_send_local_bulk_in(transmit_buf, max_tpos);
    if (ret <= 0)
        return;
    uint_fast8_t needcopy = tpos - ret;
    if (needcopy) {
        memmove(transmit_buf, &transmit_buf[ret], needcopy);
        usb_notify_local_bulk_in();
    }
    transmit_pos = needcopy;
}
DECL_TASK(usb_local_bulk_in_task);

void
console_sendf(const struct command_encoder *ce, va_list args)
{
    uint_fast8_t tpos = transmit_pos;
    if (tpos + READP(ce->min_size) > sizeof(transmit_buf))
        return;
    uint8_t *buf = &transmit_buf[tpos];
    uint_fast8_t msglen = command_encode_and_frame(
        buf, sizeof(transmit_buf) - tpos, ce, args);
    if (!msglen)
        return;
    transmit_pos = tpos + msglen;
    usb_notify_local_bulk_in();
}

static struct task_wake local_bulk_out_wake;
static uint8_t receive_buf[128], receive_pos;

void
usb_notify_local_bulk_out(void)
{
    sched_wake_task(&local_bulk_out_wake);
}

void
usb_local_bulk_out_task(void)
{
    if (!sched_check_wake(&local_bulk_out_wake))
        return;
    uint_fast8_t rpos = receive_pos, pop_count;
    if (rpos + USB_CDC_EP_BULK_OUT_SIZE <= sizeof(receive_buf)) {
        int_fast8_t ret = usb_read_local_bulk_out(
            &receive_buf[rpos], USB_CDC_EP_BULK_OUT_SIZE);
        if (ret > 0) {
            rpos += ret;
            usb_notify_local_bulk_out();
        }
    } else {
        usb_notify_local_bulk_out();
    }
    int_fast8_t ret = command_find_and_dispatch(receive_buf, rpos, &pop_count);
    if (ret) {
        uint_fast8_t needcopy = rpos - pop_count;
        if (needcopy) {
            memmove(receive_buf, &receive_buf[pop_count], needcopy);
            usb_notify_local_bulk_out();
        }
        rpos = needcopy;
    }
    receive_pos = rpos;
}
DECL_TASK(usb_local_bulk_out_task);

void
usb_local_console_configure(void)
{
    receive_pos = transmit_pos = 0;
    usb_notify_local_bulk_in();
    usb_notify_local_bulk_out();
}

void
usb_local_console_shutdown(void)
{
    usb_notify_local_bulk_in();
    usb_notify_local_bulk_out();
}
