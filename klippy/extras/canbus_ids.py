# Support for tracking canbus node ids
#
# Copyright (C) 2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import canbus_identity

NODEID_FIRST = 4

class PrinterCANBus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.ids = {}
        self.next_ids = {}
        self.legacy_by_board = {}
    def _add_identity(self, config, identity, canbus_iface):
        key = (canbus_iface, identity)
        if key in self.ids:
            raise config.error("Duplicate CAN identity %s on %s"
                               % (identity, canbus_iface))
        new_id = self.next_ids.get(canbus_iface, NODEID_FIRST)
        if new_id > 63:
            raise config.error("Too many CAN nodes on %s" % (canbus_iface,))
        self.next_ids[canbus_iface] = new_id + 1
        self.ids[key] = new_id
        return new_id
    def add_uuid(self, config, canbus_uuid, canbus_iface):
        return self._add_identity(config, canbus_uuid.lower(), canbus_iface)
    def add_board_id(self, config, board_id, canbus_iface):
        try:
            board_id = canbus_identity.normalize_board_id(board_id)
        except canbus_identity.IdentityError as exc:
            raise config.error(str(exc))
        self._add_identity(config, board_id, canbus_iface)
        return board_id
    def get_nodeid(self, identity, canbus_iface='can0'):
        key = (canbus_iface, identity.lower())
        if key not in self.ids:
            raise self.printer.config_error("Unknown CAN identity %s on %s"
                                            % (identity, canbus_iface))
        return self.ids[key]
    def resolve_legacy_handle(self, identity, canbus_iface='can0'):
        identity = identity.lower()
        if ':' not in identity:
            return identity
        key = (canbus_iface, identity)
        if key not in self.legacy_by_board:
            try:
                nodes = canbus_identity.scan_bus(canbus_iface)
            except canbus_identity.IdentityError as exc:
                raise self.printer.config_error(str(exc))
            for node in nodes:
                board_id = node['board_id']
                if board_id is not None:
                    self.legacy_by_board[(canbus_iface, board_id)] = \
                        node['legacy_uuid']
        if key not in self.legacy_by_board:
            raise self.printer.config_error(
                "CAN board_id %s was not found on %s"
                % (identity, canbus_iface))
        return self.legacy_by_board[key]

def load_config(config):
    return PrinterCANBus(config)
