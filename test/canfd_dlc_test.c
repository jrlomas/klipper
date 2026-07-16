#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "generic/canbus.h"

int
main(void)
{
    static const uint8_t lengths[16] = {
        0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64
    };
    for (uint8_t dlc = 0; dlc < 16; dlc++) {
        assert(canbus_dlc_to_len(dlc) == lengths[dlc]);
        assert(canbus_len_to_dlc(lengths[dlc]) == dlc);
    }
    for (uint8_t len = 0; len <= 64; len++) {
        uint8_t wire_len = canbus_dlc_to_len(canbus_len_to_dlc(len));
        assert(wire_len >= len);
        if (len)
            assert(canbus_dlc_to_len(canbus_len_to_dlc(len - 1)) <= wire_len);
    }
    struct canbus_msg msg = {};
    msg.dlc = 64;
    msg.flags = CANMSG_FLAG_FD | CANMSG_FLAG_BRS;
    assert(CANMSG_DATA_LEN(&msg) == 64);
    assert(sizeof(msg.data) == 64);
    puts("PASS: CAN FD length/DLC mappings cover 0..64 bytes");
    return 0;
}
