// TTY based IO
//
// Copyright (C) 2017-2021  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#define _GNU_SOURCE
#include <errno.h> // errno
#include <fcntl.h> // fcntl
#include <poll.h> // ppoll
#include <pty.h> // openpty
#include <stdio.h> // fprintf
#include <string.h> // memmove
#include <sys/stat.h> // chmod
#include <time.h> // struct timespec
#include <unistd.h> // ttyname
#include "board/irq.h" // irq_wait
#include "board/misc.h" // console_sendf
#include "command.h" // command_find_block
#include "generic/udp_console.h" // udp_console_note_rx
#include "internal.h" // console_setup
#include "sched.h" // sched_wake_task
#if CONFIG_WANT_CONSOLE_FRAMING_V2
#include "generic/console_v2.h" // console_v2_try_rx
#include "generic/framing_v2.h" // FV2_MAX
// Advertise v2 framing so a v2 host knows this console accepts the BCH
// envelope (same latch/dual-accept contract as the serial_irq console).
DECL_CONSTANT("FRAMING_V2", 1);
#endif

static struct pollfd main_pfd[1];
#define MP_TTY_IDX   0
static int is_udp;

// Route the console through the datagram (UDP) transport glue,
// polling the given socket instead of a pty (see linux/udp.c)
void
console_use_udp(int fd)
{
    main_pfd[MP_TTY_IDX].fd = fd;
    main_pfd[MP_TTY_IDX].events = POLLIN;
    is_udp = 1;
}

// Report 'errno' in a message written to stderr
void
report_errno(char *where, int rc)
{
    int e = errno;
    fprintf(stderr, "Got error %d in %s: (%d)%s\n", rc, where, e, strerror(e));
}


/****************************************************************
 * Setup
 ****************************************************************/

int
set_non_blocking(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0) {
        report_errno("fcntl getfl", flags);
        return -1;
    }
    int ret = fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    if (ret < 0) {
        report_errno("fcntl setfl", flags);
        return -1;
    }
    return 0;
}

int
set_close_on_exec(int fd)
{
    int ret = fcntl(fd, F_SETFD, FD_CLOEXEC);
    if (ret < 0) {
        report_errno("fcntl set cloexec", ret);
        return -1;
    }
    return 0;
}

int
console_setup(char *name)
{
    // Open pseudo-tty
    struct termios ti;
    memset(&ti, 0, sizeof(ti));
    int mfd, sfd, ret = openpty(&mfd, &sfd, NULL, &ti, NULL);
    if (ret) {
        report_errno("openpty", ret);
        return -1;
    }
    ret = set_non_blocking(mfd);
    if (ret)
        return -1;
    ret = set_close_on_exec(mfd);
    if (ret)
        return -1;
    ret = set_close_on_exec(sfd);
    if (ret)
        return -1;
    main_pfd[MP_TTY_IDX].fd = mfd;
    main_pfd[MP_TTY_IDX].events = POLLIN;

    // Create symlink to tty
    unlink(name);
    char *tname = ttyname(sfd);
    if (!tname) {
        report_errno("ttyname", 0);
        return -1;
    }
    ret = symlink(tname, name);
    if (ret) {
        report_errno("symlink", ret);
        return -1;
    }
    ret = chmod(tname, 0660);
    if (ret) {
        report_errno("chmod", ret);
        return -1;
    }

    // Make sure stderr is non-blocking
    ret = set_non_blocking(STDERR_FILENO);
    if (ret)
        return -1;

    return 0;
}


/****************************************************************
 * Console handling
 ****************************************************************/

static struct task_wake console_wake;
static uint8_t receive_buf[4096];
static int receive_pos;

void *
console_receive_buffer(void)
{
    if (is_udp)
        return udp_console_get_rx_buf();
    return receive_buf;
}

// Process any incoming commands
void
console_task(void)
{
    if (!sched_check_wake(&console_wake))
        return;

    // Read data
    int ret = read(main_pfd[MP_TTY_IDX].fd, &receive_buf[receive_pos]
                   , sizeof(receive_buf) - receive_pos);
    if (ret < 0) {
        if (errno == EWOULDBLOCK) {
            ret = 0;
        } else {
            report_errno("read", ret);
            return;
        }
    }
    if (ret == 15 && receive_buf[receive_pos+14] == '\n'
        && memcmp(&receive_buf[receive_pos], "FORCE_SHUTDOWN\n", 15) == 0)
        shutdown("Force shutdown command");

    // Find and dispatch message blocks in the input
    int len = receive_pos + ret;
#if CONFIG_WANT_CONSOLE_FRAMING_V2
    // Dual-accept framing: a v2 (BCH) frame - up to FV2_MAX bytes, so the
    // scan window is wider than a v1 frame's - is de-framed, dispatched
    // and popped here; the stock v1 path below handles everything else.
    uint_fast8_t pop_count, msglen = len > FV2_MAX ? FV2_MAX : len;
    uint_fast8_t v2_consumed;
    int_fast8_t v2_ret = console_v2_try_rx(receive_buf, msglen, &v2_consumed);
    if (v2_ret) {
        if (v2_ret < 0) {
            // A v2 frame is mid-arrival; wait for the rest.
            receive_pos = len;
            return;
        }
        len -= v2_consumed;
        if (len) {
            memmove(receive_buf, &receive_buf[v2_consumed], len);
            sched_wake_task(&console_wake);
        }
        receive_pos = len;
        return;
    }
#else
    uint_fast8_t pop_count, msglen = len > MESSAGE_MAX ? MESSAGE_MAX : len;
#endif
    ret = command_find_and_dispatch(receive_buf, msglen, &pop_count);
    if (ret) {
        len -= pop_count;
        if (len) {
            memmove(receive_buf, &receive_buf[pop_count], len);
            sched_wake_task(&console_wake);
        }
    }
    receive_pos = len;
}
DECL_TASK(console_task);

// Encode and transmit a "response" message
void
console_sendf(const struct command_encoder *ce, va_list args)
{
    if (is_udp) {
        udp_console_sendf(ce, args);
        return;
    }

    // Generate message
#if CONFIG_WANT_CONSOLE_FRAMING_V2
    // A v2 (BCH) frame is up to 2 bytes longer than the v1 frame it wraps.
    uint8_t buf[FV2_MAX];
#else
    uint8_t buf[MESSAGE_MAX];
#endif
    uint_fast8_t msglen = command_encode_and_frame(buf, MESSAGE_MAX, ce, args);
    if (!msglen)
        return;
#if CONFIG_WANT_CONSOLE_FRAMING_V2
    // Once the link has latched to v2, re-frame the v1 reply in place.
    msglen = console_v2_wrap_tx(buf, msglen, sizeof(buf));
#endif

    // Transmit message
    int ret = write(main_pfd[MP_TTY_IDX].fd, buf, msglen);
    if (ret < 0)
        report_errno("write", ret);
}

// Sleep until a signal received (waking early for console input if needed)
void
console_sleep(sigset_t *sigset)
{
    int ret = ppoll(main_pfd, ARRAY_SIZE(main_pfd), NULL, sigset);
    if (ret <= 0) {
        if (errno != EINTR)
            report_errno("ppoll main_pfd", ret);
        return;
    }
    if (main_pfd[MP_TTY_IDX].revents) {
        if (is_udp)
            udp_console_note_rx();
        else
            sched_wake_task(&console_wake);
    }
}
