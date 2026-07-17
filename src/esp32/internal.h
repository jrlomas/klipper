#ifndef __ESP32_INTERNAL_H
#define __ESP32_INTERNAL_H
// Local definitions for the ESP32 port

#include <stdint.h> // uint32_t
#include "sdkconfig.h" // CONFIG_KLIPPER_ARCH_MODEM

// Architecture selection (FD-0001 doc 12).  "component": klipper
// runs as a FreeRTOS task pinned to core 1 inside the IDF app (the
// stage-1 build).  "modem": core 1 runs bare-metal klipper booted by
// appcpu_boot.c and IDF is reduced to a core-0 network coprocessor
// behind the shared-memory ring (stage 3).
#ifdef CONFIG_KLIPPER_ARCH_MODEM
#define KLIPPER_ARCH_MODEM 1
#else
#define KLIPPER_ARCH_MODEM 0
#endif

// Kconfig booleans that are disabled are absent from sdkconfig.h, so expose a
// C value for code that needs to pass the setting to the transport glue.
#ifdef CONFIG_KLIPPER_TRUST_NETWORK
#define KLIPPER_TRUST_NETWORK 1
#else
#define KLIPPER_TRUST_NETWORK 0
#endif

// Place a function in IRAM (mapped by the IDF linker's *(.iram1
// .iram1.*) rule in both architectures).  Code that runs on the
// motion core's hot path must never fault into the flash cache: a
// miss stalls on the SPI flash controller and - because the cache
// fill path is shared - can stall BOTH cores.  See "IRAM discipline"
// in docs/ESP32.md.  Unique subsections (mirroring IDF's IRAM_ATTR)
// keep --gc-sections effective.
#define _DECL_IRAM2(cnt) __attribute__((section(".iram1.klipper." #cnt)))
#define _DECL_IRAM1(cnt) _DECL_IRAM2(cnt)
#define DECL_IRAM _DECL_IRAM1(__COUNTER__)

// irq.c (component arch)
void board_wake_main(void);
void board_wake_main_from_isr(void);
void board_set_main_task(void *task_handle);

// timer.c (component arch)
void esp32_timer_setup(void);
uint8_t esp32_timer_ready(void);

// adc.c / gpio.c
#define ESP32_GPIO_COUNT 40

// gpio.c - register-level pad configuration (IO_MUX + GPIO matrix +
// RTC pull table), used by the modem architecture where the IDF gpio
// driver is off limits on the motion core
void esp32_pad_config(uint32_t pin, int input_en, int output_en
                      , int open_drain, int pull /* 1 up, -1 down, 0 none */);
void esp32_matrix_out(uint32_t pin, uint32_t sig_idx);
void esp32_matrix_in(uint32_t pin, uint32_t sig_idx);

// adc.c / adc_stream.c (IDF ADC drivers are confined to core 0)
void esp32_adc_init(void);
uint8_t esp32_adc_legacy_is_active(void);
void esp32_adc_stream_init(void);
void esp32_adc_stream_poll(void);
uint8_t esp32_adc_stream_is_claimed(void);

// wifi.c
void esp32_wifi_start(void);

// udp_port.c (component arch)
int esp32_udp_port_setup(uint16_t port, const uint8_t *psk
                         , uint32_t psk_len);

// modem.c (modem arch: core-0 datagram shuttle)
int esp32_modem_start(uint16_t port);

// shmem_console.c (modem arch: core-1 console backing)
void shmem_console_init(void);
void shmem_console_poll(void);

// appcpu_boot.c (modem arch)
int esp32_appcpu_start(void);
// flag, exccause, epc, excvaddr, vector, PS, WINDOWBASE, WINDOWSTART, a0
extern uint32_t esp32_core1_fault[9];

#endif // internal.h
