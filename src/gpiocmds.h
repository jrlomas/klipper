#ifndef __GPIOCMDS_H
#define __GPIOCMDS_H

#include <stdint.h>

// Cancel queued/toggling software-PWM work for a configured digital output
// and set its pin immediately. Returns zero when the pin was found.
int digital_out_takeover_pin(uint32_t pin, uint8_t value);
int digital_out_release_pin(uint32_t pin);

#endif // gpiocmds.h
