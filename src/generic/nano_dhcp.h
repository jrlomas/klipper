#ifndef __GENERIC_NANO_DHCP_H
#define __GENERIC_NANO_DHCP_H

#include <stdint.h>
#include "network_config.h"

#define NANO_DHCP_CLIENT_PORT 68
#define NANO_DHCP_SERVER_PORT 67
#define NANO_DHCP_DISCOVER 1
#define NANO_DHCP_OFFER 2
#define NANO_DHCP_REQUEST 3
#define NANO_DHCP_ACK 5
#define NANO_DHCP_NAK 6
#define NANO_DHCP_MIN_LEN 240

struct nano_dhcp_message {
    uint8_t type;
    uint32_t xid;
    uint32_t yiaddr;
    struct helix_dhcp_lease lease;
};

uint32_t nano_dhcp_build(uint8_t *out, uint32_t cap,
                         const uint8_t mac[6], uint8_t type, uint32_t xid,
                         uint32_t requested_ip, uint32_t server);
int nano_dhcp_parse(struct nano_dhcp_message *message,
                    const uint8_t *data, uint32_t length,
                    const uint8_t mac[6], uint32_t xid);

#endif
