#ifndef __ESP32_RMT_STEP_H
#define __ESP32_RMT_STEP_H
// Experimental hardware-timed step pulse trains via the ESP32 RMT
// peripheral - see src/esp32/rmt_step.c and docs/ESP32.md.

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

// Begin emitting the queued pulse train.  Transmission ends (and the
// channel returns to idle) when the queue underruns; moves for a
// continuous train must stay ahead of consumption.
int rmt_step_start(struct rmt_step_chan *sc);

// Nonzero while a pulse train is being emitted
uint8_t rmt_step_is_busy(struct rmt_step_chan *sc);

// Best-effort stop; the pulse completing at that instant may finish
void rmt_step_abort(struct rmt_step_chan *sc);

#endif // rmt_step.h
