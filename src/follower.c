// OpenAMS inline follower — autonomous pressure-following stepper control
//
// Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.
//
// Implements FOLLOWER_PROTOCOL.md (klipper_openams): a stepper on a spare
// port, closed-loop controlled HERE in the MCU — never by the motion queue.
// The host streams the FPS buffer-pressure value (slow trim) and
// feed-forward velocity segments sampled ahead of real time from the motion
// queue; a 1 kHz control tick combines them:
//     v = slew(clamp(ff(t) + PID(fps_target - fps), +-max_v), accel)
// A self-rescheduling step timer turns v into step pulses. Load/unload ops
// run on the two filament switches (PRE = spool staged, POST = fed through)
// with step-count budgets. All op completions carry the host's echoed `gen`
// and every op emits EXACTLY ONE terminal status. Watchdogs stop the motor
// if the host's FPS stream goes stale (host death) and time ops out on
// no-progress; a shutdown handler de-energizes the motor.
//
// Everything on the wire is an integer: distances in steps, velocities in
// steps/s, FPS on a 0..65535 scale, PID gains in Q12 (steps/s of trim per
// count of FPS error; ki per second, kd seconds).

#include <string.h> // memset
#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_setup
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_TASK

DECL_CONSTANT("FOLLOWER_PROTOCOL_VERSION", 1);

// Op result codes (same values as the OAMS mainboard protocol)
DECL_CONSTANT("FOLLOWER_OP_CODE_SUCCESS", 0);
DECL_CONSTANT("FOLLOWER_OP_CODE_ERROR_UNSPECIFIED", 1);
DECL_CONSTANT("FOLLOWER_OP_CODE_ERROR_BUSY", 2);
DECL_CONSTANT("FOLLOWER_OP_CODE_SPOOL_ALREADY_IN_BAY", 3);
DECL_CONSTANT("FOLLOWER_OP_CODE_NO_SPOOL_IN_BAY", 4);
DECL_CONSTANT("FOLLOWER_OP_CODE_ERROR_KLIPPER_CALL", 5);
DECL_CONSTANT("FOLLOWER_OP_CODE_CANCEL_LOAD_SPOOL", 6);
DECL_CONSTANT("FOLLOWER_OP_CODE_TIMEOUT", 7);
enum {
    OP_CODE_SUCCESS = 0, OP_CODE_ERROR_UNSPECIFIED = 1, OP_CODE_BUSY = 2,
    OP_CODE_ALREADY_IN_BAY = 3, OP_CODE_NO_SPOOL = 4,
    OP_CODE_CANCEL = 6, OP_CODE_TIMEOUT = 7,
};

// Action ids for follower_action_status (same values as OAMS_STATUS_*)
DECL_CONSTANT("FOLLOWER_STATUS_LOADING", 0);
DECL_CONSTANT("FOLLOWER_STATUS_UNLOADING", 1);
DECL_CONSTANT("FOLLOWER_STATUS_FORWARD_FOLLOWING", 2);
DECL_CONSTANT("FOLLOWER_STATUS_REVERSE_FOLLOWING", 3);
DECL_CONSTANT("FOLLOWER_STATUS_COASTING", 4);
DECL_CONSTANT("FOLLOWER_STATUS_STOPPED", 5);
DECL_CONSTANT("FOLLOWER_STATUS_CALIBRATING", 6);
DECL_CONSTANT("FOLLOWER_STATUS_ERROR", 7);
enum { STATUS_LOADING = 0, STATUS_UNLOADING = 1, STATUS_ERROR = 7 };

DECL_CONSTANT("FOLLOWER_REVERSE", 0);
DECL_CONSTANT("FOLLOWER_FORWARD", 1);
enum { DIR_REVERSE = 0, DIR_FORWARD = 1 };

// telemetry flag bits
enum {
    TF_FOLLOWING = 1 << 0, TF_DIRECTION = 1 << 1, TF_OP_IN_FLIGHT = 1 << 2,
    TF_FPS_STALE = 1 << 3, TF_FF_UNDERRUN = 1 << 4, TF_ERROR_LATCHED = 1 << 5,
};

// op state machine
enum {
    OP_NONE = 0,
    OP_LOAD_TO_POST,     // feed until POST makes (budget: switch_travel)
    OP_LOAD_TO_FPS,      // feed until FPS >= fps_upper (budget: 1.2*path)
    OP_UNLOAD_TO_CLEAR,  // reverse until POST clears (budget: 1.2*path)
    OP_UNLOAD_PARK,      // reverse park_extra more, then done
};

// config flag bits (config_follower flags=%c)
enum { CF_INVERT_STEP = 1, CF_INVERT_DIR = 2, CF_INVERT_ENABLE = 4 };

#define CONTROL_HZ 1000
// Step pulse width and the resulting step-rate budget. 2us high time plus
// the interval math floor gives a hard MAX_STEP_RATE the config command
// enforces, so an impossible max_v is a config error, not a runtime stall.
#define STEP_PULSE_US 2
#define MAX_STEP_RATE 100000
// FPS staleness escalation: op aborts after this many ms of staleness.
#define FPS_OP_ABORT_MS 5000
// Load slows for the final segment of the path (press into the gears).
#define LOAD_SLOW_ZONE_STEPS(f) ((f)->path_steps / 8)

#define FF_RING_SIZE 64          // power of two
#define STATUS_RING_SIZE 4       // power of two

struct ff_seg {
    uint32_t clock;
    int32_t velocity;            // steps/s signed
};

struct pending_status {
    uint8_t action, code, gen;
    uint32_t value;
};

struct follower {
    struct timer step_timer, control_timer;
    struct gpio_out step_pin, dir_pin, enable_pin;
    struct gpio_in pre_pin, post_pin;
    uint32_t ticks_per_sec, control_ticks, pulse_ticks;
    // --- configuration (steps / steps/s / Q12 / ms) ---
    uint32_t max_v, accel_per_tick, load_v, unload_v;
    uint32_t path_steps, switch_travel_steps, park_extra_steps;
    uint32_t fps_stale_ms, telemetry_ms;
    uint16_t kp, ki, kd;
    uint16_t fps_target, fps_lower, fps_upper;
    uint8_t fps_reversed, debounce_ms;
    uint8_t invert_enable, have_switches, have_tuning;
    // --- switch debounce ---
    uint8_t pre_state, post_state, pre_raw, post_raw;
    uint8_t pre_count, post_count;
    uint8_t pre_invert, post_invert;
    // --- control state ---
    int32_t v_cmd, v_target;     // steps/s signed
    int32_t step_count;
    int32_t pid_integ;           // count*ms, clamped
    int16_t pid_prev_err;
    uint16_t fps_value;
    uint32_t fps_age_ms;
    uint8_t following, direction, motor_on;
    // --- feed-forward ring ---
    struct ff_seg ff[FF_RING_SIZE];
    uint8_t ff_head, ff_tail;
    int32_t ff_current;
    uint8_t ff_underrun, ff_seen;
    // --- op state machine ---
    uint8_t op, op_gen, op_cancel;
    int32_t op_origin;           // step_count at phase start
    uint32_t op_stale_ms;
    // --- reporting ---
    struct pending_status status[STATUS_RING_SIZE];
    uint8_t status_head, status_tail;
    uint32_t telemetry_countdown_ms;
    uint8_t telemetry_due, error_latched;
    // --- step generator ---
    uint8_t step_phase;          // 1 = pulse high, awaiting unstep
    uint8_t dir_state;           // last written dir level (logical forward=1)
    uint8_t oid;
};

static struct task_wake follower_wake;

/****************************************************************
 * Helpers
 ****************************************************************/

static void
follower_motor_enable(struct follower *f, uint8_t on)
{
    // enable_pin invert is handled by gpio_out_setup polarity: we store the
    // logical level; gpio_out_write takes the physical level.
    gpio_out_write(f->enable_pin, f->invert_enable ? !on : on);
    f->motor_on = on;
}

static void
follower_hard_stop(struct follower *f)
{
    // Called with irqs disabled (or from shutdown). Motor to zero NOW.
    f->v_cmd = f->v_target = 0;
    f->following = 0;
    f->ff_head = f->ff_tail = 0;
    f->ff_current = 0;
    f->ff_seen = 0;
}

static void
follower_push_status(struct follower *f, uint8_t action, uint8_t code,
                     uint32_t value, uint8_t gen)
{
    // Callers hold irq_disable (or run pre-sched). Drop-oldest on overflow
    // is impossible in practice (ring 4, one op in flight); guard anyway.
    uint8_t next = (f->status_head + 1) % STATUS_RING_SIZE;
    if (next == f->status_tail)
        f->status_tail = (f->status_tail + 1) % STATUS_RING_SIZE;
    struct pending_status *ps = &f->status[f->status_head];
    ps->action = action;
    ps->code = code;
    ps->value = value;
    ps->gen = gen;
    f->status_head = next;
    sched_wake_task(&follower_wake);
}

static void
follower_op_finish(struct follower *f, uint8_t code)
{
    // irqs disabled. Exactly one terminal status per op.
    uint8_t action = (f->op == OP_LOAD_TO_POST || f->op == OP_LOAD_TO_FPS)
        ? STATUS_LOADING : STATUS_UNLOADING;
    int32_t moved = f->step_count - f->op_origin;
    if (moved < 0)
        moved = -moved;
    follower_push_status(f, action, code, moved, f->op_gen);
    f->op = OP_NONE;
    f->op_cancel = 0;
    f->v_target = 0;
    if (action == STATUS_LOADING && code == OP_CODE_SUCCESS) {
        // Contract: auto-start forward following after a successful load.
        f->following = 1;
        f->direction = DIR_FORWARD;
        f->fps_age_ms = 0;       // grace period for the trim loop
    } else {
        f->v_cmd = 0;            // failure/cancel/unload-done: stop now
    }
}

/****************************************************************
 * Step generation (self-rescheduling timer, velocity driven)
 ****************************************************************/

static uint_fast8_t
follower_step_event(struct timer *t)
{
    struct follower *f = container_of(t, struct follower, step_timer);
    if (f->step_phase) {
        // Unstep half of the pulse
        gpio_out_toggle_noirq(f->step_pin);
        f->step_phase = 0;
        f->step_timer.waketime += f->pulse_ticks;
        return SF_RESCHEDULE;
    }
    int32_t v = f->v_cmd;
    if (!v) {
        // Idle: poll for a new velocity at the control cadence. Keeping the
        // timer alive avoids re-arming a timer from another timer's context.
        f->step_timer.waketime += f->control_ticks;
        return SF_RESCHEDULE;
    }
    uint8_t fwd = v > 0;
    uint32_t mag = fwd ? v : -v;
    if (fwd != f->dir_state) {
        gpio_out_write(f->dir_pin, fwd);
        f->dir_state = fwd;
        // Direction setup time: skip one interval before stepping.
        f->step_timer.waketime += f->pulse_ticks;
        return SF_RESCHEDULE;
    }
    // Step pulse leading edge
    gpio_out_toggle_noirq(f->step_pin);
    f->step_phase = 1;
    f->step_count += fwd ? 1 : -1;
    uint32_t interval = f->ticks_per_sec / mag;
    if (interval < 2 * f->pulse_ticks)
        interval = 2 * f->pulse_ticks;
    f->step_timer.waketime += interval - f->pulse_ticks;
    return SF_RESCHEDULE;
}

/****************************************************************
 * Control tick (1 kHz): debounce, watchdogs, ff playback, PID, ops
 ****************************************************************/

static void
follower_debounce(struct follower *f)
{
    uint8_t pre = !!gpio_in_read(f->pre_pin) ^ f->pre_invert;
    uint8_t post = !!gpio_in_read(f->post_pin) ^ f->post_invert;
    if (pre != f->pre_state) {
        if (++f->pre_count >= f->debounce_ms) {
            f->pre_state = pre;
            f->pre_count = 0;
        }
    } else {
        f->pre_count = 0;
    }
    if (post != f->post_state) {
        if (++f->post_count >= f->debounce_ms) {
            f->post_state = post;
            f->post_count = 0;
        }
    } else {
        f->post_count = 0;
    }
}

static int32_t
follower_ff_playback(struct follower *f, uint32_t now)
{
    // Advance to the newest segment whose start clock has passed.
    uint8_t advanced = 0;
    while (f->ff_tail != f->ff_head) {
        struct ff_seg *seg = &f->ff[f->ff_tail];
        if (timer_is_before(now, seg->clock))
            break;
        f->ff_current = seg->velocity;
        f->ff_tail = (f->ff_tail + 1) % FF_RING_SIZE;
        f->ff_seen = 1;
        advanced = 1;
    }
    if (f->ff_tail == f->ff_head && !advanced && f->ff_seen) {
        // Ring drained and nothing new: the host stream underran. Hold ff
        // at 0 and continue on FPS trim alone (degraded, not fatal).
        if (f->ff_current) {
            f->ff_current = 0;
            f->ff_underrun = 1;
        }
    }
    return f->ff_current;
}

static int32_t
follower_pid(struct follower *f)
{
    int32_t err = (int32_t)f->fps_target - (int32_t)f->fps_value;
    // Integrator in count*ms; clamp so ki*integ can't wind up past max_v.
    f->pid_integ += err;
    int32_t integ_max = 65535 * 1000;
    if (f->pid_integ > integ_max)
        f->pid_integ = integ_max;
    else if (f->pid_integ < -integ_max)
        f->pid_integ = -integ_max;
    int32_t deriv = err - f->pid_prev_err;      // per ms
    f->pid_prev_err = err;
    // Q12 gains: steps/s per count (kp), per count-second (ki, so /1000 for
    // the ms integrator), per count/second (kd, so *1000 for the ms slope).
    int64_t out = ((int64_t)(int32_t)f->kp * err)
        + ((int64_t)(int32_t)f->ki * f->pid_integ) / 1000
        + ((int64_t)(int32_t)f->kd * deriv) * 1000;
    return (int32_t)(out >> 12);
}

static void
follower_run_op(struct follower *f)
{
    int32_t moved = f->step_count - f->op_origin;
    if (moved < 0)
        moved = -moved;
    switch (f->op) {
    case OP_LOAD_TO_POST:
        if (f->op_cancel) {
            follower_op_finish(f, OP_CODE_CANCEL);
            return;
        }
        if (f->post_state) {
            f->op = OP_LOAD_TO_FPS;
            f->op_origin = f->step_count;
            return;
        }
        if ((uint32_t)moved > f->switch_travel_steps) {
            follower_op_finish(f, OP_CODE_TIMEOUT);
            return;
        }
        f->v_target = f->load_v;
        break;
    case OP_LOAD_TO_FPS:
        if (f->op_cancel) {
            follower_op_finish(f, OP_CODE_CANCEL);
            return;
        }
        if (f->fps_value >= f->fps_upper) {
            follower_op_finish(f, OP_CODE_SUCCESS);
            return;
        }
        if ((uint32_t)moved > f->path_steps + f->path_steps / 5) {
            follower_op_finish(f, OP_CODE_TIMEOUT);
            return;
        }
        // Slow down near the end of the path to press gently into the gears
        if (f->path_steps && (uint32_t)moved
                > f->path_steps - LOAD_SLOW_ZONE_STEPS(f))
            f->v_target = f->load_v / 4 ? f->load_v / 4 : 1;
        else
            f->v_target = f->load_v;
        break;
    case OP_UNLOAD_TO_CLEAR:
        if (!f->post_state) {
            f->op = OP_UNLOAD_PARK;
            f->op_origin = f->step_count;
            return;
        }
        if ((uint32_t)moved > f->path_steps + f->path_steps / 5) {
            follower_op_finish(f, OP_CODE_TIMEOUT);
            return;
        }
        f->v_target = -(int32_t)f->unload_v;
        break;
    case OP_UNLOAD_PARK:
        if ((uint32_t)moved >= f->park_extra_steps) {
            follower_op_finish(f, OP_CODE_SUCCESS);
            return;
        }
        f->v_target = -(int32_t)f->unload_v;
        break;
    }
}

static uint_fast8_t
follower_control_event(struct timer *t)
{
    struct follower *f = container_of(t, struct follower, control_timer);
    uint32_t now = f->control_timer.waketime;
    f->control_timer.waketime += f->control_ticks;

    if (f->have_switches)
        follower_debounce(f);

    uint8_t active = f->following || f->op != OP_NONE;

    // FPS staleness watchdog (the host-death protection): only armed while
    // the motor has a reason to move.
    if (active) {
        if (f->fps_age_ms < 0xFFFFFF)
            f->fps_age_ms++;
    } else {
        f->fps_age_ms = 0;
    }
    uint8_t fps_stale = active && f->fps_age_ms > f->fps_stale_ms;

    if (f->op != OP_NONE) {
        if (fps_stale) {
            f->op_stale_ms++;
            f->v_target = 0;
            if (f->op_stale_ms > FPS_OP_ABORT_MS)
                follower_op_finish(f, OP_CODE_TIMEOUT);
        } else {
            f->op_stale_ms = 0;
            follower_run_op(f);
        }
    } else if (f->following) {
        if (fps_stale) {
            f->v_target = 0;
        } else if (f->direction == DIR_FORWARD) {
            int32_t ff = follower_ff_playback(f, now);
            f->v_target = ff + follower_pid(f);
        } else {
            // Reverse follow (unload assist): back out while the buffer
            // still shows pressure, stop once it drops below fps_lower.
            f->v_target = f->fps_value > f->fps_lower
                ? -(int32_t)f->unload_v : 0;
        }
    } else {
        f->v_target = 0;
    }

    // Clamp and slew v_cmd toward v_target
    int32_t vt = f->v_target;
    int32_t maxv = f->max_v;
    if (vt > maxv)
        vt = maxv;
    else if (vt < -maxv)
        vt = -maxv;
    int32_t v = f->v_cmd;
    int32_t step = f->accel_per_tick;
    if (vt > v)
        v = (vt - v > step) ? v + step : vt;
    else if (vt < v)
        v = (v - vt > step) ? v - step : vt;
    f->v_cmd = v;

    // Telemetry cadence
    if (f->telemetry_countdown_ms == 0) {
        f->telemetry_countdown_ms = f->telemetry_ms;
        f->telemetry_due = 1;
        sched_wake_task(&follower_wake);
    }
    f->telemetry_countdown_ms--;

    return SF_RESCHEDULE;
}

/****************************************************************
 * Config commands
 ****************************************************************/

void
command_config_follower(uint32_t *args)
{
    struct follower *f = oid_alloc(args[0], command_config_follower,
                                   sizeof(*f));
    f->oid = args[0];
    uint8_t flags = args[4];
    f->step_pin = gpio_out_setup(args[1], flags & CF_INVERT_STEP ? 1 : 0);
    f->dir_pin = gpio_out_setup(args[2], flags & CF_INVERT_DIR ? 1 : 0);
    f->invert_enable = !!(flags & CF_INVERT_ENABLE);
    // Motor de-energized until first commanded motion
    f->enable_pin = gpio_out_setup(args[3], f->invert_enable ? 1 : 0);
    f->ticks_per_sec = timer_from_us(1000000);
    f->control_ticks = timer_from_us(1000000 / CONTROL_HZ);
    f->pulse_ticks = timer_from_us(STEP_PULSE_US);
    if (!f->pulse_ticks)
        f->pulse_ticks = 1;
    // Safe defaults until the tuning/limit commands arrive
    f->max_v = 1;
    f->accel_per_tick = 1;
    f->fps_stale_ms = 500;
    f->telemetry_ms = 500;
    f->telemetry_countdown_ms = 500;
    f->debounce_ms = 5;
    f->step_timer.func = follower_step_event;
    f->control_timer.func = follower_control_event;
    irq_disable();
    f->step_timer.waketime = timer_read_time() + f->control_ticks;
    f->control_timer.waketime = f->step_timer.waketime + f->pulse_ticks;
    sched_add_timer(&f->step_timer);
    sched_add_timer(&f->control_timer);
    irq_enable();
}
DECL_COMMAND(command_config_follower,
             "config_follower oid=%c step_pin=%u dir_pin=%u enable_pin=%u"
             " flags=%c");

void
command_config_follower_switches(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    f->pre_pin = gpio_in_setup(args[1], args[2] ? 1 : 0);
    f->pre_invert = !!args[3];
    f->post_pin = gpio_in_setup(args[4], args[5] ? 1 : 0);
    f->post_invert = !!args[6];
    uint32_t debounce = args[7];
    f->debounce_ms = debounce && debounce < 255 ? debounce : 5;
    f->have_switches = 1;
}
DECL_COMMAND(command_config_follower_switches,
             "config_follower_switches oid=%c pre_pin=%u pre_pullup=%c"
             " pre_invert=%c post_pin=%u post_pullup=%c post_invert=%c"
             " debounce_ms=%u");

void
command_config_follower_tuning(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    f->kp = args[1];
    f->ki = args[2];
    f->kd = args[3];
    f->fps_target = args[4];
    f->fps_lower = args[5];
    f->fps_upper = args[6];
    f->fps_reversed = !!args[7];
    f->have_tuning = 1;
}
DECL_COMMAND(command_config_follower_tuning,
             "config_follower_tuning oid=%c kp=%u ki=%u kd=%u fps_target=%u"
             " fps_lower=%u fps_upper=%u fps_reversed=%c");

void
command_config_follower_limits(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    uint32_t max_v = args[1], accel = args[2];
    if (!max_v || max_v > MAX_STEP_RATE)
        // Config error, not a runtime surprise (contract requirement).
        shutdown("follower max_v exceeds step generation budget");
    if (args[3] > max_v || args[4] > max_v)
        shutdown("follower load/unload speed exceeds max_v");
    f->max_v = max_v;
    f->accel_per_tick = accel / CONTROL_HZ;
    if (!f->accel_per_tick)
        f->accel_per_tick = 1;
    f->load_v = args[3];
    f->unload_v = args[4];
}
DECL_COMMAND(command_config_follower_limits,
             "config_follower_limits oid=%c max_v=%u accel=%u load_v=%u"
             " unload_v=%u");

void
command_config_follower_geometry(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    f->path_steps = args[1];
    f->switch_travel_steps = args[2];
    f->park_extra_steps = args[3];
}
DECL_COMMAND(command_config_follower_geometry,
             "config_follower_geometry oid=%c path_steps=%u"
             " switch_travel_steps=%u park_extra_steps=%u");

void
command_config_follower_watchdog(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    f->fps_stale_ms = args[1] ? args[1] : 500;
    f->telemetry_ms = args[2] ? args[2] : 500;
}
DECL_COMMAND(command_config_follower_watchdog,
             "config_follower_watchdog oid=%c fps_stale_ms=%u telemetry_ms=%u");

/****************************************************************
 * Runtime commands
 ****************************************************************/

void
command_follower_cmd_load(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    uint8_t gen = args[1];
    irq_disable();
    if (f->op != OP_NONE) {
        // Rejection of the NEW op: carries the rejected op's gen.
        follower_push_status(f, STATUS_LOADING, OP_CODE_BUSY, 0, gen);
        irq_enable();
        return;
    }
    if (f->have_switches && f->post_state) {
        follower_push_status(f, STATUS_LOADING, OP_CODE_ALREADY_IN_BAY, 0,
                             gen);
        irq_enable();
        return;
    }
    if (f->have_switches && !f->pre_state) {
        follower_push_status(f, STATUS_LOADING, OP_CODE_NO_SPOOL, 0, gen);
        irq_enable();
        return;
    }
    f->op = OP_LOAD_TO_POST;
    f->op_gen = gen;
    f->op_cancel = 0;
    f->op_origin = f->step_count;
    f->op_stale_ms = 0;
    f->fps_age_ms = 0;
    follower_motor_enable(f, 1);
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_load, "follower_cmd_load oid=%c gen=%c");

void
command_follower_cmd_unload(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    uint8_t gen = args[1];
    irq_disable();
    if (f->op != OP_NONE) {
        follower_push_status(f, STATUS_UNLOADING, OP_CODE_BUSY, 0, gen);
        irq_enable();
        return;
    }
    if (f->have_switches && !f->post_state) {
        follower_push_status(f, STATUS_UNLOADING, OP_CODE_NO_SPOOL, 0, gen);
        irq_enable();
        return;
    }
    f->following = 0;
    f->op = OP_UNLOAD_TO_CLEAR;
    f->op_gen = gen;
    f->op_cancel = 0;
    f->op_origin = f->step_count;
    f->op_stale_ms = 0;
    f->fps_age_ms = 0;
    follower_motor_enable(f, 1);
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_unload, "follower_cmd_unload oid=%c gen=%c");

void
command_follower_cmd_load_cancel(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    irq_disable();
    if (f->op == OP_LOAD_TO_POST || f->op == OP_LOAD_TO_FPS)
        f->op_cancel = 1;      // silent no-op otherwise (contract)
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_load_cancel,
             "follower_cmd_load_cancel oid=%c");

void
command_follower_cmd_set(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    uint8_t enable = args[1], direction = args[2];
    irq_disable();
    if (f->op == OP_NONE) {
        if (enable) {
            f->following = 1;
            f->direction = direction ? DIR_FORWARD : DIR_REVERSE;
            f->fps_age_ms = 0;
            f->pid_integ = 0;
            f->pid_prev_err = 0;
            follower_motor_enable(f, 1);
        } else {
            follower_hard_stop(f);
        }
    } else if (!enable) {
        // An explicit stop always wins; the in-flight op is cancelled the
        // hard way and still gets its one terminal status.
        follower_op_finish(f, OP_CODE_CANCEL);
        follower_hard_stop(f);
    }
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_set,
             "follower_cmd_set oid=%c enable=%c direction=%c");

void
command_follower_cmd_fps(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    uint16_t value = args[1] > 0xFFFF ? 0xFFFF : args[1];
    if (f->fps_reversed)
        value = 0xFFFF - value;
    irq_disable();
    f->fps_value = value;
    f->fps_age_ms = 0;
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_fps, "follower_cmd_fps oid=%c value=%u");

void
command_follower_cmd_ff(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    irq_disable();
    uint8_t next = (f->ff_head + 1) % FF_RING_SIZE;
    if (next == f->ff_tail)
        // Full: drop the oldest so the freshest horizon wins.
        f->ff_tail = (f->ff_tail + 1) % FF_RING_SIZE;
    f->ff[f->ff_head].clock = args[1];
    f->ff[f->ff_head].velocity = args[2];
    f->ff_head = next;
    f->ff_underrun = 0;
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_ff,
             "follower_cmd_ff oid=%c clock=%u velocity=%i");

void
command_follower_cmd_clear_errors(uint32_t *args)
{
    struct follower *f = oid_lookup(args[0], command_config_follower);
    irq_disable();
    if (f->op != OP_NONE)
        follower_op_finish(f, OP_CODE_CANCEL);
    follower_hard_stop(f);
    f->error_latched = 0;
    f->ff_underrun = 0;
    irq_enable();
}
DECL_COMMAND(command_follower_cmd_clear_errors,
             "follower_cmd_clear_errors oid=%c");

/****************************************************************
 * Reporting task (all sendf lives here) and shutdown
 ****************************************************************/

void
follower_task(void)
{
    if (!sched_check_wake(&follower_wake))
        return;
    uint8_t oid;
    struct follower *f;
    foreach_oid(oid, f, command_config_follower) {
        for (;;) {
            irq_disable();
            if (f->status_tail == f->status_head) {
                irq_enable();
                break;
            }
            struct pending_status ps = f->status[f->status_tail];
            f->status_tail = (f->status_tail + 1) % STATUS_RING_SIZE;
            irq_enable();
            sendf("follower_action_status oid=%c action=%c code=%c value=%u"
                  " gen=%c", oid, ps.action, ps.code, ps.value, ps.gen);
        }
        if (f->telemetry_due) {
            irq_disable();
            f->telemetry_due = 0;
            uint8_t flags = (f->following ? TF_FOLLOWING : 0)
                | (f->direction ? TF_DIRECTION : 0)
                | (f->op != OP_NONE ? TF_OP_IN_FLIGHT : 0)
                | ((f->following || f->op != OP_NONE)
                   && f->fps_age_ms > f->fps_stale_ms ? TF_FPS_STALE : 0)
                | (f->ff_underrun ? TF_FF_UNDERRUN : 0)
                | (f->error_latched ? TF_ERROR_LATCHED : 0);
            uint8_t pre = f->pre_state, post = f->post_state;
            int32_t steps = f->step_count, vel = f->v_cmd;
            irq_enable();
            sendf("follower_stats oid=%c pre=%c post=%c flags=%c"
                  " step_count=%i velocity=%i", oid, pre, post, flags,
                  steps, vel);
        }
    }
}
DECL_TASK(follower_task);

void
follower_shutdown(void)
{
    uint8_t oid;
    struct follower *f;
    foreach_oid(oid, f, command_config_follower) {
        follower_hard_stop(f);
        f->op = OP_NONE;
        follower_motor_enable(f, 0);
        f->error_latched = 1;
    }
}
DECL_SHUTDOWN(follower_shutdown);
