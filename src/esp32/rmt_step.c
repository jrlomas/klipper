// Experimental hardware-timed step pulse emission via the ESP32 RMT
// peripheral (FD-0001 docs 07/12: the flagged escape hatch for step
// generation on this chip, where WiFi-induced ISR jitter makes
// classic timer-IRQ stepping suspect).
//
// What this module is: a register-level pulse-train emitter.  Each
// RMT channel owns a 64-item on-chip RAM used as a ring buffer in
// wrap mode; items (15-bit duration + level, twice per item) are
// generated from klipper-style (interval, count, add) move triples
// at CONFIG_CLOCK_FREQ resolution (RMT tick = APB 80MHz / 4 =
// 20MHz), long gaps become low-level filler items, and a
// threshold interrupt refills half the ring while the other half
// transmits.  Emitted edges are hardware timed - WiFi and flash
// stalls cannot jitter them.
//
// The stepper backend that consumes this emitter is
// src/esp32/rmt_stepper.c (built in place of the portable stepper.c
// when CONFIG_WANT_ESP32_RMT_STEP is set): it owns the pulse stream,
// fences the dir GPIO across trains, anchors the first pulse to the
// klipper clock, and wires the wrap-underrun latch below to the
// shutdown path.  See docs/ESP32.md ("RMT step generation") for the
// integration design and residual-error accounting.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "freertos/FreeRTOS.h" // portENTER_CRITICAL_ISR
#include "driver/gpio.h" // gpio_config
#include "esp_attr.h" // IRAM_ATTR
#include "esp_intr_alloc.h" // esp_intr_alloc
#include "esp_private/periph_ctrl.h" // periph_module_enable
#include "esp_rom_gpio.h" // esp_rom_gpio_connect_out_signal
#include "soc/gpio_sig_map.h" // RMT_SIG_OUT0_IDX
#include "soc/interrupts.h" // ETS_RMT_INTR_SOURCE
#include "soc/rmt_struct.h" // RMT
#include "autoconf.h" // CONFIG_CLOCK_FREQ
#include "board/irq.h" // irq_save
#include "internal.h" // ESP32_GPIO_COUNT
#include "rmt_step.h" // rmt_step_setup

// RMT channel RAM (8 channels x 64 items); address from the IDF
// linker script (esp32.peripherals.ld: PROVIDE RMTMEM = 0x3ff56800)
extern volatile uint32_t RMTMEM[];

#define RMT_STEP_DIV (80000000 / CONFIG_CLOCK_FREQ) // APB -> klipper ticks
#define RMT_ITEMS 64      // one memory block per channel
#define RMT_REFILL 32     // items per threshold refill (half the ring)
#define RMT_MAX_HALF 32767u
#define MOVE_RING 16
// Wrap-underrun watermark: at a threshold event the read cursor
// should trail the write cursor by ~RMT_REFILL items (we keep one
// half ahead of the other).  If the lead has collapsed below this
// margin the refill arrived too late and the transmitter is about to
// re-read stale ring items - treat it as an underrun stop.  The exact
// value wants scope calibration on first silicon (see docs/ESP32.md).
#define RMT_WRAP_MARGIN 6

extern portMUX_TYPE klipper_mux; // irq.c

struct rmt_move {
    uint32_t interval;
    uint16_t count;
    int16_t add;
};

struct rmt_step_chan {
    uint8_t chan, in_use, running, done;
    uint8_t underrun;       // wrap-mode underrun latch (see ISR)
    uint16_t high_ticks;
    uint8_t wr;             // next ring write index
    // Current move being expanded into items
    uint32_t interval, low_carry;
    uint16_t count;
    int16_t add;
    // Pending moves
    struct rmt_move moves[MOVE_RING];
    uint8_t mhead, mtail;
};

static struct rmt_step_chan channels[8];
static intr_handle_t rmt_intr;

/****************************************************************
 * Item generation from (interval, count, add) triples
 ****************************************************************/

// Split a low period so no emitted half-duration is ever zero (a
// zero duration is the hardware's end-of-data marker)
static inline uint32_t
split_low(uint32_t low, uint32_t *carry)
{
    uint32_t d = low > RMT_MAX_HALF ? RMT_MAX_HALF : low;
    uint32_t c = low - d;
    if (c == 1) {
        d--;
        c = 2;
    }
    *carry = c;
    return d;
}

// Produce the next 32-bit RMT item; returns 0 when out of moves
static uint_fast8_t IRAM_ATTR
rmt_step_gen(struct rmt_step_chan *sc, uint32_t *item)
{
    if (sc->low_carry) {
        // Filler: both halves at level 0
        uint32_t carry = sc->low_carry, d0, d1;
        if (carry > 2 * RMT_MAX_HALF) {
            d0 = d1 = RMT_MAX_HALF;
            carry -= 2 * RMT_MAX_HALF;
            if (carry == 1) {
                d1--;
                carry = 2;
            }
        } else if (carry > RMT_MAX_HALF) {
            d0 = RMT_MAX_HALF;
            d1 = carry - RMT_MAX_HALF;
            carry = 0;
        } else {
            d0 = carry - 1;
            d1 = 1;
            carry = 0;
        }
        sc->low_carry = carry;
        *item = d0 | (d1 << 16);
        return 1;
    }
    if (!sc->count) {
        // Pull the next queued move
        if (sc->mhead == sc->mtail)
            return 0;
        struct rmt_move *m = &sc->moves[sc->mtail % MOVE_RING];
        sc->mtail++;
        sc->interval = m->interval;
        sc->count = m->count;
        sc->add = m->add;
        if (!sc->count)
            return 0;
    }
    // One step: high for high_ticks, low for the rest of the interval
    uint32_t interval = sc->interval;
    sc->interval += sc->add;
    sc->count--;
    uint32_t high = sc->high_ticks;
    uint32_t low = interval > high + 1 ? interval - high : 2;
    uint32_t d1 = split_low(low, &sc->low_carry);
    *item = (high | 0x8000) | (d1 << 16);
    return 1;
}

// Fill up to n ring slots; writes the end-of-data marker on underrun
static void IRAM_ATTR
rmt_step_fill(struct rmt_step_chan *sc, uint_fast8_t n)
{
    volatile uint32_t *mem = &RMTMEM[(uint32_t)sc->chan * RMT_ITEMS];
    while (n--) {
        uint32_t item;
        if (!rmt_step_gen(sc, &item)) {
            mem[sc->wr] = 0; // end marker -> tx_end interrupt
            sc->done = 1;
            return;
        }
        mem[sc->wr] = item;
        sc->wr = (sc->wr + 1) % RMT_ITEMS;
    }
}

/****************************************************************
 * Pure pulse-planning helpers (no hardware access - unit tested on
 * the host, see rmt_plan_test.c / the esp32 hostcheck harness)
 ****************************************************************/

// Ticks spanned by a whole (interval, count, add) move.  64-bit
// accumulation, truncated to the 32-bit klipper clock.
uint32_t
rmt_step_move_ticks(uint32_t interval, uint16_t count, int16_t add)
{
    uint64_t total = (uint64_t)count * interval;
    // add*count*(count-1)/2 ; count*(count-1) is always even
    int64_t ramp = (int64_t)add * ((int64_t)count * (count - 1) / 2);
    return (uint32_t)(total + (uint64_t)ramp);
}

// How many step edges have been emitted 'elapsed' ticks into a move.
// Edge m (0-based) is emitted at offset off(m) = m*interval +
// add*m*(m-1)/2 (monotonic while intervals stay positive).  Returns
// the count of edges with off(m) <= elapsed, clamped to [0, count].
uint16_t
rmt_step_move_emitted(uint32_t interval, uint16_t count, int16_t add
                      , uint32_t elapsed)
{
    // Monotonic linear scan bounded by a coarse estimate seed keeps
    // this exact for the ramp sign edge cases without 64-bit sqrt.
    // Callers only invoke it for the single in-flight move, whose
    // count is bounded, and typically near the end of the move.
    uint64_t off = 0;
    uint32_t m = 0;
    while (m < count) {
        // off(m) for the next edge
        uint64_t next = (uint64_t)m * interval
            + (int64_t)add * ((int64_t)m * (m - 1) / 2);
        if (next > elapsed)
            break;
        off = next;
        m++;
    }
    (void)off;
    return (uint16_t)m;
}

// Wrap-underrun predicate (see RMT_WRAP_MARGIN).  'wr' is the next
// ring slot we will write; 'rd' is the transmitter's current read
// slot.  In steady state the writer leads the reader by ~RMT_REFILL;
// a lead that has shrunk under the margin means the reader is about
// to overtake the writer and re-read not-yet-refreshed items.
int
rmt_step_wrap_hazard(uint8_t wr, uint8_t rd)
{
    uint8_t lead = (uint8_t)(wr - rd) % RMT_ITEMS;
    return lead < RMT_WRAP_MARGIN;
}

// Transmitter read cursor within this channel's 64-item block.  The
// low 10 bits of RMT.status_ch[] are the current RAM address; IDF
// v5.3.2 hal/esp32/include/hal/rmt_ll.h:481 decodes it as
// (status_ch[ch] & 0x3FF) - ch*64.  (TX read cursor and RX write
// cursor share this field on the classic ESP32 - TRM 13.4.)
static uint_fast8_t IRAM_ATTR
rmt_read_offset(uint8_t ch)
{
    return (uint_fast8_t)((RMT.status_ch[ch] & 0x3FF)
                          - (uint32_t)ch * RMT_ITEMS)
        % RMT_ITEMS;
}

/****************************************************************
 * Interrupt handling (runs on the core that called rmt_step_setup)
 ****************************************************************/

static void IRAM_ATTR
rmt_step_isr(void *arg)
{
    uint32_t st = RMT.int_st.val;
    portENTER_CRITICAL_ISR(&klipper_mux);
    for (uint_fast8_t ch = 0; ch < 8; ch++) {
        struct rmt_step_chan *sc = &channels[ch];
        uint32_t thr_bit = 1u << (24 + ch);
        uint32_t end_bit = 1u << (ch * 3);
        uint32_t err_bit = 1u << (ch * 3 + 2);
        if (st & thr_bit) {
            RMT.int_clr.val = thr_bit;
            if (sc->running && !sc->done) {
                // Watermark: has the reader closed on the writer?  If
                // the refill is this late the transmitter is about to
                // re-read stale items - latch an underrun and blank
                // the ring so it hits an end marker (controlled stop)
                // instead of emitting duplicated/garbage steps.
                if (rmt_step_wrap_hazard(sc->wr, rmt_read_offset(ch))) {
                    volatile uint32_t *mem =
                        &RMTMEM[(uint32_t)ch * RMT_ITEMS];
                    for (uint_fast8_t i = 0; i < RMT_ITEMS; i++)
                        mem[i] = 0;
                    sc->done = 1;
                    sc->underrun = 1;
                } else {
                    rmt_step_fill(sc, RMT_REFILL);
                }
            }
        }
        if (st & (end_bit | err_bit)) {
            RMT.int_clr.val = end_bit | err_bit;
            sc->running = 0;
            // Mask this channel's interrupts until the next start
            RMT.int_ena.val &= ~(thr_bit | end_bit | err_bit);
        }
    }
    portEXIT_CRITICAL_ISR(&klipper_mux);
}

/****************************************************************
 * Public interface
 ****************************************************************/

struct rmt_step_chan *
rmt_step_setup(uint8_t chan, uint32_t pin, uint8_t invert
               , uint16_t high_ticks)
{
    if (chan >= 8 || pin >= 34 || !high_ticks || high_ticks > RMT_MAX_HALF)
        return NULL;
    struct rmt_step_chan *sc = &channels[chan];
    if (sc->in_use)
        return NULL;

    static uint8_t global_init;
    if (!global_init) {
        global_init = 1;
        periph_module_enable(PERIPH_RMT_MODULE);
        RMT.apb_conf.fifo_mask = 1;      // direct RAM access
        RMT.apb_conf.mem_tx_wrap_en = 1; // ring-buffer wrap mode
        // The interrupt lands on the calling core; call from the
        // klipper task so refills share core 1 with timer dispatch
        if (esp_intr_alloc(ETS_RMT_INTR_SOURCE, ESP_INTR_FLAG_IRAM
                           , rmt_step_isr, NULL, &rmt_intr))
            return NULL;
    }

    sc->chan = chan;
    sc->high_ticks = high_ticks;

    typeof(RMT.conf_ch[0].conf0) c0 = { .val = 0 };
    c0.div_cnt = RMT_STEP_DIV; // RMT tick == klipper tick (20MHz)
    c0.mem_size = 1;
    c0.idle_thres = 0;
    RMT.conf_ch[chan].conf0.val = c0.val;

    typeof(RMT.conf_ch[0].conf1) c1 = { .val = 0 };
    c1.mem_owner = 0;      // transmitter owns the RAM
    c1.ref_always_on = 1;  // clock from APB (80MHz), not REF_TICK
    c1.idle_out_en = 1;
    // Inversion happens in the GPIO matrix below; the RMT-side idle
    // level stays 0 (so an inverted pin idles high)
    c1.idle_out_lv = 0;
    RMT.conf_ch[chan].conf1.val = c1.val;
    RMT.tx_lim_ch[chan].limit = RMT_REFILL;

    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    if (gpio_config(&config))
        return NULL;
    esp_rom_gpio_connect_out_signal(pin, RMT_SIG_OUT0_IDX + chan
                                    , !!invert, false);
    sc->in_use = 1;
    return sc;
}

int
rmt_step_queue(struct rmt_step_chan *sc, uint32_t interval
               , uint16_t count, int16_t add)
{
    irqstatus_t flag = irq_save();
    if ((uint8_t)(sc->mhead - sc->mtail) >= MOVE_RING) {
        irq_restore(flag);
        return -1;
    }
    struct rmt_move *m = &sc->moves[sc->mhead % MOVE_RING];
    m->interval = interval;
    m->count = count;
    m->add = add;
    sc->mhead++;
    irq_restore(flag);
    return 0;
}

uint_fast8_t
rmt_step_queue_space(struct rmt_step_chan *sc)
{
    irqstatus_t flag = irq_save();
    uint_fast8_t used = (uint8_t)(sc->mhead - sc->mtail);
    irq_restore(flag);
    return used >= MOVE_RING ? 0 : MOVE_RING - used;
}

int
rmt_step_start(struct rmt_step_chan *sc)
{
    irqstatus_t flag = irq_save();
    if (sc->running || sc->mhead == sc->mtail) {
        irq_restore(flag);
        return -1;
    }
    uint8_t ch = sc->chan;
    sc->wr = 0;
    sc->low_carry = 0;
    sc->count = 0;
    sc->done = 0;
    sc->underrun = 0;
    rmt_step_fill(sc, RMT_ITEMS); // prime the whole ring
    RMT.conf_ch[ch].conf1.mem_rd_rst = 1;
    RMT.conf_ch[ch].conf1.mem_rd_rst = 0;
    RMT.int_clr.val = (1u << (24 + ch)) | (7u << (ch * 3));
    RMT.int_ena.val |= (1u << (24 + ch)) | (1u << (ch * 3))
        | (1u << (ch * 3 + 2));
    sc->running = 1;
    RMT.conf_ch[ch].conf1.tx_start = 1;
    irq_restore(flag);
    return 0;
}

uint8_t
rmt_step_is_busy(struct rmt_step_chan *sc)
{
    return sc->running;
}

uint8_t
rmt_step_take_underrun(struct rmt_step_chan *sc)
{
    irqstatus_t flag = irq_save();
    uint8_t u = sc->underrun;
    sc->underrun = 0;
    irq_restore(flag);
    return u;
}

void
rmt_step_abort(struct rmt_step_chan *sc)
{
    irqstatus_t flag = irq_save();
    if (sc->running) {
        // The classic ESP32 has no tx_stop bit; blank the ring so
        // the reader hits an end marker within one item
        volatile uint32_t *mem = &RMTMEM[(uint32_t)sc->chan * RMT_ITEMS];
        for (uint_fast8_t i = 0; i < RMT_ITEMS; i++)
            mem[i] = 0;
        sc->done = 1;
    }
    sc->mhead = sc->mtail = 0;
    irq_restore(flag);
}
