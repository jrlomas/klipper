// Execution log: the uplink twin of the intention queue (FD-0001
// doc 08). A per-board ring buffer records what was actually
// executed (segments, triggers, holds, rebases, faults). Records
// stream live as best-effort telemetry and remain retrievable after
// a failure via a reliable pull (execlog_dump) — recovery must never
// depend on records that were droppable while things were going
// wrong.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "basecmd.h" // oid_alloc
#include "board/irq.h" // irq_disable
#include "command.h" // DECL_COMMAND
#include "execlog.h" // execlog_append
#include "sched.h" // DECL_TASK

struct execlog_record {
    uint32_t seq;
    uint32_t clock;
    int32_t pos;
    uint32_t aux;
    uint8_t type;
    uint8_t src_oid;
};

struct execlog {
    struct execlog_record *ring;
    uint16_t size;
    // Sequence of the next record to be written; the ring holds
    // records [max(0, next_seq - size), next_seq).
    uint32_t next_seq;
    uint32_t stream_dropped;
    // Live streaming state
    uint32_t stream_next; // next seq to stream (task context)
    uint8_t stream_max;   // records per task wake (0 = streaming off)
    uint8_t oid;
};

static struct execlog *main_log;
static struct task_wake execlog_wake;

void
execlog_append(uint8_t type, uint8_t src_oid, uint32_t clock
               , int32_t pos, uint32_t aux)
{
    struct execlog *el = main_log;
    if (!el)
        return;
    irqstatus_t flag = irq_save();
    struct execlog_record *r = &el->ring[el->next_seq % el->size];
    r->seq = el->next_seq++;
    r->type = type;
    r->src_oid = src_oid;
    r->clock = clock;
    r->pos = pos;
    r->aux = aux;
    irq_restore(flag);
    if (el->stream_max)
        sched_wake_task(&execlog_wake);
}

void
command_config_execlog(uint32_t *args)
{
    struct execlog *el = oid_alloc(args[0], command_config_execlog
                                   , sizeof(*el));
    uint16_t size = args[1];
    if (!size)
        shutdown("Invalid execlog size");
    el->ring = alloc_chunk(sizeof(*el->ring) * size);
    el->size = size;
    el->oid = args[0];
    main_log = el;
}
DECL_COMMAND(command_config_execlog, "config_execlog oid=%c size=%hu");

static struct execlog *
execlog_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_execlog);
}

// Copy one record out of the ring; returns 0 if seq already evicted
// or not yet written.
static int
execlog_fetch(struct execlog *el, uint32_t seq, struct execlog_record *out)
{
    irq_disable();
    uint32_t next = el->next_seq;
    uint32_t oldest = next > el->size ? next - el->size : 0;
    if (seq >= next || seq < oldest) {
        irq_enable();
        return 0;
    }
    *out = el->ring[seq % el->size];
    irq_enable();
    return 1;
}

static void
execlog_send(struct execlog *el, const struct execlog_record *r)
{
    sendf("execlog_data oid=%c seq=%u type=%c src=%c clock=%u pos=%i aux=%u"
          , el->oid, r->seq, r->type, r->src_oid, r->clock, r->pos, r->aux);
}

void
command_execlog_query(uint32_t *args)
{
    struct execlog *el = execlog_oid_lookup(args[0]);
    irq_disable();
    uint32_t next = el->next_seq;
    uint32_t dropped = el->stream_dropped;
    irq_enable();
    uint32_t oldest = next > el->size ? next - el->size : 0;
    sendf("execlog_status oid=%c next_seq=%u oldest_seq=%u dropped=%u"
          , el->oid, next, oldest, dropped);
}
DECL_COMMAND(command_execlog_query, "execlog_query oid=%c");

// Reliable post-failure pull: the host drains the retained ring in
// bounded chunks. This is the Class-1 path recovery depends on.
void
command_execlog_dump(uint32_t *args)
{
    struct execlog *el = execlog_oid_lookup(args[0]);
    uint32_t seq = args[1];
    uint8_t count = args[2];
    if (count > 16)
        count = 16;
    while (count--) {
        struct execlog_record r;
        if (!execlog_fetch(el, seq, &r))
            break;
        execlog_send(el, &r);
        seq++;
    }
}
DECL_COMMAND(command_execlog_dump, "execlog_dump oid=%c seq=%u count=%c");

// Enable/disable live streaming (best-effort telemetry). max_per_wake
// bounds the send rate; records evicted before they stream are
// counted, never silently lost.
void
command_execlog_stream(uint32_t *args)
{
    struct execlog *el = execlog_oid_lookup(args[0]);
    irq_disable();
    el->stream_max = args[1];
    el->stream_next = el->next_seq;
    irq_enable();
    if (el->stream_max)
        sched_wake_task(&execlog_wake);
}
DECL_COMMAND(command_execlog_stream, "execlog_stream oid=%c max_per_wake=%c");

void
execlog_task(void)
{
    if (!sched_check_wake(&execlog_wake))
        return;
    struct execlog *el = main_log;
    if (!el || !el->stream_max)
        return;
    uint8_t budget = el->stream_max;
    for (;;) {
        irq_disable();
        uint32_t next = el->next_seq;
        uint32_t oldest = next > el->size ? next - el->size : 0;
        if (el->stream_next < oldest) {
            // Ring lapped the streamer: account the loss
            el->stream_dropped += oldest - el->stream_next;
            el->stream_next = oldest;
        }
        uint32_t seq = el->stream_next;
        irq_enable();
        if (seq >= next)
            break;
        if (!budget--) {
            // More pending; keep the task awake for the next slice
            sched_wake_task(&execlog_wake);
            break;
        }
        struct execlog_record r;
        if (execlog_fetch(el, seq, &r))
            execlog_send(el, &r);
        el->stream_next = seq + 1;
    }
}
DECL_TASK(execlog_task);
