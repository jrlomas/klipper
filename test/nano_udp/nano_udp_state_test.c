// Stateful host test for the native-RMII nano UDP console adapter.
//
// In particular, prove that a second datagram dropped while the one-slot
// receive queue is occupied cannot replace the candidate return address for
// the datagram that the authenticated session is about to accept.

#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "generic/nano_udp.h"
#include "generic/udp_console.h"

static int failures;
static unsigned wakeups, emits;
static uint8_t emitted[256];
static uint32_t emitted_len;

#define CHECK(cond) do { \
    if (!(cond)) { \
        printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        failures++; \
    } \
} while (0)

void
udp_console_note_rx(void)
{
    wakeups++;
}

static int
capture_emit(const uint8_t *frame, uint32_t len)
{
    emits++;
    emitted_len = len < sizeof(emitted) ? len : sizeof(emitted);
    memcpy(emitted, frame, emitted_len);
    return 0;
}

static uint32_t
make_packet(uint8_t *frame, const uint8_t src_mac[6], uint32_t src_ip,
            uint16_t src_port, const uint8_t *payload, uint32_t payload_len)
{
    static const uint8_t our_mac[6] = { 0x02, 0x12, 0x34, 0x56, 0x78, 0x9a };
    return nano_udp_build_frame(frame, 128, src_mac, our_mac,
                                src_ip, 0xc0a80164, src_port, 4950,
                                payload, payload_len);
}

static void
check_emit_to(const uint8_t mac[6], uint32_t ip, uint16_t port)
{
    CHECK(emitted_len >= NANO_UDP_OVERHEAD);
    CHECK(memcmp(emitted, mac, 6) == 0);
    CHECK(emitted[NANO_ETH_HLEN + 16] == (uint8_t)(ip >> 24));
    CHECK(emitted[NANO_ETH_HLEN + 17] == (uint8_t)(ip >> 16));
    CHECK(emitted[NANO_ETH_HLEN + 18] == (uint8_t)(ip >> 8));
    CHECK(emitted[NANO_ETH_HLEN + 19] == (uint8_t)ip);
    CHECK(emitted[NANO_ETH_HLEN + NANO_IP_HLEN + 2] == (uint8_t)(port >> 8));
    CHECK(emitted[NANO_ETH_HLEN + NANO_IP_HLEN + 3] == (uint8_t)port);
}

int
main(void)
{
    static const uint8_t our_mac[6] = { 0x02, 0x12, 0x34, 0x56, 0x78, 0x9a };
    static const uint8_t mac_a[6] = { 0x02, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa };
    static const uint8_t mac_b[6] = { 0x02, 0xbb, 0xbb, 0xbb, 0xbb, 0xbb };
    static const uint8_t payload_a[] = { 'p', 'e', 'e', 'r', '-', 'a' };
    static const uint8_t payload_b[] = { 'p', 'e', 'e', 'r', '-', 'b' };
    static const uint8_t reply[] = { 'o', 'k' };
    uint8_t frame_a[128], frame_b[128], got[32];
    uint32_t len_a = make_packet(frame_a, mac_a, 0xc0a8010a, 4100,
                                 payload_a, sizeof(payload_a));
    uint32_t len_b = make_packet(frame_b, mac_b, 0xc0a8010b, 4200,
                                 payload_b, sizeof(payload_b));

    nano_udp_setup(our_mac, 0xc0a80164, 4950, capture_emit,
                   udp_console_note_rx);

    // IPv4 unicast for another station is ignored before parsing.
    frame_b[0] ^= 1;
    nano_udp_input(frame_b, len_b);
    CHECK(wakeups == 0);
    frame_b[0] ^= 1;

    nano_udp_input(frame_a, len_a);
    CHECK(wakeups == 1);
    nano_udp_input(frame_b, len_b);
    CHECK(wakeups == 1); // occupied slot drops B without changing candidate

    nano_udp_ops.send_candidate(NULL, reply, sizeof(reply));
    CHECK(emits == 1);
    check_emit_to(mac_a, 0xc0a8010a, 4100);

    int32_t got_len = nano_udp_ops.recv(NULL, got, sizeof(got));
    CHECK(got_len == (int32_t)sizeof(payload_a));
    CHECK(memcmp(got, payload_a, sizeof(payload_a)) == 0);
    nano_udp_ops.rx_accepted(NULL);
    nano_udp_ops.send(NULL, reply, sizeof(reply));
    CHECK(emits == 2);
    check_emit_to(mac_a, 0xc0a8010a, 4100);

    if (failures) {
        printf("nano_udp state: %d check(s) FAILED\n", failures);
        return 1;
    }
    printf("nano_udp state: all tests passed\n");
    return 0;
}
