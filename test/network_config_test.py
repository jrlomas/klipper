#!/usr/bin/env python3
"""Compile and exercise atomic IPv4 configuration and DHCP state."""

import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "generic/network_config.h"
#include "generic/nano_dhcp.h"

static void put32(uint8_t *p, uint32_t v) {
  p[0] = v >> 24; p[1] = v >> 16; p[2] = v >> 8; p[3] = v;
}

static unsigned make_reply(uint8_t *out, const uint8_t mac[6], uint32_t xid,
                           uint8_t type, uint32_t ip, uint32_t server) {
  memset(out, 0, 300); out[0] = 2; out[1] = 1; out[2] = 6;
  put32(out + 4, xid); put32(out + 16, ip); memcpy(out + 28, mac, 6);
  put32(out + 236, 0x63825363); unsigned p = 240;
  out[p++] = 53; out[p++] = 1; out[p++] = type;
  out[p++] = 54; out[p++] = 4; put32(out + p, server); p += 4;
  if (type != NANO_DHCP_NAK) {
    out[p++] = 1; out[p++] = 4; put32(out + p, 0xffffff00); p += 4;
    out[p++] = 3; out[p++] = 4; put32(out + p, 0xc0a80101); p += 4;
    out[p++] = 51; out[p++] = 4; put32(out + p, 120); p += 4;
    out[p++] = 58; out[p++] = 4; put32(out + p, 60); p += 4;
    out[p++] = 59; out[p++] = 4; put32(out + p, 105); p += 4;
  }
  out[p++] = 255; return p;
}

int main(void) {
  struct helix_network_params initial = {
    HELIX_NETWORK_STATIC, 0xc0a80164, 0xffffff00, 0xc0a80101, 41415};
  struct helix_network_params replacement = {
    HELIX_NETWORK_STATIC, 0x0a00002a, 0xffffff00, 0x0a000001, 41415};
  struct helix_network_config config;
  helix_network_config_init(&config, &initial);
  assert(config.active.ip == initial.ip);
  assert(!helix_network_prepare(&config, 7, &replacement));
  assert(!helix_network_prepare(&config, 7, &replacement));
  assert(helix_network_prepare(&config, 8, &initial));
  assert(helix_network_commit(&config, 8));
  assert(!helix_network_commit(&config, 7));
  struct helix_network_params applied; uint32_t epoch;
  assert(helix_network_take_apply(&config, &applied, &epoch));
  assert(epoch == 7 && applied.ip == replacement.ip);
  assert(!helix_network_take_apply(&config, &applied, &epoch));

  struct helix_dhcp_client client;
  helix_dhcp_start(&client, 100, 0x12345678, &initial);
  struct helix_dhcp_lease lease = {
    0xc0a80180, 0xffffff00, 0xc0a80101, 0xc0a80101,
    120000, 60000, 105000};
  assert(helix_dhcp_ack(&client, 0x12345678, &lease, 100));
  assert(helix_dhcp_poll(&client, 100) == HELIX_DHCP_ACTION_DISCOVER);
  assert(helix_dhcp_poll(&client, 500) == HELIX_DHCP_ACTION_NONE);
  assert(!helix_dhcp_offer(&client, 0x12345678, 0xc0a80180,
                           0xc0a80101, 600));
  assert(helix_dhcp_poll(&client, 600) == HELIX_DHCP_ACTION_REQUEST);
  assert(!helix_dhcp_ack(&client, 0x12345678, &lease, 700));
  assert(client.state == HELIX_DHCP_BOUND);
  assert(helix_dhcp_poll(&client, 60700) == HELIX_DHCP_ACTION_RENEW);
  assert(helix_dhcp_poll(&client, 105700) == HELIX_DHCP_ACTION_REBIND);
  assert(helix_dhcp_poll(&client, 120700) == HELIX_DHCP_ACTION_EXPIRE);
  helix_dhcp_nak(&client, 0x12345678, 121000);
  assert(client.state == HELIX_DHCP_SELECTING);

  struct helix_dhcp_client rebind;
  helix_dhcp_start(&rebind, 0, 22, &initial);
  assert(helix_dhcp_poll(&rebind, 0) == HELIX_DHCP_ACTION_DISCOVER);
  assert(!helix_dhcp_offer(&rebind, 22, lease.ip, lease.server, 1));
  assert(helix_dhcp_poll(&rebind, 1) == HELIX_DHCP_ACTION_REQUEST);
  assert(!helix_dhcp_ack(&rebind, 22, &lease, 2));
  assert(helix_dhcp_poll(&rebind, 105002) == HELIX_DHCP_ACTION_RENEW);
  assert(helix_dhcp_poll(&rebind, 106002) == HELIX_DHCP_ACTION_REBIND);
  struct helix_dhcp_lease replacement_lease = lease;
  replacement_lease.server = 0xc0a80102;
  assert(!helix_dhcp_ack(&rebind, 22, &replacement_lease, 106003));
  assert(rebind.state == HELIX_DHCP_BOUND);
  assert(rebind.offered_server == replacement_lease.server);

  struct helix_dhcp_client fallback;
  helix_dhcp_start(&fallback, 0, 9, &initial);
  assert(helix_dhcp_poll(&fallback, 30000) == HELIX_DHCP_ACTION_FALLBACK);
  assert(fallback.state == HELIX_DHCP_FALLBACK);

  const uint8_t mac[6] = {2, 1, 2, 3, 4, 5};
  uint8_t wire[400];
  unsigned n = nano_dhcp_build(wire, sizeof(wire), mac,
                                NANO_DHCP_DISCOVER, 0x12345678, 0, 0);
  assert(n > NANO_DHCP_MIN_LEN && wire[0] == 1);
  n = nano_dhcp_build(wire, sizeof(wire), mac, NANO_DHCP_REQUEST,
                       0x12345678, 0xc0a80180, 0xc0a80101);
  assert(n > NANO_DHCP_MIN_LEN);
  n = make_reply(wire, mac, 0x12345678, NANO_DHCP_ACK,
                 0xc0a80180, 0xc0a80101);
  struct nano_dhcp_message message;
  assert(!nano_dhcp_parse(&message, wire, n, mac, 0x12345678));
  assert(message.type == NANO_DHCP_ACK);
  assert(message.lease.ip == 0xc0a80180);
  assert(message.lease.lease_ms == 120000);
  wire[n - 1] = 0; // no END option
  assert(nano_dhcp_parse(&message, wire, n, mac, 0x12345678));
  return 0;
}
'''


def main():
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, 'network.c')
        output = os.path.join(tmp, 'network')
        with open(source, 'w', encoding='utf-8') as stream:
            stream.write(SOURCE)
        subprocess.check_call([
            'cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-I' + os.path.join(ROOT, 'src'), source,
            os.path.join(ROOT, 'src/generic/network_config.c'),
            os.path.join(ROOT, 'src/generic/nano_dhcp.c'), '-o', output])
        subprocess.check_call([output])
    print('network_config_test: PASS')


if __name__ == '__main__':
    main()
