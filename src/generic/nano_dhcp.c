// Bounded DHCPv4 codec for the native-RMII nano UDP transport.

#include <string.h>
#include "nano_dhcp.h"

#define DHCP_MAGIC 0x63825363u
#define OPT_PAD 0
#define OPT_SUBNET 1
#define OPT_ROUTER 3
#define OPT_REQ_IP 50
#define OPT_LEASE 51
#define OPT_TYPE 53
#define OPT_SERVER 54
#define OPT_PARAM_REQ 55
#define OPT_T1 58
#define OPT_T2 59
#define OPT_CLIENT_ID 61
#define OPT_END 255

static uint32_t
rd_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
           | ((uint32_t)p[2] << 8) | p[3];
}

static void
wr_be32(uint8_t *p, uint32_t value)
{
    p[0] = value >> 24;
    p[1] = value >> 16;
    p[2] = value >> 8;
    p[3] = value;
}

static int
put_option(uint8_t *out, uint32_t cap, uint32_t *pos, uint8_t code,
           const uint8_t *data, uint8_t length)
{
    if (*pos + 2u + length > cap)
        return -1;
    out[(*pos)++] = code;
    out[(*pos)++] = length;
    if (length)
        memcpy(out + *pos, data, length);
    *pos += length;
    return 0;
}

uint32_t
nano_dhcp_build(uint8_t *out, uint32_t cap, const uint8_t mac[6],
                 uint8_t type, uint32_t xid, uint32_t requested_ip,
                 uint32_t server)
{
    if (!out || !mac || cap < NANO_DHCP_MIN_LEN + 32
        || (type != NANO_DHCP_DISCOVER && type != NANO_DHCP_REQUEST)
        || !xid)
        return 0;
    memset(out, 0, NANO_DHCP_MIN_LEN);
    out[0] = 1; // BOOTREQUEST
    out[1] = 1; // Ethernet
    out[2] = 6;
    wr_be32(out + 4, xid);
    out[10] = 0x80; // broadcast flag
    memcpy(out + 28, mac, 6);
    wr_be32(out + 236, DHCP_MAGIC);
    uint32_t pos = NANO_DHCP_MIN_LEN;
    uint8_t client_id[7] = {1};
    memcpy(client_id + 1, mac, 6);
    static const uint8_t requested[] = {
        OPT_SUBNET, OPT_ROUTER, OPT_LEASE, OPT_SERVER, OPT_T1, OPT_T2,
    };
    if (put_option(out, cap, &pos, OPT_TYPE, &type, 1)
        || put_option(out, cap, &pos, OPT_CLIENT_ID,
                      client_id, sizeof(client_id))
        || put_option(out, cap, &pos, OPT_PARAM_REQ,
                      requested, sizeof(requested)))
        return 0;
    if (type == NANO_DHCP_REQUEST) {
        uint8_t value[4];
        if (!requested_ip)
            return 0;
        wr_be32(value, requested_ip);
        if (put_option(out, cap, &pos, OPT_REQ_IP, value, sizeof(value)))
            return 0;
        if (server) {
            wr_be32(value, server);
            if (put_option(out, cap, &pos, OPT_SERVER,
                           value, sizeof(value)))
                return 0;
        }
    }
    if (pos >= cap)
        return 0;
    out[pos++] = OPT_END;
    return pos;
}

static uint32_t
seconds_to_ms(uint32_t seconds)
{
    return seconds > 0xffffffffu / 1000u ? 0xffffffffu : seconds * 1000u;
}

int
nano_dhcp_parse(struct nano_dhcp_message *message, const uint8_t *data,
                uint32_t length, const uint8_t mac[6], uint32_t xid)
{
    if (!message || !data || !mac || length < NANO_DHCP_MIN_LEN
        || data[0] != 2 || data[1] != 1 || data[2] != 6
        || rd_be32(data + 4) != xid || memcmp(data + 28, mac, 6)
        || rd_be32(data + 236) != DHCP_MAGIC)
        return -1;
    memset(message, 0, sizeof(*message));
    message->xid = xid;
    message->yiaddr = rd_be32(data + 16);
    message->lease.ip = message->yiaddr;
    uint32_t pos = NANO_DHCP_MIN_LEN;
    uint8_t ended = 0;
    while (pos < length) {
        uint8_t code = data[pos++];
        if (code == OPT_PAD)
            continue;
        if (code == OPT_END) {
            ended = 1;
            break;
        }
        if (pos >= length)
            return -1;
        uint8_t olen = data[pos++];
        if (pos + olen > length)
            return -1;
        const uint8_t *value = data + pos;
        if (code == OPT_TYPE && olen == 1)
            message->type = value[0];
        else if (code == OPT_SUBNET && olen == 4)
            message->lease.netmask = rd_be32(value);
        else if (code == OPT_ROUTER && olen >= 4)
            message->lease.gateway = rd_be32(value);
        else if (code == OPT_SERVER && olen == 4)
            message->lease.server = rd_be32(value);
        else if (code == OPT_LEASE && olen == 4)
            message->lease.lease_ms = seconds_to_ms(rd_be32(value));
        else if (code == OPT_T1 && olen == 4)
            message->lease.t1_ms = seconds_to_ms(rd_be32(value));
        else if (code == OPT_T2 && olen == 4)
            message->lease.t2_ms = seconds_to_ms(rd_be32(value));
        pos += olen;
    }
    if (!ended || (message->type != NANO_DHCP_OFFER
                   && message->type != NANO_DHCP_ACK
                   && message->type != NANO_DHCP_NAK))
        return -1;
    if (message->type != NANO_DHCP_NAK && !message->yiaddr)
        return -1;
    return 0;
}
