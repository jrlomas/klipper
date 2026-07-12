// Console transport over authenticated UDP datagrams
//
// This module binds klipper's console contract (command.c frame
// blocks - see serial_irq.c for the reference wired binding) to the
// intentproto datagram transport of RFC 0001 doc 07:
//
//  - received datagrams are authenticated (truncated HMAC-SHA256)
//    and sequence-checked by the intentproto layer (udp_datagram.h),
//    then their payload - whole legacy klipper frames - is fed to
//    command_find_and_dispatch()
//  - transmitted frames are batched for ~2ms (amortizing per-packet
//    overhead, mirroring udp_bridge.py's host-side batching), then
//    wrapped and sealed into one datagram
//
// Erasure FEC (RFC 0001 doc 07, "two layers"): when a port selects a
// non-zero fec_k (udp_console_set_fec_k), the tx side emits a parity
// datagram after every k data datagrams and the rx side reconstructs a
// single datagram lost inside a protected block the moment that block's
// parity arrives - no retransmit-timeout wait.  Reassembly is in-order
// and single-loss: the reconstructed datagram's frames are unwrapped
// and fed to the SAME command_find_and_dispatch() path as a normally
// received datagram, so recovery is transparent to the console.  Two
// or more losses in one block (or a lost parity) fall through to the
// frame layer's ARQ exactly as before.  The recovered bytes are the
// XOR of already-authenticated survivors and parity, so they inherit
// the block's authentication and are not re-verified (there is no
// per-datagram tag on a reconstruction).  fec_k defaults to 0, which
// disables the erasure layer and preserves the pure-ARQ behaviour.
//
// The module is transport independent: socket send/recv lives behind
// a small per-port ops struct (struct udp_console_ops), so the same
// code serves an ESP32 WiFi lwIP socket, an ESP32 RMII-Ethernet lwIP
// socket, and the linux mcu's desktop-testable UDP option.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // command_encode_and_frame
#include "sched.h" // sched_add_timer
#include "udp_console.h" // udp_console_init
#include "udp_datagram.h" // udpdg_encode

// Batch outgoing frames briefly to amortize per-datagram overhead
#define UDP_CONSOLE_BATCH_US 2000

static const struct udp_console_ops *udp_ops;
static void *udp_ops_ctx;
static struct task_wake udp_wake;
// XOR erasure block size; 0 = erasure layer off (set before init)
static uint8_t udp_fec_k;

// Reassembled receive stream (frame bytes unwrapped from datagrams)
static uint8_t receive_buf[2 * UDPDG_FRAMES_MAX];
static uint32_t receive_pos;
// Scratch space for one inbound datagram
static uint8_t rx_dgram[UDPDG_DATAGRAM_MAX];
// Scratch space for a parity-reconstructed datagram (hdr + frames)
static uint8_t rx_recovered[UDPDG_DATAGRAM_MAX];

// Outbound frames pending batching (guarded by irq_save)
static uint8_t transmit_buf[UDPDG_FRAMES_MAX];
static uint32_t transmit_pos;
static uint8_t flush_timer_armed, flush_due;
static struct timer flush_timer;
// Staging area for the sealed outbound datagram (task context only)
static uint8_t tx_stage[UDPDG_FRAMES_MAX];
static uint8_t tx_dgram[UDPDG_DATAGRAM_MAX];

void *
udp_console_get_rx_buf(void)
{
    return receive_buf;
}

void
udp_console_note_rx(void)
{
    sched_wake_task(&udp_wake);
}

// Batch deadline reached - flush pending frames from task context
static uint_fast8_t
flush_event(struct timer *t)
{
    flush_timer_armed = 0;
    flush_due = 1;
    sched_wake_task(&udp_wake);
    return SF_DONE;
}

// Encode and queue a "response" message (board console_sendf handler)
void
udp_console_sendf(const struct command_encoder *ce, va_list args)
{
    if (!udp_ops)
        return;
    uint8_t framebuf[MESSAGE_MAX];
    uint_fast8_t msglen = command_encode_and_frame(framebuf, sizeof(framebuf)
                                                   , ce, args);
    if (!msglen)
        return;
    irqstatus_t flag = irq_save();
    if (transmit_pos + msglen <= sizeof(transmit_buf)) {
        memcpy(&transmit_buf[transmit_pos], framebuf, msglen);
        transmit_pos += msglen;
        if (!flush_timer_armed) {
            flush_timer_armed = 1;
            flush_timer.func = flush_event;
            flush_timer.waketime = (timer_read_time()
                                    + timer_from_us(UDP_CONSOLE_BATCH_US));
            sched_add_timer(&flush_timer);
        }
        if (transmit_pos + MESSAGE_MAX > sizeof(transmit_buf)) {
            // No room for another frame - drain without waiting
            flush_due = 1;
            sched_wake_task(&udp_wake);
        }
    }
    // else: buffer full - drop the message (as wired ports do on
    // transmit overflow; the host retransmits on a missing ack)
    irq_restore(flag);
}

// Seal and transmit the pending frame batch (task context)
static void
udp_console_flush(void)
{
    irqstatus_t flag = irq_save();
    uint32_t len = transmit_pos;
    if (len)
        memcpy(tx_stage, transmit_buf, len);
    transmit_pos = 0;
    flush_due = 0;
    irq_restore(flag);
    if (!len)
        return;
    uint32_t dlen = udpdg_encode(tx_dgram, tx_stage, len);
    if (dlen)
        udp_ops->send(udp_ops_ctx, tx_dgram, dlen);
    // If FEC is on and this datagram completed a protected block, emit
    // its parity datagram.  Reuse tx_dgram: ops->send has already
    // copied the data datagram into the socket.
    uint32_t plen = udpdg_parity_flush(tx_dgram);
    if (plen)
        udp_ops->send(udp_ops_ctx, tx_dgram, plen);
}

// Append unwrapped frame bytes to the receive stream
static void
rx_append(const uint8_t *data, uint32_t len)
{
    if (receive_pos + len > sizeof(receive_buf))
        // Overflow - drop buffered bytes; the frame sequence check
        // naks and the host retransmits (as on serial rx overflow)
        receive_pos = 0;
    if (len > sizeof(receive_buf))
        return;
    memcpy(&receive_buf[receive_pos], data, len);
    receive_pos += len;
}

// Process incoming datagrams and dispatch any complete frame blocks
void
udp_console_task(void)
{
    if (!sched_check_wake(&udp_wake))
        return;
    if (!udp_ops)
        return;

    // Unwrap all pending datagrams into the receive stream
    for (;;) {
        int32_t got = udp_ops->recv(udp_ops_ctx, rx_dgram, sizeof(rx_dgram));
        if (got <= 0)
            break;
        const uint8_t *frames;
        int32_t flen = udpdg_decode(rx_dgram, got, &frames);
        if (flen < 0)
            // Authentication failure or malformed - silently drop
            continue;
        if (udp_ops->rx_accepted)
            udp_ops->rx_accepted(udp_ops_ctx);
        if (flen > 0) {
            rx_append(frames, flen);
        } else {
            // A consumed (flen==0) datagram may have been a parity that
            // reconstructed a single lost datagram of its block.  Feed
            // the recovered datagram's frames (past its header) into the
            // same dispatch stream, in block order, so the lost command
            // takes effect without waiting out an ARQ retransmit.
            uint32_t rlen = udpdg_take_recovered(rx_recovered
                                                 , sizeof(rx_recovered));
            if (rlen > UDPDG_HEADER)
                rx_append(rx_recovered + UDPDG_HEADER, rlen - UDPDG_HEADER);
        }
    }

    // Find and dispatch message blocks in the receive stream
    uint32_t len = receive_pos;
    while (len) {
        uint_fast8_t pop_count;
        uint_fast8_t msglen = len > MESSAGE_MAX ? MESSAGE_MAX : len;
        int_fast8_t ret = command_find_and_dispatch(receive_buf, msglen
                                                    , &pop_count);
        if (!ret)
            break;
        len -= pop_count;
        if (len)
            memmove(receive_buf, &receive_buf[pop_count], len);
    }
    receive_pos = len;

    // Transmit pending responses once the batch deadline has passed
    if (flush_due)
        udp_console_flush();
}
DECL_TASK(udp_console_task);

// sched_shutdown() clears the timer list - re-arm the flush state so
// the shutdown messages queued by the shutdown handlers still go out
void
udp_console_shutdown(void)
{
    flush_timer_armed = 0;
    flush_due = 1;
    sched_wake_task(&udp_wake);
}
DECL_SHUTDOWN(udp_console_shutdown);

void
udp_console_set_fec_k(uint8_t fec_k)
{
    udp_fec_k = fec_k;
}

void
udp_console_init(const struct udp_console_ops *ops, void *ctx
                 , const uint8_t *psk, uint32_t psk_len)
{
    udpdg_init(psk, psk_len, udp_fec_k);
    udp_ops_ctx = ctx;
    udp_ops = ops;
}
