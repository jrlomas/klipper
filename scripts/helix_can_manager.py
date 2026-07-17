#!/usr/bin/env python3
"""Constrained rtnetlink frontend for HELIX CAN profile changes.

The service never accepts arbitrary commands. It translates an allowlisted
profile request into fixed iproute2 argv; iproute2 performs the rtnetlink
transaction without a custom kernel driver or sudo subprocess.
"""

import argparse
import grp
import json
import os
import re
import socketserver
import subprocess


PROFILES = {
    # Maintenance-only compatibility profile for retained CanBoot/Katapult
    # installations.  Helix application negotiation never selects it.
    'CLASSIC_125K': {'fd': False, 'nominal': 125000, 'data': None},
    'CLASSIC_250K': {'fd': False, 'nominal': 250000, 'data': None},
    'CLASSIC_500K': {'fd': False, 'nominal': 500000, 'data': None},
    'CLASSIC_1M': {'fd': False, 'nominal': 1000000, 'data': None},
    'FD_1M_NOBRS': {'fd': True, 'nominal': 1000000, 'data': 1000000},
    'FD_2M_BRS': {'fd': True, 'nominal': 1000000, 'data': 2000000},
    'FD_5M_BRS': {'fd': True, 'nominal': 1000000, 'data': 5000000},
    'FD_8M_BRS': {'fd': True, 'nominal': 1000000, 'data': 8000000},
}

IFACE_RE = re.compile(r'^[A-Za-z0-9_.-]{1,15}$')


class ManagerError(Exception):
    pass


class LinkManager:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def _run(self, argv, capture=False):
        result = self.runner(argv, check=False, text=True,
                             capture_output=capture, timeout=3.0)
        if result.returncode:
            detail = (result.stderr or result.stdout or '').strip()
            raise ManagerError('%s failed: %s' % (' '.join(argv), detail))
        return result.stdout if capture else ''

    def _configure(self, interface, profile_name):
        profile = PROFILES[profile_name]
        self._run(['ip', 'link', 'set', 'dev', interface, 'down'])
        argv = ['ip', 'link', 'set', 'dev', interface, 'type', 'can',
                'bitrate', str(profile['nominal']), 'restart-ms', '100']
        if profile['fd']:
            argv.extend(['dbitrate', str(profile['data']), 'fd', 'on'])
        else:
            argv.extend(['fd', 'off'])
        self._run(argv)
        self._run(['ip', 'link', 'set', 'dev', interface, 'txqueuelen',
                   '1024'])
        self._run(['ip', 'link', 'set', 'dev', interface, 'up'])

    def _readback(self, interface):
        output = self._run(['ip', '-details', '-json', 'link', 'show', 'dev',
                            interface], capture=True)
        data = json.loads(output)
        if len(data) != 1:
            raise ManagerError('interface readback was not unique')
        return data[0]

    def apply(self, interface, profile_name):
        if not IFACE_RE.match(interface):
            raise ManagerError('invalid interface name')
        if profile_name not in PROFILES:
            raise ManagerError('unsupported CAN profile')
        try:
            self._configure(interface, profile_name)
            readback = self._readback(interface)
            info = readback.get('linkinfo', {}).get('info_data', {})
            actual_nominal = info.get('bittiming', {}).get('bitrate')
            if actual_nominal is None:
                raise ManagerError('nominal bitrate missing from readback')
            if int(actual_nominal) != PROFILES[profile_name]['nominal']:
                raise ManagerError('nominal bitrate readback mismatch')
            expected_data = PROFILES[profile_name]['data']
            if expected_data is not None:
                ctrlmode = set(info.get('ctrlmode', []))
                if 'FD' not in ctrlmode:
                    raise ManagerError('CAN FD mode missing from readback')
                if int(readback.get('mtu', 0)) != 72:
                    raise ManagerError('CAN FD MTU missing from readback')
                actual_data = info.get('data_bittiming', {}).get('bitrate')
                if actual_data is None:
                    raise ManagerError('data bitrate missing from readback')
                if int(actual_data) != expected_data:
                    raise ManagerError('data bitrate readback mismatch')
            elif ('FD' in set(info.get('ctrlmode', []))
                  or int(readback.get('mtu', 0)) != 16):
                raise ManagerError('Classical CAN mode missing from readback')
            return {'ok': True, 'profile': profile_name,
                    'interface': interface, 'readback': readback}
        except Exception as original:
            if profile_name != 'CLASSIC_1M':
                try:
                    self._configure(interface, 'CLASSIC_1M')
                except Exception as rollback:
                    raise ManagerError('%s; Classical rollback also failed: %s'
                                       % (original, rollback))
            raise


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(65537)
            if not line or len(line) > 65536:
                raise ManagerError('invalid request size')
            request = json.loads(line.decode())
            if request.get('version') != 1 or request.get('action') != 'apply':
                raise ManagerError('unsupported manager request')
            interface = request.get('interface', '')
            if interface not in self.server.allowed_interfaces:
                raise ManagerError('interface is not managed by this service')
            result = self.server.manager.apply(interface,
                                               request.get('profile', ''))
        except Exception as exc:
            result = {'ok': False, 'error': str(exc)}
        self.wfile.write((json.dumps(result, sort_keys=True) + '\n').encode())


class UnixServer(socketserver.UnixStreamServer):
    def __init__(self, path, manager, allowed_interfaces):
        self.manager = manager
        self.allowed_interfaces = frozenset(allowed_interfaces)
        super().__init__(path, RequestHandler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default='/run/helix/helix-can-manager.sock')
    parser.add_argument('--interface', action='append')
    parser.add_argument('--group', default='klipper')
    args = parser.parse_args()
    os.makedirs(os.path.dirname(args.socket), mode=0o750, exist_ok=True)
    if os.path.exists(args.socket):
        os.unlink(args.socket)
    interfaces = args.interface or ['helixcan0']
    with UnixServer(args.socket, LinkManager(), interfaces) as server:
        try:
            gid = grp.getgrnam(args.group).gr_gid
        except KeyError:
            raise SystemExit("HELIX CAN manager group '%s' does not exist"
                             % (args.group,))
        os.chown(args.socket, 0, gid)
        os.chmod(args.socket, 0o660)
        server.serve_forever()


if __name__ == '__main__':
    main()
