// Machine-time authority and sync-beacon discipline (FD-0001 doc 01).
//
// Machine time is the primary MCU's free-running counter. This file
// implements both halves of the host-relayed beacon protocol:
//
//   host -> primary:    sync_beacon_read
//   primary -> host:    sync_beacon seq=%c clock=%u
//   host -> secondary:  sync_beacon_relay seq=%c machine_clock=%u
//                                         local_est=%u
//
// A secondary disciplines an (offset, rate) pair mapping machine time
// to its local clock. The rate is a Q8.24 fixed-point ratio (local
// ticks per machine tick). MCU timer frequencies are not necessarily
// equal (for example, a 64MHz secondary against a 12MHz primary), so
// the integer range must cover the nominal frequency ratio as well as
// crystal mismatch. Conversion is one 32x32->64 multiply plus
// shift per segment at ingest - never per step, never on the
// interrupt path.
//
// Offset errors are corrected by a slew-limited proportional-integral
// filter that biases the rate - the clock is never stepped while
// disciplined, as a step would corrupt in-flight segment schedules.
// Stepping only happens while priming (before Class-0 traffic is
// enabled) and when re-priming after the freewheel budget is blown
// (by which point ingest has been refused and any motion has taken
// its underrun ramp).
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND_FLAGS
#include "execlog.h" // execlog_append
#include "sched.h" // sched_shutdown
#include "timesync.h" // timesync_ticks_to_local
#include "timesync_math.h" // timesync_err_to_adj

// Q8.24 covers ratios [0, 256). Keep symmetric practical bounds around
// one while retaining sub-ppm resolution throughout the supported range.
#define RATE_MIN (RATE_ONE / 128)
#define RATE_MAX (RATE_ONE * 128U)
// Beacons blended before the filter leaves the priming phase
// (mirrors clocksync.py's 8-sample connect priming)
#define PRIME_TARGET 8
// Consecutive in-window beacons before convergence is declared
#define CONVERGE_COUNT 3

struct timesync_state {
    // Machine-time -> local-clock mapping (read at trajq ingest):
    //   local = local_ref + ((machine - machine_ref) * rate) >> 24
    uint32_t machine_ref, local_ref;
    uint32_t rate; // applied Q8.24 ratio (rate_base + slew bias)
    // Discipline filter state
    uint32_t rate_base; // PI integrator: clock ratio estimate, Q8.24
    uint32_t last_machine, last_local; // raw previous beacon sample
    uint32_t prime_machine, prime_local; // first sample of priming span
    uint32_t beacon_rx_local; // local clock at last beacon receipt
    uint32_t freewheel_ticks; // stale budget in local ticks (0=none)
    uint32_t converge_window; // |err| bound in local ticks (0=priming only)
    int32_t last_err; // last offset error, local ticks
    uint8_t flags, tx_seq, last_seq, prime_count, good_count;
};

enum {
    TS_ENABLED   = 1 << 0, // secondary role: mapping in effect
    TS_PRIMED    = 1 << 1, // at least one beacon received
    TS_CONVERGED = 1 << 2, // filter reports bounded offset error
};

static struct timesync_state timesync;

/****************************************************************
 * Machine-time conversions (called from command/task context)
 ****************************************************************/

uint32_t
timesync_ticks_to_local(uint32_t machine_ticks)
{
    struct timesync_state *ts = &timesync;
    if (!(ts->flags & TS_ENABLED))
        return machine_ticks;
    // Round to nearest: duration truncation would otherwise
    // accumulate up to a tick of drift per ingested segment.
    return ((uint64_t)machine_ticks * ts->rate + RATE_HALF) >> RATE_SHIFT;
}

int32_t
timesync_derivative_to_local(int32_t value, uint8_t order)
{
    struct timesync_state *ts = &timesync;
    if (!(ts->flags & TS_ENABLED) || !value)
        return value;
    // If local ticks l advance at r ticks per machine tick m, then
    // d^n q/dl^n = d^n q/dm^n / r^n. Apply the same rounded Q8.24
    // division once per derivative order. The practical rate bounds and
    // the signed wire range keep each intermediate in int64.
    uint32_t rate = ts->rate;
    int64_t scaled = value;
    while (order--) {
        int64_t numerator = scaled * RATE_ONE;
        numerator += numerator < 0 ? -(int64_t)(rate / 2) : rate / 2;
        scaled = numerator / rate;
        if (scaled > INT32_MAX || scaled < INT32_MIN)
            shutdown("traj segment overflow");
    }
    return (int32_t)scaled;
}

uint32_t
timesync_clock_to_local(uint32_t machine_clock)
{
    struct timesync_state *ts = &timesync;
    if (!(ts->flags & TS_ENABLED))
        return machine_clock;
    int32_t dm = machine_clock - ts->machine_ref;
    int64_t dl = ((int64_t)dm * ts->rate + RATE_HALF) >> RATE_SHIFT;
    return ts->local_ref + (uint32_t)dl;
}

int
timesync_class0_ok(void)
{
    struct timesync_state *ts = &timesync;
    if (!(ts->flags & TS_ENABLED))
        return 1;
    if (!(ts->flags & TS_CONVERGED))
        return 0;
    if (ts->freewheel_ticks
        && timer_read_time() - ts->beacon_rx_local > ts->freewheel_ticks)
        // Beacon loss beyond the freewheel budget: the mapping can
        // no longer be vouched for.
        return 0;
    return 1;
}

/****************************************************************
 * Primary role: beacon timestamping
 ****************************************************************/

void
command_sync_beacon_read(uint32_t *args)
{
    irq_disable();
    uint32_t clock = timer_read_time();
    irq_enable();
    sendf("sync_beacon seq=%c clock=%u", timesync.tx_seq++, clock);
}
DECL_COMMAND_FLAGS(command_sync_beacon_read, HF_IN_SHUTDOWN
                   , "sync_beacon_read");

/****************************************************************
 * Secondary role: discipline filter
 ****************************************************************/

static uint32_t
clamp_rate(int64_t rate)
{
    if (rate < RATE_MIN)
        return RATE_MIN;
    if (rate > RATE_MAX)
        return RATE_MAX;
    return rate;
}

// Atomically publish a new mapping (readers may someday be in irq)
static void
timesync_set_mapping(struct timesync_state *ts, uint32_t machine_ref
                     , uint32_t local_ref, uint32_t rate)
{
    irq_disable();
    ts->machine_ref = machine_ref;
    ts->local_ref = local_ref;
    ts->rate = rate;
    irq_enable();
}

void
command_sync_beacon_relay(uint32_t *args)
{
    struct timesync_state *ts = &timesync;
    uint8_t seq = args[0];
    uint32_t m = args[1], l = args[2];
    uint32_t now = timer_read_time();
    if (!(ts->flags & TS_ENABLED)) {
        // Receiving a relay marks this board a secondary
        ts->flags = TS_ENABLED;
        ts->rate = ts->rate_base = RATE_ONE;
    }
    if (ts->flags & TS_PRIMED) {
        if (seq == ts->last_seq)
            // Duplicate relay
            return;
        if (ts->freewheel_ticks
            && now - ts->beacon_rx_local > ts->freewheel_ticks) {
            // Stale beyond budget: ingest has been refused and any
            // motion has ramped out, so stepping is safe - re-prime.
            ts->flags &= ~(TS_PRIMED | TS_CONVERGED);
            ts->prime_count = ts->good_count = 0;
        }
    }
    ts->last_seq = seq;
    if (!(ts->flags & TS_PRIMED)) {
        // First beacon: step the mapping onto the sample
        timesync_set_mapping(ts, m, l, ts->rate_base);
        ts->prime_machine = m;
        ts->prime_local = l;
        ts->last_machine = m;
        ts->last_local = l;
        ts->beacon_rx_local = now;
        ts->prime_count = 1;
        ts->flags |= TS_PRIMED;
        return;
    }
    int32_t dm = m - ts->last_machine;
    if (dm <= 0)
        // Out-of-order or repeated machine timestamp
        return;
    if (ts->prime_count < PRIME_TARGET) {
        // Priming: measure over the full span since the first sample.
        // Per-interval estimates over the 50ms host burst magnify USB
        // timestamp jitter and can seed the discipline loop far from the
        // known nominal clock ratio.
        int32_t prime_dm = m - ts->prime_machine;
        uint32_t prime_dl = l - ts->prime_local;
        if (prime_dm > 0)
            ts->rate_base = clamp_rate(
                ((uint64_t)prime_dl << RATE_SHIFT) / prime_dm);
        // Class-0 is not yet enabled, so stepping onto each priming sample
        // is harmless.
        timesync_set_mapping(ts, m, l, ts->rate_base);
        ts->prime_count++;
    } else {
        // Disciplined: slew-limited PI filter. The offset error is
        // never stepped out - the mapping is re-anchored at its own
        // prediction (continuous) and the error is worked off by
        // biasing the rate over the following interval.
        uint32_t predicted = timesync_clock_to_local(m);
        int32_t err = l - predicted;
        int32_t adj = timesync_err_to_adj(err, dm);
        // Integral: absorb 1/32 of the implied rate error per beacon
        // (gains validated against the +-10us budget with 5us-sigma
        // beacon stamping noise and 70ppm crystal mismatch)
        ts->rate_base = clamp_rate(
            (int64_t)ts->rate_base + (adj >> 5));
        // Proportional: slew out a quarter of the offset per interval
        int32_t slew = adj >> 2;
        int32_t max_slew = ts->rate_base / 2000;
        if (slew > max_slew)
            slew = max_slew;
        else if (slew < -max_slew)
            slew = -max_slew;
        timesync_set_mapping(ts, m, predicted
                             , clamp_rate((int64_t)ts->rate_base + slew));
        ts->last_err = err;
        // Convergence: bounded offset error on consecutive beacons.
        // A zero window (host never sent timesync_setup) accepts any
        // post-priming beacon.
        uint32_t mag = err < 0 ? -(uint32_t)err : (uint32_t)err;
        if (!ts->converge_window || mag <= ts->converge_window) {
            if (ts->good_count < CONVERGE_COUNT)
                ts->good_count++;
            if (ts->good_count >= CONVERGE_COUNT)
                ts->flags |= TS_CONVERGED;
        } else {
            ts->good_count = 0;
            ts->flags &= ~TS_CONVERGED;
        }
        // Doc 08: discipline record in the execution log
        execlog_append(EL_DISCIPLINE, 0, now, err, ts->rate);
    }
    ts->last_machine = m;
    ts->last_local = l;
    ts->beacon_rx_local = now;
}
DECL_COMMAND_FLAGS(command_sync_beacon_relay, HF_IN_SHUTDOWN
                   , "sync_beacon_relay seq=%c machine_clock=%u local_est=%u");

void
command_timesync_setup(uint32_t *args)
{
    struct timesync_state *ts = &timesync;
    // Setup establishes a new host/MCU epoch. Never retain convergence or
    // mapping anchors from an earlier Klipper configuration.
    ts->flags = TS_ENABLED;
    ts->rate = ts->rate_base = clamp_rate(args[2]);
    ts->machine_ref = ts->local_ref = 0;
    ts->last_machine = ts->last_local = 0;
    ts->prime_machine = ts->prime_local = 0;
    ts->beacon_rx_local = 0;
    ts->last_err = 0;
    ts->last_seq = ts->prime_count = ts->good_count = 0;
    ts->freewheel_ticks = args[0];
    ts->converge_window = args[1];
}
DECL_COMMAND(command_timesync_setup,
             "timesync_setup freewheel_ticks=%u converge_window=%u"
             " nominal_rate=%u");

void
command_timesync_query(uint32_t *args)
{
    struct timesync_state *ts = &timesync;
    sendf("timesync_state flags=%c prime_count=%c rate=%u last_err=%i"
          " machine_ref=%u local_ref=%u"
          , ts->flags, ts->prime_count, ts->rate, ts->last_err
          , ts->machine_ref, ts->local_ref);
}
DECL_COMMAND_FLAGS(command_timesync_query, HF_IN_SHUTDOWN, "timesync_query");
