#ifndef __TRACE_H
#define __TRACE_H
// Structured trace plane (Atlas / FD-0002 §3): registered, machine-time-
// stamped events rendered to human strings on the host via the data
// dictionary — the HELIX-native answer to OAMS's CAN printf. Near-zero
// cost when a subsystem's level is off; a few bytes on the wire when on.

#include <stdint.h>
#include "command.h" // DECL_ENUMERATION, DECL_CONSTANT_STR

// Severity levels, low value = more severe (so "emit at or above" is a
// simple <= test). Kept tiny to fit a per-subsystem byte.
enum {
    TRACE_LVL_ERROR = 0,
    TRACE_LVL_WARN = 1,
    TRACE_LVL_INFO = 2,
    TRACE_LVL_DEBUG = 3,
    TRACE_LVL_OFF = 255, // subsystem default: emit nothing
};

// Trace subsystems. Add here as instrumentation lands; the host reads
// the names from the dictionary, so ids need only be stable within a
// build. Keep the count small — it sizes the per-subsystem level array.
enum {
    TRACE_SUB_CORE = 0,
    TRACE_SUB_MOTION,
    TRACE_SUB_COMMS,
    TRACE_SUB_HEATER,
    TRACE_SUB_TRIGGER,
    TRACE_SUB_COUNT,
};

// Registered trace events. Ids are stable within a build and published
// to the host dictionary via DECL_ENUMERATION in trace.c, alongside a
// render-format string (DECL_CONSTANT_STR "trace_fmt_<name>").
enum {
    TRACE_EV_NONE = 0,
    TRACE_EV_step_underrun,   // motion queue ran dry: horizon_us, queue_depth
    TRACE_EV_queue_refill,    // refill watermark hit: depth, added
    TRACE_EV_comm_retransmit, // link retransmit: seq, count
    TRACE_EV_hold_enter,      // entered hold: reason
    TRACE_EV_rebase,          // host re-anchored: new_anchor
    TRACE_EV_trigger_fire,    // hardware trigger: source_oid, reason
    TRACE_EV_COUNT,
};

#define TRACE_MAX_ARGS 4

// Cheap, IRQ-safe emit. A no-op until the host configures the log and
// raises the subsystem's level. Prefer the LOG* macros, which skip
// argument evaluation entirely when the level is off.
void trace_emit(uint16_t event, uint8_t sub, uint8_t level
                , uint8_t argc, uint32_t a0, uint32_t a1
                , uint32_t a2, uint32_t a3);

// True when (sub, level) would be recorded. Guards the LOG* macros so
// nothing is computed when tracing is off.
int trace_enabled(uint8_t sub, uint8_t level);

// printf-ergonomics with near-zero cost when off. The firmware author
// names the subsystem and level; the host renders event+args to a
// string via the dictionary.
#define LOG0(sub, lvl, ev) \
    do { if (trace_enabled((sub), (lvl))) \
        trace_emit((ev), (sub), (lvl), 0, 0, 0, 0, 0); } while (0)
#define LOG1(sub, lvl, ev, a0) \
    do { if (trace_enabled((sub), (lvl))) \
        trace_emit((ev), (sub), (lvl), 1, (a0), 0, 0, 0); } while (0)
#define LOG2(sub, lvl, ev, a0, a1) \
    do { if (trace_enabled((sub), (lvl))) \
        trace_emit((ev), (sub), (lvl), 2, (a0), (a1), 0, 0); } while (0)
#define LOG3(sub, lvl, ev, a0, a1, a2) \
    do { if (trace_enabled((sub), (lvl))) \
        trace_emit((ev), (sub), (lvl), 3, (a0), (a1), (a2), 0); } while (0)
#define LOG4(sub, lvl, ev, a0, a1, a2, a3) \
    do { if (trace_enabled((sub), (lvl))) \
        trace_emit((ev), (sub), (lvl), 4, (a0), (a1), (a2), (a3)); } while (0)

#endif // trace.h
