#ifndef __ESP32_INTERNAL_H
#define __ESP32_INTERNAL_H
// Local definitions for the ESP32 port

#include <stdint.h> // uint32_t

// irq.c
void board_wake_main(void);
void board_wake_main_from_isr(void);
void board_set_main_task(void *task_handle);

// timer.c
void esp32_timer_setup(void);

// adc.c / gpio.c
#define ESP32_GPIO_COUNT 40

// wifi.c
void esp32_wifi_start(void);

// udp_port.c
int esp32_udp_port_setup(uint16_t port, const uint8_t *psk
                         , uint32_t psk_len);

#endif // internal.h
