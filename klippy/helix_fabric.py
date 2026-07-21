"""Transport-neutral CAN-fabric endpoint owned by Klippy.

The endpoint keeps physical transport, real board identity, profile state, and
conservation status together.  AF_CAN remains the default projection for USB;
an authenticated Ethernet proxy may project the same fabric without changing
MCU identity or the serial protocol above this boundary.
"""

import copy


STATUS_SCHEMA_VERSION = 1
BACKENDS = frozenset(('af_can', 'authenticated_udp'))


class CanFabricEndpoint:
    def __init__(self, name, interface, backend='af_can'):
        if backend not in BACKENDS:
            raise ValueError("unknown CAN fabric backend '%s'" % backend)
        self.name = name
        self.interface = interface
        self.backend = backend
        self.profile = 'CLASSIC_1M'
        self.profile_epoch = 0
        self.owner_epoch = 0
        self.nodes = []
        self.health = {}
        self.generation = 0

    def add_node(self, board_id):
        if not isinstance(board_id, str) or ':' not in board_id:
            raise ValueError('CAN fabric nodes require canonical board_id')
        if board_id in self.nodes:
            raise ValueError('duplicate CAN fabric board_id')
        self.nodes.append(board_id)
        self.generation += 1

    def activate_profile(self, profile, epoch):
        self.profile = profile
        self.profile_epoch = epoch
        self.generation += 1

    def set_owner(self, epoch):
        if epoch != self.owner_epoch:
            self.owner_epoch = epoch
            self.generation += 1

    def update_health(self, status):
        status = dict(status)
        if status != self.health:
            self.health = status
            self.generation += 1

    def status(self):
        health = copy.deepcopy(self.health)
        accepted = health.get('hw_rx_frames')
        forwarded = health.get('host_forwarded_frames',
                               health.get('usb_forwarded_frames'))
        drops = health.get('rx_queue_drops')
        depth = health.get('rx_queue_depth')
        residual = None
        if None not in (accepted, forwarded, drops, depth):
            residual = (accepted - forwarded - drops - depth) & 0xffffffff
        return {
            'schema_version': STATUS_SCHEMA_VERSION,
            'name': self.name,
            'backend': self.backend,
            'interface': self.interface,
            'profile': self.profile,
            'profile_epoch': self.profile_epoch,
            'owner_epoch': self.owner_epoch,
            'generation': self.generation,
            'nodes': list(self.nodes),
            'health': health,
            'conservation': {
                'accepted': accepted, 'forwarded': forwarded,
                'dropped': drops, 'queued': depth, 'residual': residual,
            },
        }
