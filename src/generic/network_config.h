#ifndef __GENERIC_NETWORK_CONFIG_H
#define __GENERIC_NETWORK_CONFIG_H

#include <stdint.h>

enum helix_network_mode {
    HELIX_NETWORK_STATIC = 0,
    HELIX_NETWORK_DHCP = 1,
};

enum helix_dhcp_state {
    HELIX_DHCP_DISABLED = 0,
    HELIX_DHCP_SELECTING = 1,
    HELIX_DHCP_REQUESTING = 2,
    HELIX_DHCP_BOUND = 3,
    HELIX_DHCP_RENEWING = 4,
    HELIX_DHCP_REBINDING = 5,
    HELIX_DHCP_FALLBACK = 6,
};

enum helix_dhcp_action {
    HELIX_DHCP_ACTION_NONE = 0,
    HELIX_DHCP_ACTION_DISCOVER = 1,
    HELIX_DHCP_ACTION_REQUEST = 2,
    HELIX_DHCP_ACTION_RENEW = 3,
    HELIX_DHCP_ACTION_REBIND = 4,
    HELIX_DHCP_ACTION_EXPIRE = 5,
    HELIX_DHCP_ACTION_FALLBACK = 6,
};

struct helix_network_params {
    uint8_t mode;
    uint32_t ip;
    uint32_t netmask;
    uint32_t gateway;
    uint16_t port;
};

struct helix_network_config {
    struct helix_network_params active;
    struct helix_network_params staged;
    uint32_t active_epoch;
    uint32_t staged_epoch;
    uint32_t generation;
    uint32_t rejected;
    uint8_t staged_valid;
    uint8_t apply_pending;
};

struct helix_dhcp_lease {
    uint32_t ip;
    uint32_t netmask;
    uint32_t gateway;
    uint32_t server;
    uint32_t lease_ms;
    uint32_t t1_ms;
    uint32_t t2_ms;
};

struct helix_dhcp_client {
    struct helix_dhcp_lease lease;
    struct helix_network_params fallback;
    uint32_t xid;
    uint32_t started_ms;
    uint32_t deadline_ms;
    uint32_t renew_ms;
    uint32_t rebind_ms;
    uint32_t expire_ms;
    uint32_t offered_ip;
    uint32_t offered_server;
    uint32_t retries;
    uint32_t malformed;
    uint32_t naks;
    uint8_t state;
};

int helix_network_params_valid(const struct helix_network_params *params);
void helix_network_config_init(struct helix_network_config *config,
                               const struct helix_network_params *initial);
int helix_network_prepare(struct helix_network_config *config, uint32_t epoch,
                          const struct helix_network_params *params);
int helix_network_commit(struct helix_network_config *config, uint32_t epoch);
void helix_network_abort(struct helix_network_config *config, uint32_t epoch);
int helix_network_take_apply(struct helix_network_config *config,
                             struct helix_network_params *params,
                             uint32_t *epoch);

void helix_dhcp_start(struct helix_dhcp_client *client, uint32_t now_ms,
                      uint32_t xid,
                      const struct helix_network_params *fallback);
uint8_t helix_dhcp_poll(struct helix_dhcp_client *client, uint32_t now_ms);
int helix_dhcp_offer(struct helix_dhcp_client *client, uint32_t xid,
                     uint32_t offered_ip, uint32_t server, uint32_t now_ms);
int helix_dhcp_ack(struct helix_dhcp_client *client, uint32_t xid,
                   const struct helix_dhcp_lease *lease, uint32_t now_ms);
void helix_dhcp_nak(struct helix_dhcp_client *client, uint32_t xid,
                    uint32_t now_ms);

#endif
