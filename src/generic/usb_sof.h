#ifndef __GENERIC_USB_SOF_H
#define __GENERIC_USB_SOF_H

#include <stdint.h>

#define USB_SOF_GUARD_PROBE_VALID 0x01
#define USB_SOF_GUARD_PROBE_PENDING 0x02

#define USB_SOF_GUARD_ENTRY_PRE_VALID 0x01
#define USB_SOF_GUARD_ENTRY_PRE_PENDING 0x02
#define USB_SOF_GUARD_ENTRY_POST_PENDING 0x04
#define USB_SOF_GUARD_ENTRY_ACTIVE 0x08

extern uint8_t usb_sof_guard_enabled;

void usb_sof_notify(uint16_t frame, uint32_t clock);
void usb_sof_note_discard(uint16_t frame, uint8_t primask, uint32_t source
                          , uint32_t source_caller, uint32_t exit_source
                          , uint32_t exit_caller, uint32_t duration
                          , uint8_t entry_flags);
void usb_sof_board_enable(uint8_t enable);
uint8_t usb_sof_board_guard_probe(void);
void usb_sof_board_guard_begin(uint32_t source, uint32_t source_caller
                               , uint8_t probe);
void usb_sof_board_guard_end(uint32_t exit_source, uint32_t exit_caller);

#endif // generic/usb_sof.h
