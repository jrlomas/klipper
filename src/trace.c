// Structured trace plane (Atlas / FD-0002 §3).
//
// A registered, machine-time-stamped event channel: the firmware author
// writes LOG2(TRACE_SUB_MOTION, TRACE_LVL_WARN, TRACE_EV_step_underrun,
// horizon_us, queue_depth) and the host renders it to a human string via
// the data dictionary — the same annotation/self-description mechanism
// the command registry already uses (FD-0001 doc 10). Events carry the
// MCU clock, so the host merges traces from every board into one
// machine-time timeline (the substrate Atlas Planes 2-4 read).
//
// Structurally this is the execution log's lighter sibling: an IRAM-safe
// ring written from any context, streamed best-effort as Class-2
// telemetry, bounded and near-zero-cost when a subsystem's level is off.
// Recovery never depends on it (that is execlog's job); trace is for
// understanding what a healthy or misbehaving board is doing.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "basecmd.h" // oid_alloc
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // DECL_COMMAND
#include "sched.h" // DECL_TASK
#include "trace.h" // trace_emit

struct trace_record {
    uint32_t seq;
    uint32_t clock;
    uint32_t args[TRACE_MAX_ARGS];
    uint16_t event;
    uint8_t sub;
    uint8_t level;
    uint8_t argc;
};

struct trace {
    struct trace_record *ring;
    uint16_t size;
    uint32_t next_seq;
    uint32_t stream_dropped;
    uint32_t stream_next;
    uint8_t stream_max; // records per task wake (0 = streaming off)
    uint8_t oid;
};

static struct trace *main_trace;
static struct task_wake trace_wake;
// Per-subsystem threshold: a record is kept when its level <= the
// subsystem's threshold. Default OFF, so a freshly booted board traces
// nothing until the host opts a subsystem in.
static uint8_t trace_level[TRACE_SUB_COUNT];

int
trace_enabled(uint8_t sub, uint8_t level)
{
    if (!main_trace || sub >= TRACE_SUB_COUNT)
        return 0;
    return level <= trace_level[sub];
}

void
trace_emit(uint16_t event, uint8_t sub, uint8_t level, uint8_t argc
           , uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
    struct trace *t = main_trace;
    if (!t || sub >= TRACE_SUB_COUNT || level > trace_level[sub])
        return;
    if (argc > TRACE_MAX_ARGS)
        argc = TRACE_MAX_ARGS;
    uint32_t clock = timer_read_time();
    irqstatus_t flag = irq_save();
    struct trace_record *r = &t->ring[t->next_seq % t->size];
    r->seq = t->next_seq++;
    r->clock = clock;
    r->event = event;
    r->sub = sub;
    r->level = level;
    r->argc = argc;
    r->args[0] = a0;
    r->args[1] = a1;
    r->args[2] = a2;
    r->args[3] = a3;
    irq_restore(flag);
    if (t->stream_max)
        sched_wake_task(&trace_wake);
}

void
command_config_trace(uint32_t *args)
{
    struct trace *t = oid_alloc(args[0], command_config_trace, sizeof(*t));
    uint16_t size = args[1];
    if (!size)
        shutdown("Invalid trace size");
    t->ring = alloc_chunk(sizeof(*t->ring) * size);
    t->size = size;
    t->oid = args[0];
    main_trace = t;
}
DECL_COMMAND(command_config_trace, "config_trace oid=%c size=%hu");

// Raise/lower a subsystem's trace level (host-driven, cheap). Levels
// above the subsystem count are ignored so a stale host cannot corrupt
// the array.
void
command_trace_set_level(uint32_t *args)
{
    uint8_t sub = args[0], level = args[1];
    if (sub < TRACE_SUB_COUNT)
        trace_level[sub] = level;
}
DECL_COMMAND(command_trace_set_level, "trace_set_level sub=%c level=%c");

static struct trace *
trace_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_trace);
}

static void
trace_send(struct trace *t, const struct trace_record *r)
{
    // Args ride as a length-prefixed byte blob so the wire carries only
    // argc*4 bytes and the host unpacks per the event's registered
    // format. Little-endian to match the record layout on the wire.
    uint8_t buf[TRACE_MAX_ARGS * 4];
    uint8_t n = r->argc * 4;
    for (uint8_t i = 0; i < r->argc; i++) {
        uint32_t v = r->args[i];
        buf[i * 4 + 0] = v;
        buf[i * 4 + 1] = v >> 8;
        buf[i * 4 + 2] = v >> 16;
        buf[i * 4 + 3] = v >> 24;
    }
    sendf("trace_data oid=%c seq=%u clock=%u event=%hu sub=%c level=%c"
          " data=%*s", t->oid, r->seq, r->clock, r->event, r->sub
          , r->level, n, buf);
}

static int
trace_fetch(struct trace *t, uint32_t seq, struct trace_record *out)
{
    irq_disable();
    uint32_t next = t->next_seq;
    uint32_t oldest = next > t->size ? next - t->size : 0;
    if (seq >= next || seq < oldest) {
        irq_enable();
        return 0;
    }
    *out = t->ring[seq % t->size];
    irq_enable();
    return 1;
}

void
command_trace_query(uint32_t *args)
{
    struct trace *t = trace_oid_lookup(args[0]);
    irq_disable();
    uint32_t next = t->next_seq;
    uint32_t dropped = t->stream_dropped;
    irq_enable();
    uint32_t oldest = next > t->size ? next - t->size : 0;
    sendf("trace_status oid=%c next_seq=%u oldest_seq=%u dropped=%u"
          , t->oid, next, oldest, dropped);
}
DECL_COMMAND(command_trace_query, "trace_query oid=%c");

void
command_trace_stream(uint32_t *args)
{
    struct trace *t = trace_oid_lookup(args[0]);
    irq_disable();
    t->stream_max = args[1];
    t->stream_next = t->next_seq;
    irq_enable();
    if (t->stream_max)
        sched_wake_task(&trace_wake);
}
DECL_COMMAND(command_trace_stream, "trace_stream oid=%c max_per_wake=%c");

// Best-effort live streaming (Class-2). Records evicted before they
// stream are counted, never silently lost.
void
trace_task(void)
{
    if (!sched_check_wake(&trace_wake))
        return;
    struct trace *t = main_trace;
    if (!t || !t->stream_max)
        return;
    uint8_t budget = t->stream_max;
    for (;;) {
        irq_disable();
        uint32_t next = t->next_seq;
        uint32_t oldest = next > t->size ? next - t->size : 0;
        if (t->stream_next < oldest) {
            t->stream_dropped += oldest - t->stream_next;
            t->stream_next = oldest;
        }
        uint32_t seq = t->stream_next;
        irq_enable();
        if (seq >= next)
            break;
        if (!budget--) {
            sched_wake_task(&trace_wake);
            break;
        }
        struct trace_record r;
        if (trace_fetch(t, seq, &r))
            trace_send(t, &r);
        t->stream_next = seq + 1;
    }
}
DECL_TASK(trace_task);

// --- Event & subsystem registration (published to the host dictionary) --
// The host reads these to render event id -> name and to unpack the arg
// blob per event. Format strings name the args in order; %u = unsigned,
// %i = signed, %x = hex. Keep names terse — they cross the wire once, in
// the dictionary, not per event.
DECL_ENUMERATION("trace_sub", "core", TRACE_SUB_CORE);
DECL_ENUMERATION("trace_sub", "motion", TRACE_SUB_MOTION);
DECL_ENUMERATION("trace_sub", "comms", TRACE_SUB_COMMS);
DECL_ENUMERATION("trace_sub", "heater", TRACE_SUB_HEATER);
DECL_ENUMERATION("trace_sub", "trigger", TRACE_SUB_TRIGGER);

DECL_ENUMERATION("trace_event", "step_underrun", TRACE_EV_step_underrun);
DECL_ENUMERATION("trace_event", "queue_refill", TRACE_EV_queue_refill);
DECL_ENUMERATION("trace_event", "comm_retransmit", TRACE_EV_comm_retransmit);
DECL_ENUMERATION("trace_event", "hold_enter", TRACE_EV_hold_enter);
DECL_ENUMERATION("trace_event", "rebase", TRACE_EV_rebase);
DECL_ENUMERATION("trace_event", "trigger_fire", TRACE_EV_trigger_fire);

DECL_CONSTANT_STR("trace_fmt step_underrun", "horizon_us=%u queue_depth=%u");
DECL_CONSTANT_STR("trace_fmt queue_refill", "depth=%u added=%u");
DECL_CONSTANT_STR("trace_fmt comm_retransmit", "seq=%u count=%u");
DECL_CONSTANT_STR("trace_fmt hold_enter", "reason=%u");
DECL_CONSTANT_STR("trace_fmt rebase", "new_anchor=%u");
DECL_CONSTANT_STR("trace_fmt trigger_fire", "source_oid=%u reason=%u");
