#ifndef __ESP32_AUTOCONF_H
#define __ESP32_AUTOCONF_H
// Hand-written static autoconf.h for the ESP32 target.
//
// The ESP32 port is built by ESP-IDF (idf.py) rather than klipper's
// Kconfig/Makefile flow, so the usual generated out/autoconf.h does
// not exist; this file provides the CONFIG_* symbols the compiled
// klipper sources reference.
//
// CONFIG_CLOCK_FREQ is 20MHz: the klipper timer is a GPTimer fed
// from the 80MHz APB clock, and 20MHz is the highest integer-divided
// rate that keeps a comfortable 32-bit wraparound period (~214s;
// klipper only needs it to be well over a few seconds) while giving
// 50ns scheduling granularity.  See src/esp32/timer.c.

#define CONFIG_MCU "esp32"
#define CONFIG_BOARD_DIRECTORY "esp32"
#define CONFIG_CLOCK_FREQ 20000000

#define CONFIG_MACH_ESP32 1
#define CONFIG_MACH_AVR 0
#define CONFIG_MACH_LINUX 0

#define CONFIG_HAVE_GPIO 1
#define CONFIG_HAVE_GPIO_ADC 1
#define CONFIG_HAVE_GPIO_SPI 1
#define CONFIG_HAVE_GPIO_I2C 1
#define CONFIG_HAVE_GPIO_HARD_PWM 1
#define CONFIG_HAVE_GPIO_EDGE_TRIGGER 0
#define CONFIG_HAVE_STRICT_TIMING 0
#define CONFIG_HAVE_CHIPID 0
#define CONFIG_HAVE_BOOTLOADER_REQUEST 0
#define CONFIG_HAVE_LIMITED_CODE_SIZE 0

#define CONFIG_WANT_ADC 1
#define CONFIG_WANT_SPI 1
#define CONFIG_WANT_SOFTWARE_SPI 1
#define CONFIG_WANT_I2C 1
#define CONFIG_WANT_SOFTWARE_I2C 1
#define CONFIG_WANT_HARD_PWM 1
#define CONFIG_WANT_BUTTONS 1
#define CONFIG_WANT_TMCUART 1
#define CONFIG_WANT_NEOPIXEL 1
#define CONFIG_WANT_TRAJECTORY 1
#define CONFIG_WANT_HEATER_HOLD 1
#define CONFIG_WANT_TRIGGER_ANALOG 0
#define CONFIG_WANT_TRIGGER_SOURCE 0

// The WiFi/RTOS environment cannot honor tick-exact step pulse
// timing guarantees (RFC 0001 doc 07's caution); the classic stepper
// backend compiles and runs but is experimental on this chip.
#define CONFIG_INLINE_STEPPER_HACK 1
#define CONFIG_HAVE_STEPPER_OPTIMIZED_BOTH_EDGE 0
#define CONFIG_WANT_STEPPER_OPTIMIZED_BOTH_EDGE 0

#define CONFIG_INITIAL_PINS ""

#endif // autoconf.h
