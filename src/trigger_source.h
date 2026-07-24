#ifndef __TRIGGER_SOURCE_H
#define __TRIGGER_SOURCE_H

#include <stdint.h>
#include "board/gpio.h" // struct gpio_in
#include "sched.h" // struct timer

// Hardware event trigger sources (FD-0001 doc 09): edge interrupts
// and analog comparators fire trsync directly, replacing timer-list
// polling for detection. The polled endstop path remains the
// portability fallback.

struct trsync;

struct trigger_source {
    struct trsync *ts;
    // Production homing arms at the same MCU clock carried by the motion
    // start.  This prevents an early host command from observing the old
    // switch level and stopping a still-pending retract.
    struct timer arm_timer;
    // Mask/unmask the hardware event delivery (called irqs off or
    // from irq context); may be NULL for always-on sources.
    void (*hw_arm)(struct trigger_source *tsrc, int enable);
    struct gpio_in pin_in;   // for qualify-after-event re-reads
    uint32_t pin;
    uint32_t qualify_ticks;
    uint32_t trigger_clock;  // timestamp latched at hardware event
    uint32_t hw[2];          // board-half scratch (gpio: capture chan;
                             // adc_watchdog: [0]=high [1]=low threshold)
    uint8_t qualify_count;
    uint8_t edge;            // 1 = rising (trigger level high)
    uint8_t reason;
    uint8_t flags;
    uint8_t kind;
    uint8_t oid;
};

enum { TS_KIND_GPIO, TS_KIND_COMP, TS_KIND_ADC_WATCHDOG };
enum {
    TSRC_ARMED = 1 << 0, TSRC_TRIGGERED = 1 << 1, TSRC_CAN_QUALIFY = 1 << 2,
    // Timer input-capture (FD-0001 doc 09 sec 3): CAN_CAPTURE means the
    // board wired this source's pin to a capture channel; CAPTURE_ON
    // means the host armed it with capture=1 so the latched
    // hardware-exact edge tick is used instead of the ISR-entry read.
    TSRC_CAN_CAPTURE = 1 << 3, TSRC_CAPTURE_ON = 1 << 4,
    // Observer mode timestamps and records the edge but deliberately does
    // not fire trsync. It permits a direct comparison with legacy polling.
    TSRC_OBSERVER = 1 << 5,
    TSRC_ARM_PENDING = 1 << 6,
};

// Allocate a trigger source oid for a non-gpio hardware kind (e.g.
// the analog comparator); the arm/disarm/query commands then apply.
struct trigger_source *trigger_source_alloc(uint8_t oid, uint8_t kind);

// Board-half contract for the gpio_edge kind (implemented per-mcu,
// gated by CONFIG_HAVE_GPIO_EDGE_TRIGGER):
int board_edge_trigger_setup(struct trigger_source *tsrc);
void board_edge_trigger_arm(struct trigger_source *tsrc, int enable);

// Board-half contract for the adc_watchdog kind (analog auto-compare
// while the ADC free-runs; FD-0001 doc 09 sec 2 fallback where COMP is
// absent). Returns 0 if the pin/thresholds were accepted.
int board_adc_watchdog_setup(struct trigger_source *tsrc);
void board_adc_watchdog_arm(struct trigger_source *tsrc, int enable);

// Board-half timer input-capture hooks. _setup tries to route the
// source's pin to a capture channel of the free-running system-time
// timer; returns 1 and sets TSRC_CAN_CAPTURE when wired, else 0
// (caller falls back to the ISR-entry timestamp). _read returns the
// latched capture tick and is only called for wired+armed sources.
int board_timer_capture_setup(struct trigger_source *tsrc);
uint32_t board_timer_capture_read(struct trigger_source *tsrc);

// Board -> generic event delivery; called from the peripheral IRQ
// with the hardware timestamp (or timer_read_time() at IRQ entry).
void trigger_source_notify(struct trigger_source *tsrc, uint32_t clock);

#endif // trigger_source.h
