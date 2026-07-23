// Console transport over authenticated UDP datagrams
//
// This module binds klipper's console contract (command.c frame
// blocks - see serial_irq.c for the reference wired binding) to the
// intentproto datagram transport of FD-0001 doc 07:
//
//  - received datagrams are authenticated (truncated HMAC-SHA256)
//    and sequence-checked by the intentproto layer (udp_datagram.h),
//    then their payload - whole legacy klipper frames - is fed to
//    command_find_and_dispatch()
//  - responses produced by one command dispatch are coalesced, then flushed
//    immediately from task context and sealed into one datagram
//
// Erasure FEC (FD-0001 doc 07, "two layers"): when a port selects a
// fec_k=2 (udp_console_set_fec_k), the tx side emits a parity datagram
// after every data pair and the rx side reconstructs either single loss
// the moment that pair's
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
#include "autoconf.h" // CONFIG_WANT_DATAGRAM_SESSION
#include "board/irq.h" // irq_save
#include "board/misc.h" // timer_read_time
#include "command.h" // command_encode_and_frame
#include "sched.h" // sched_wake_task
#include "udp_console.h" // udp_console_init
#include "udp_datagram.h" // udpdg_encode

#define UDP_SESSION_HANDSHAKE_TIMEOUT_US 2000000

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

// Outbound frames pending the task-context flush (guarded by irq_save)
static uint8_t transmit_buf[UDPDG_FRAMES_MAX];
static uint32_t transmit_pos;
static uint8_t flush_due;
// Staging area for the sealed outbound datagram (task context only)
static uint8_t tx_stage[UDPDG_FRAMES_MAX];
static uint8_t tx_dgram[UDPDG_DATAGRAM_MAX];
static uint32_t console_rx_decoded, console_responses;
static uint32_t console_response_drops, console_flushes;
static uint32_t console_no_peer_drops, console_send_failures;

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
        // Flush in task context without relying on a deferred timer wake.
        // Multiple responses emitted by one command dispatch (including its
        // ACK) still coalesce because the task drains only after dispatch.
        flush_due = 1;
        sched_wake_task(&udp_wake);
        console_responses++;
    } else
        // Buffer full: account for the drop; v1 ARQ retransmits when the
        // acknowledgement does not arrive.
        console_response_drops++;
    irq_restore(flag);
}

static void
udp_console_record_send(int result)
{
    if (result == UDP_CONSOLE_SEND_NO_PEER)
        console_no_peer_drops++;
    else if (result != UDP_CONSOLE_SEND_OK)
        console_send_failures++;
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
    console_flushes++;
#if CONFIG_WANT_DATAGRAM_SESSION
    // Once the session is established, replies go out as session
    // datagrams (rotating keys, replay-protected). The static path and
    // its erasure-FEC layer are used only before/without a session.
    if (udpsess_established()) {
        uint32_t slen = udpsess_encode(tx_dgram, sizeof(tx_dgram),
                                       tx_stage, len);
        if (slen) {
            if (udp_ops->send_checked) {
                int ret = udp_ops->send_checked(udp_ops_ctx, tx_dgram, slen);
                udp_console_record_send(ret);
            } else
                udp_ops->send(udp_ops_ctx, tx_dgram, slen);
        }
        return;
    }
#endif
    uint32_t dlen = udpdg_encode(tx_dgram, tx_stage, len);
    if (dlen) {
        if (udp_ops->send_checked) {
            int ret = udp_ops->send_checked(udp_ops_ctx, tx_dgram, dlen);
            udp_console_record_send(ret);
        } else
            udp_ops->send(udp_ops_ctx, tx_dgram, dlen);
    }
    // If FEC is on and this datagram completed a protected block, emit
    // its parity datagram.  Reuse tx_dgram: ops->send has already
    // copied the data datagram into the socket.
    uint32_t plen = udpdg_parity_flush(tx_dgram);
    if (plen) {
        if (udp_ops->send_checked) {
            int ret = udp_ops->send_checked(udp_ops_ctx, tx_dgram, plen);
            udp_console_record_send(ret);
        } else
            udp_ops->send(udp_ops_ctx, tx_dgram, plen);
    }
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
        int32_t flen;
#if CONFIG_WANT_DATAGRAM_SESSION
        // Bounded half-open lifetime (see below).
        static uint32_t sess_hs_deadline;
        // Route by datagram kind. A handshake message is answered by the
        // session; a session datagram is decrypted-authenticated by it;
        // anything else falls to the static path before session pinning.
        int kind = udpsess_msg_type(rx_dgram, got);
        // A static datagram's sequence high byte may equal a handshake
        // type (0x51/0x53) or carry DGF_SESSION's bit. Authentication is
        // the unambiguous discriminator: valid static traffic always wins.
        if (kind && !udpsess_established()
            && udpdg_is_authenticated_static(rx_dgram, got))
            kind = 0;
        if (kind == 1 || kind == 3) {
            // DoS hardening in both startup and reconnect: ClientHello has
            // already been PSK-authenticated by the session layer before it
            // can produce rlen, a different hello cannot replace an active
            // candidate, and half-open state expires. ServerHello is sent
            // directly to the candidate without changing the authenticated
            // tx peer; that peer is committed only after ClientFin.
            uint32_t now = timer_read_time();
            if (sess_hs_deadline
                && !timer_is_before(now, sess_hs_deadline)) {
                udpsess_reset_handshake();
                sess_hs_deadline = 0;
            }
            uint32_t rlen = udpsess_on_handshake(rx_dgram, got, tx_dgram,
                                                 sizeof(tx_dgram));
            int adopted = udpsess_take_peer_adopted();
            if (adopted) {
                sess_hs_deadline = 0;
                if (udp_ops->rx_accepted)
                    udp_ops->rx_accepted(udp_ops_ctx);
            } else if (rlen && !sess_hs_deadline) {
                sess_hs_deadline = (
                    now + timer_from_us(UDP_SESSION_HANDSHAKE_TIMEOUT_US));
            }
            if (rlen && udp_ops->send_candidate)
                udp_ops->send_candidate(udp_ops_ctx, tx_dgram, rlen);
            continue;
        }
        if (kind == 2 && udpsess_established()) {
            flen = udpsess_decode(rx_dgram, got, &frames);
            if (flen < 0)
                continue;
            console_rx_decoded++;
            if (udp_ops->rx_accepted)
                udp_ops->rx_accepted(udp_ops_ctx);
            if (flen > 0)
                rx_append(frames, flen);
            continue;
        }
        // Once a session is live, pin the data path to it. Static-PSK
        // traffic remains the pre-session/backward-compatible bootstrap,
        // but cannot bypass session identity, replay, or rotating keys.
        if (udpsess_established())
            continue;
#endif
        flen = udpdg_decode(rx_dgram, got, &frames);
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
            uint32_t rlen;
            while ((rlen = udpdg_take_recovered(
                        rx_recovered, sizeof(rx_recovered))) > UDPDG_HEADER)
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

    // Transmit the responses produced by this task drain.
    if (flush_due)
        udp_console_flush();
}
DECL_TASK(udp_console_task);

void
command_udp_console_get_status(uint32_t *args)
{
    (void)args;
    struct udpsess_stats session = {0};
#if CONFIG_WANT_DATAGRAM_SESSION
    udpsess_get_stats(&session);
#endif
    sendf("udp_console_status decoded=%u responses=%u response_drops=%u"
          " flushes=%u no_peer_drops=%u send_failures=%u"
          " session_tx_epoch=%u session_tx_seq=%u"
          " session_rx_epoch=%u session_rx_top=%u"
          " session_auth_failures=%u session_replays=%u"
          " session_old_epoch=%u",
          console_rx_decoded, console_responses, console_response_drops,
          console_flushes, console_no_peer_drops, console_send_failures,
          session.tx_epoch, session.tx_seq,
          session.rx_epoch, session.rx_window_top,
          session.auth_failures, session.replays_rejected,
          session.old_epoch_rejected);
}
DECL_COMMAND_FLAGS(command_udp_console_get_status, HF_IN_SHUTDOWN,
                   "udp_console_get_status");

// Ensure shutdown messages queued by shutdown handlers still go out.
void
udp_console_shutdown(void)
{
    flush_due = 1;
    sched_wake_task(&udp_wake);
}
DECL_SHUTDOWN(udp_console_shutdown);

void
udp_console_set_fec_k(uint8_t fec_k)
{
    udp_fec_k = fec_k;
}

#if CONFIG_WANT_DATAGRAM_SESSION
// Gather a 16-byte per-boot nonce for the session handshake. Uniqueness,
// not secrecy, is required (the PSK authenticates), so the free-running
// timer sampled across a short spin is adequate; a port with a hardware
// UID/RNG can mix more in here.
static void
session_nonce(uint8_t out[16])
{
    for (int i = 0; i < 16; i++) {
        uint32_t t = timer_read_time();
        out[i] = (uint8_t)(t ^ (t >> 8) ^ (t >> 16) ^ (t >> 24));
        // a tiny variable spin so successive samples differ
        for (volatile int s = 0; s < (int)(t & 7) + 1; s++)
            ;
    }
}
#endif

void
udp_console_init(const struct udp_console_ops *ops, void *ctx
                 , const uint8_t *psk, uint32_t psk_len)
{
    udpdg_init(psk, psk_len, udp_fec_k);
#if CONFIG_WANT_DATAGRAM_SESSION
    // Offer the DTLS-class session on top of the static-PSK floor. It
    // requires an authenticated link, so a PSK is mandatory here; without
    // one the session simply never establishes and traffic stays static.
    if (psk && psk_len) {
        static const char board_id[] = CONFIG_DATAGRAM_SESSION_ID;
        _Static_assert(sizeof(board_id) - 1 <= UDPDG_SESSION_ID_MAX,
                       "CONFIG_DATAGRAM_SESSION_ID exceeds 24 bytes");
        uint8_t nonce[16];
        session_nonce(nonce);
        udpsess_init(psk, psk_len, (const uint8_t *)board_id,
                     sizeof(board_id) - 1, nonce);
    }
#endif
    udp_ops_ctx = ctx;
    udp_ops = ops;
}
