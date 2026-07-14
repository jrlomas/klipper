#ifndef __TRAJQ_H
#define __TRAJQ_H

#include <stdint.h>
#include "autoconf.h" // CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
#include "basecmd.h" // struct move_queue_head

// Trajectory intention protocol (FD-0001): positions are in
// sub-units (1 native unit = 2^16 sub-units); velocity is Q16.16
// sub-units/tick; accel is sub-units/tick^2 with 32 fractional bits.
// The chained position accumulator carries the low 32 position bits with 32
// fractional bits. Integration is exact modulo 2^64; the stepper's separate
// physical microstep counter unwraps that phase for arbitrary travel.
#define TRAJ_SUBUNIT_SHIFT 16
#define TRAJ_MAX_DURATION (1 << 26)

struct traj_segment {
    struct move_node node;
    // Exact Q32.32 endpoint delta of the machine-time wire polynomial.
    // Execution coefficients below are converted to the local timer domain;
    // retaining this one value preserves host/MCU zero-drift chaining.
    int64_t wire_delta;
    uint32_t duration;
    int32_t velocity;
    int32_t accel;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    // Higher-order power-basis coefficients (FD-0001 doc 02
    // "Higher-order segments"). jerk is Q sub-units/tick^3 with 48
    // fractional bits, snap Q .tick^4 with 64, crackle Q .tick^5 with
    // 80 - each derivative adds one power of t and 16 fractional bits.
    int32_t jerk, snap, crackle;
#endif
    uint8_t flags;
    uint8_t kind;
};

enum {
    TSEG_HOLD_AT_END = 1 << 0,
    // Duration and derivatives have already been quantized in the executing
    // MCU's local timer domain. Absolute rebase clocks remain machine time.
    TSEG_LOCAL_TIME = 1 << 1,
    // bits 6-7 carry the segment polynomial order (00 = quadratic)
    TSEG_POLY_MASK = 3 << 6,
    TSEG_POLY_QUADRATIC = 0 << 6,
    TSEG_POLY_CUBIC = 1 << 6,
    TSEG_POLY_QUINTIC = 2 << 6,
};

enum {
    TSEGK_MOTION,
    // Internal queue barrier produced by trajectory_rebase.  duration holds
    // the absolute local clock, velocity the new sub-unit anchor, and accel
    // the backend-specific auxiliary anchor (physical step count for a
    // stepper).  It is never accepted from a queue_traj_segment wire command.
    TSEGK_REBASE,
};

struct trajq;

struct trajq_backend_ops {
    // Activate an idle actuator: the core has loaded the first
    // segment; the backend must derive its phase from tq->acc and
    // schedule execution from tq->seg_start_clock. Called irqs off.
    void (*start)(struct trajq *tq);
    // Halt motion immediately (trsync trigger or shutdown) and
    // record the live position back into tq->acc. Called irqs off.
    void (*stop)(struct trajq *tq);
    // Apply backend-specific state at a rebase boundary.  For steppers this
    // synchronizes the physical integer step counter; value outputs need no
    // auxiliary state and may leave this NULL. Called irqs off.
    void (*rebase)(struct trajq *tq, int32_t aux);
};

enum {
    TQF_NEED_REBASE   = 1 << 0, // no valid anchor - segments rejected
    TQF_UNDERRUN      = 1 << 1, // underrun latched until rebase
    TQF_ACTIVE        = 1 << 2, // backend executing
    TQF_RAMPING       = 1 << 3, // current segment is a synthesized ramp
    TQF_EVENT_PENDING = 1 << 4, // task must emit traj_underrun
};

struct trajq {
    struct move_queue_head mq;
    const struct trajq_backend_ops *ops;
    // Exact chained position at the START of the current segment
    // (modulo-2^64 Q32.32 sub-units); the authoritative wire phase.
    int64_t acc;
    int64_t wire_delta;
    // Local clock of the start of the current segment (when active)
    // or of the next segment to start (when idle).
    uint32_t seg_start_clock;
    // Local clock at which the queued stream ends.
    uint32_t horizon_clock;
    uint32_t underrun_decel; // magnitude, accel wire units
    // Active segment
    uint32_t duration;
    int32_t velocity;
    int32_t accel;
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
    int32_t jerk, snap, crackle;
#endif
    uint8_t seg_flags;
    uint8_t flags;
    uint8_t oid;
    uint16_t queued; // segments waiting in mq
    uint16_t dropped; // segments rejected while latched
    // Latched underrun event data
    uint32_t event_clock;
    int32_t event_pos;
};

static inline int64_t
trajq_acc_add(int64_t acc, int64_t delta)
{
    // Signed overflow is undefined in C, but the trajectory phase is
    // deliberately modulo 2^64 so crossing the signed position boundary is
    // ordinary motion, not overflow.
    return (int64_t)((uint64_t)acc + (uint64_t)delta);
}

static inline int64_t
trajq_q16_to_acc(int64_t position16)
{
    // Preserve the two's-complement bit pattern without left-shifting a
    // negative signed value (undefined behavior in C).
    return (int64_t)((uint64_t)position16 << 16);
}

enum { TQ_ADV_SEG, TQ_ADV_IDLE, TQ_ADV_REBASE };

void trajq_setup(struct trajq *tq, uint8_t oid
                 , const struct trajq_backend_ops *ops
                 , uint32_t underrun_decel);
void trajq_queue_segment(struct trajq *tq, uint8_t flags, uint32_t duration
                         , int32_t velocity, int32_t accel);
#if CONFIG_WANT_TRAJECTORY_HIGHER_ORDER
void trajq_queue_segment_ho(struct trajq *tq, uint8_t flags, uint32_t duration
                            , int32_t velocity, int32_t accel, int32_t jerk
                            , int32_t snap, int32_t crackle);
#endif
int trajq_rebase(struct trajq *tq, uint32_t clock, int32_t pos, int32_t aux);
int trajq_advance(struct trajq *tq);
void trajq_halt(struct trajq *tq, uint8_t set_flags);
int64_t trajq_end_delta(uint32_t duration, int32_t velocity, int32_t accel);
int32_t trajq_velocity_at(int32_t velocity, int32_t accel, uint32_t t);
int64_t trajq_pos_at(int32_t velocity, int32_t accel, uint32_t t);
// Coefficient-aware evaluation of the active segment. When higher-order
// support is compiled out these reduce exactly to the quadratic form.
int64_t trajq_pos_at_seg(struct trajq *tq, uint32_t t);
int32_t trajq_velocity_at_seg(struct trajq *tq, uint32_t t);
// Deadline-oriented crossing evaluators. Exact chained endpoints continue to
// use trajq_end_delta_seg().
int64_t trajq_pos_at_seg_fast(struct trajq *tq, uint32_t t);
int32_t trajq_velocity_at_seg_fast(struct trajq *tq, uint32_t t);
// Raw Horner numerators used by the quintic Newton solver.  They represent
// 120*position and 24*velocity, avoiding constant division in the timer IRQ.
int64_t trajq_pos120_at_seg_fast(struct trajq *tq, uint32_t t);
int64_t trajq_velocity24_at_seg_fast(struct trajq *tq, uint32_t t);
int64_t trajq_end_delta_seg(struct trajq *tq);
void trajq_note_underrun_wake(void);
int trajq_check_event_wake(void);

#endif // trajq.h
