#ifndef __ESP32_RMT_STEP_H
#define __ESP32_RMT_STEP_H
// Hardware-timed step pulse trains via the ESP32 RMT peripheral - see
// src/esp32/rmt_step.c and docs/ESP32.md.  Backed step generation is
// wired up in src/esp32/rmt_stepper.c (CONFIG_WANT_ESP32_RMT_STEP).

#include <stdint.h>

struct rmt_step_chan;

// Bind RMT channel 'chan' (0..7) to 'pin'.  high_ticks is the step
// pulse width in klipper clock ticks (CONFIG_CLOCK_FREQ); invert
// selects an active-low step signal.  Returns NULL on error.
struct rmt_step_chan *rmt_step_setup(uint8_t chan, uint32_t pin
                                     , uint8_t invert, uint16_t high_ticks);

// Queue a klipper-style move: 'count' steps, the first after
// 'interval' klipper ticks, each subsequent interval increasing by
// 'add'.  Returns 0 on success, -1 if the move queue is full.
int rmt_step_queue(struct rmt_step_chan *sc, uint32_t interval
                   , uint16_t count, int16_t add);

// Free move slots currently available in the channel queue.
uint_fast8_t rmt_step_queue_space(struct rmt_step_chan *sc);

// Begin emitting the queued pulse train.  Transmission ends (and the
// channel returns to idle) when the queue underruns; moves for a
// continuous train must stay ahead of consumption.
int rmt_step_start(struct rmt_step_chan *sc);

// Nonzero while a pulse train is being emitted
uint8_t rmt_step_is_busy(struct rmt_step_chan *sc);

// Best-effort stop; the pulse completing at that instant may finish
void rmt_step_abort(struct rmt_step_chan *sc);

// Read and clear the "wrap-mode underrun" latch: set by the refill
// ISR when the transmitter's read cursor caught the write cursor with
// data still owed (a late refill that would otherwise re-emit stale
// ring items - the silent-bad-motion hazard of wrap mode).  The
// channel is force-stopped when this trips.
uint8_t rmt_step_take_underrun(struct rmt_step_chan *sc);

/****************************************************************
 * Pure pulse-planning helpers (no hardware; unit-tested on host -
 * see the esp32 hostcheck harness / rmt_plan_test.c)
 ****************************************************************/

// Total klipper ticks spanned by a (interval, count, add) move:
//   sum_{k=0..count-1} (interval + k*add)
//   = count*interval + add*count*(count-1)/2
// Wraps to 32 bits, matching the klipper clock; used to chain the
// absolute start time of successive RMT trains (train N+1 starts when
// train N's last step completes) for clock anchoring.
uint32_t rmt_step_move_ticks(uint32_t interval, uint16_t count, int16_t add);

// Number of step edges of a (interval, count, add) move that have been
// emitted by 'elapsed' ticks into the move (RMT emits step j's rising
// edge at offset sum_{i<j} (interval + (i-1)*add), first edge at
// offset 0).  Clamped to [0, count].  Used to freeze an exact stopped
// position for homing/trsync without a pulse counter.
uint16_t rmt_step_move_emitted(uint32_t interval, uint16_t count
                               , int16_t add, uint32_t elapsed);

// Wrap-underrun watermark predicate: given the ring write cursor 'wr'
// and the hardware read cursor 'rd' (both 0..63) sampled at a refill
// (threshold) event, return nonzero when the reader has closed to
// within the safety margin of the writer - i.e. a late refill that is
// about to let the transmitter re-read not-yet-refreshed items.
int rmt_step_wrap_hazard(uint8_t wr, uint8_t rd);

#endif // rmt_step.h
