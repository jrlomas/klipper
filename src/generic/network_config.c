// Atomic network configuration and transport-independent DHCP lease state.

#include <string.h>
#include "network_config.h"

#define DHCP_RETRY_MIN_MS 1000u
#define DHCP_RETRY_MAX_MS 8000u
#define DHCP_FALLBACK_MS 30000u

static int
time_reached(uint32_t now, uint32_t deadline)
{
    return (int32_t)(now - deadline) >= 0;
}

static int
netmask_valid(uint32_t mask)
{
    if (!mask)
        return 0;
    uint32_t inv = ~mask;
    return !(inv & (inv + 1));
}

static int
params_equal(const struct helix_network_params *a,
             const struct helix_network_params *b)
{
    return a->mode == b->mode && a->ip == b->ip
        && a->netmask == b->netmask && a->gateway == b->gateway
        && a->port == b->port;
}

int
helix_network_params_valid(const struct helix_network_params *params)
{
    if (!params || params->mode > HELIX_NETWORK_DHCP || !params->port)
        return 0;
    if (params->mode == HELIX_NETWORK_DHCP)
        return 1;
    if (!params->ip || params->ip == 0xffffffffu
        || !netmask_valid(params->netmask))
        return 0;
    if (params->gateway
        && ((params->gateway ^ params->ip) & params->netmask))
        return 0;
    uint32_t host = params->ip & ~params->netmask;
    if (!host || host == ~params->netmask)
        return 0;
    return 1;
}

void
helix_network_config_init(struct helix_network_config *config,
                          const struct helix_network_params *initial)
{
    memset(config, 0, sizeof(*config));
    if (helix_network_params_valid(initial))
        config->active = *initial;
}

int
helix_network_prepare(struct helix_network_config *config, uint32_t epoch,
                      const struct helix_network_params *params)
{
    if (!config || !epoch || !helix_network_params_valid(params)) {
        if (config)
            config->rejected++;
        return -1;
    }
    if (config->staged_valid) {
        if (config->staged_epoch == epoch
            && params_equal(&config->staged, params))
            return 0;
        config->rejected++;
        return -1;
    }
    config->staged = *params;
    config->staged_epoch = epoch;
    config->staged_valid = 1;
    return 0;
}

int
helix_network_commit(struct helix_network_config *config, uint32_t epoch)
{
    if (!config || !config->staged_valid || config->staged_epoch != epoch) {
        if (config)
            config->rejected++;
        return -1;
    }
    config->active = config->staged;
    config->active_epoch = epoch;
    config->staged_valid = 0;
    config->apply_pending = 1;
    config->generation++;
    return 0;
}

void
helix_network_abort(struct helix_network_config *config, uint32_t epoch)
{
    if (!config || !config->staged_valid)
        return;
    if (!epoch || config->staged_epoch == epoch)
        config->staged_valid = 0;
}

int
helix_network_take_apply(struct helix_network_config *config,
                         struct helix_network_params *params, uint32_t *epoch)
{
    if (!config || !config->apply_pending)
        return 0;
    if (params)
        *params = config->active;
    if (epoch)
        *epoch = config->active_epoch;
    config->apply_pending = 0;
    return 1;
}

static uint32_t
retry_delay(uint32_t retries)
{
    uint32_t shift = retries > 3 ? 3 : retries;
    uint32_t delay = DHCP_RETRY_MIN_MS << shift;
    return delay > DHCP_RETRY_MAX_MS ? DHCP_RETRY_MAX_MS : delay;
}

void
helix_dhcp_start(struct helix_dhcp_client *client, uint32_t now_ms,
                 uint32_t xid, const struct helix_network_params *fallback)
{
    memset(client, 0, sizeof(*client));
    client->state = HELIX_DHCP_SELECTING;
    client->xid = xid ? xid : 1;
    client->started_ms = now_ms;
    client->deadline_ms = now_ms;
    if (fallback)
        client->fallback = *fallback;
}

uint8_t
helix_dhcp_poll(struct helix_dhcp_client *client, uint32_t now_ms)
{
    if (!client || !time_reached(now_ms, client->deadline_ms))
        return HELIX_DHCP_ACTION_NONE;
    if ((client->state == HELIX_DHCP_SELECTING
         || client->state == HELIX_DHCP_REQUESTING)
        && time_reached(now_ms, client->started_ms + DHCP_FALLBACK_MS)
        && helix_network_params_valid(&client->fallback)
        && client->fallback.mode == HELIX_NETWORK_STATIC) {
        client->state = HELIX_DHCP_FALLBACK;
        return HELIX_DHCP_ACTION_FALLBACK;
    }
    if (client->state == HELIX_DHCP_SELECTING) {
        client->deadline_ms = now_ms + retry_delay(client->retries++);
        return HELIX_DHCP_ACTION_DISCOVER;
    }
    if (client->state == HELIX_DHCP_REQUESTING) {
        client->deadline_ms = now_ms + retry_delay(client->retries++);
        return HELIX_DHCP_ACTION_REQUEST;
    }
    if (client->state == HELIX_DHCP_BOUND
        && time_reached(now_ms, client->renew_ms)) {
        client->state = HELIX_DHCP_RENEWING;
        client->deadline_ms = now_ms + DHCP_RETRY_MIN_MS;
        return HELIX_DHCP_ACTION_RENEW;
    }
    if (client->state == HELIX_DHCP_RENEWING) {
        if (time_reached(now_ms, client->rebind_ms)) {
            client->state = HELIX_DHCP_REBINDING;
            client->deadline_ms = now_ms + DHCP_RETRY_MIN_MS;
            return HELIX_DHCP_ACTION_REBIND;
        }
        client->deadline_ms = now_ms + DHCP_RETRY_MIN_MS;
        return HELIX_DHCP_ACTION_RENEW;
    }
    if (client->state == HELIX_DHCP_REBINDING) {
        if (time_reached(now_ms, client->expire_ms)) {
            client->state = HELIX_DHCP_SELECTING;
            client->started_ms = client->deadline_ms = now_ms;
            client->retries = 0;
            memset(&client->lease, 0, sizeof(client->lease));
            return HELIX_DHCP_ACTION_EXPIRE;
        }
        client->deadline_ms = now_ms + DHCP_RETRY_MIN_MS;
        return HELIX_DHCP_ACTION_REBIND;
    }
    return HELIX_DHCP_ACTION_NONE;
}

int
helix_dhcp_offer(struct helix_dhcp_client *client, uint32_t xid,
                 uint32_t offered_ip, uint32_t server, uint32_t now_ms)
{
    if (!client || client->state != HELIX_DHCP_SELECTING
        || xid != client->xid || !offered_ip || !server) {
        if (client)
            client->malformed++;
        return -1;
    }
    client->offered_ip = offered_ip;
    client->offered_server = server;
    client->state = HELIX_DHCP_REQUESTING;
    client->deadline_ms = now_ms;
    client->retries = 0;
    return 0;
}

int
helix_dhcp_ack(struct helix_dhcp_client *client, uint32_t xid,
               const struct helix_dhcp_lease *lease, uint32_t now_ms)
{
    uint8_t state = client ? client->state : HELIX_DHCP_DISABLED;
    if (!client || !lease || xid != client->xid
        || (state != HELIX_DHCP_REQUESTING
            && state != HELIX_DHCP_RENEWING
            && state != HELIX_DHCP_REBINDING) || !lease->ip
        || !lease->lease_ms || !netmask_valid(lease->netmask)
        || (client->offered_ip && lease->ip != client->offered_ip)
        || (state != HELIX_DHCP_REBINDING && client->offered_server
            && lease->server
            && lease->server != client->offered_server)) {
        if (client)
            client->malformed++;
        return -1;
    }
    client->lease = *lease;
    client->offered_ip = lease->ip;
    if (lease->server)
        client->offered_server = lease->server;
    uint32_t t1 = lease->t1_ms ? lease->t1_ms : lease->lease_ms / 2;
    uint32_t t2 = lease->t2_ms ? lease->t2_ms
                               : lease->lease_ms - lease->lease_ms / 8;
    if (!t1 || t1 >= t2 || t2 >= lease->lease_ms) {
        client->malformed++;
        return -1;
    }
    client->renew_ms = now_ms + t1;
    client->rebind_ms = now_ms + t2;
    client->expire_ms = now_ms + lease->lease_ms;
    client->deadline_ms = client->renew_ms;
    client->state = HELIX_DHCP_BOUND;
    client->retries = 0;
    return 0;
}

void
helix_dhcp_nak(struct helix_dhcp_client *client, uint32_t xid,
               uint32_t now_ms)
{
    if (!client || xid != client->xid)
        return;
    client->naks++;
    client->state = HELIX_DHCP_SELECTING;
    client->started_ms = client->deadline_ms = now_ms;
    client->offered_ip = client->offered_server = 0;
    client->retries = 0;
    memset(&client->lease, 0, sizeof(client->lease));
}
