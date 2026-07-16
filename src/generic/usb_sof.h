#ifndef __GENERIC_USB_SOF_H
#define __GENERIC_USB_SOF_H

#include <stdint.h>

void usb_sof_notify(uint16_t frame, uint32_t clock);
void usb_sof_board_enable(uint8_t enable);
void usb_sof_board_discard_pending(void);

#endif // generic/usb_sof.h
