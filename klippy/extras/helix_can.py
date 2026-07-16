"""Named HELIX CAN bus configuration and profile ownership."""

import json
import logging
import secrets
import socket


PROFILE_DATA = {
    'CLASSIC_1M': (8, False, 1000000),
    'FD_1M_NOBRS': (64, False, 1000000),
    'FD_2M_BRS': (64, True, 2000000),
    'FD_5M_BRS': (64, True, 5000000),
    'FD_8M_BRS': (64, True, 8000000),
}

PROFILE_MASKS = {
    'FD_1M_NOBRS': 1 << 0,
    'FD_2M_BRS': 1 << 1,
    'FD_5M_BRS': 1 << 2,
    'FD_8M_BRS': 1 << 3,
}


class HelixCANManagerClient:
    def __init__(self, path, timeout=2.0):
        self.path = path
        self.timeout = timeout
    def request(self, payload):
        request = dict(payload)
        request['version'] = 1
        data = (json.dumps(request, sort_keys=True) + '\n').encode()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
            sock.sendall(data)
            response = bytearray()
            while b'\n' not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 65536:
                    raise RuntimeError("HELIX CAN manager response too large")
        finally:
            sock.close()
        if not response:
            raise RuntimeError("HELIX CAN manager returned no response")
        result = json.loads(bytes(response).split(b'\n', 1)[0].decode())
        if not result.get('ok'):
            raise RuntimeError(result.get('error', 'manager request failed'))
        return result


class HelixCANBus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.interface = config.get('interface', self.name)
        self.nominal_bitrate = config.getint(
            'nominal_bitrate', 1000000, minval=1000000, maxval=1000000)
        preferred = config.get('preferred_profiles',
                               'FD_8M_BRS, FD_5M_BRS, FD_2M_BRS, '
                               'FD_1M_NOBRS')
        self.preferred_profiles = [item.strip().upper()
                                   for item in preferred.split(',')
                                   if item.strip()]
        unknown = [item for item in self.preferred_profiles
                   if item not in PROFILE_DATA]
        if unknown:
            raise config.error("Unknown HELIX CAN profile %s"
                               % (unknown[0],))
        self.require_fd = config.getboolean('require_fd', True)
        self.classic_node_policy = config.getchoice(
            'classic_node_policy', {'refuse': 'refuse',
                                    'fallback': 'fallback'}, 'refuse')
        self.required_nodes = []
        self.connections = []
        self.manager_socket = config.get(
            'manager_socket', '/run/helix/helix-can-manager.sock')
        self.manager = HelixCANManagerClient(self.manager_socket)
        self.bridge_mcu = config.get('bridge_mcu', None)
        self.bridge_is_primary = config.getboolean(
            'bridge_is_primary', False)
        self.time_beacon_us = config.getint(
            'time_beacon_us', 20000, minval=5000, maxval=1000000)
        # The manager replaces this bootstrap value before CAN MCU attach.
        self.active_profile = 'CLASSIC_1M'
        self.state = 'bootstrap'
        self.epoch = 0
        self.time_epoch = 0
        self.printer.register_event_handler('klippy:mcu_identify',
                                            self._bootstrap)
        self.printer.register_event_handler('klippy:connect', self._activate)
    def add_required_node(self, board_id):
        if board_id in self.required_nodes:
            raise self.printer.config_error(
                "Duplicate board_id %s on HELIX CAN bus %s"
                % (board_id, self.name))
        self.required_nodes.append(board_id)
    def register_connection(self, connection):
        self.connections.append(connection)
    def get_interface(self):
        return self.interface
    def get_connection_profile(self):
        mtu, brs, data_bitrate = PROFILE_DATA[self.active_profile]
        return {'name': self.active_profile, 'mtu': mtu, 'brs': brs,
                'data_bitrate': data_bitrate}
    def _manager_apply(self, profile):
        return self.manager.request({'action': 'apply',
                                     'interface': self.interface,
                                     'profile': profile})
    def _bootstrap(self):
        try:
            self._manager_apply('CLASSIC_1M')
        except Exception as exc:
            raise self.printer.config_error(
                "Unable to bootstrap HELIX CAN bus %s: %s"
                % (self.name, exc))
        self.active_profile = 'CLASSIC_1M'
        self.state = 'bootstrap'
    def _select_profile(self):
        capabilities = [conn.get_can_capabilities()
                        for conn in self.connections]
        if not capabilities:
            raise self.printer.config_error(
                "HELIX CAN bus %s has no configured nodes" % (self.name,))
        for profile in self.preferred_profiles:
            if profile == 'CLASSIC_1M':
                continue
            mask = PROFILE_MASKS[profile]
            if all(cap['fd'] and cap['max_payload'] >= 64
                   and cap['bitrate_mask'] & mask for cap in capabilities):
                return profile
        if not self.require_fd or self.classic_node_policy == 'fallback':
            return 'CLASSIC_1M'
        raise self.printer.config_error(
            "No unanimous CAN FD profile is supported on %s" % (self.name,))
    def _abort(self, connections, epoch, reason):
        for conn in connections:
            try:
                conn.abort_can_profile(epoch)
            except Exception:
                pass
        try:
            self._manager_apply('CLASSIC_1M')
        except Exception:
            pass
        self.active_profile = 'CLASSIC_1M'
        self.state = 'failed'
        self.printer.send_event('helix_can:incident', {
            'bus': self.name, 'kind': 'profile_activation_failed',
            'epoch': epoch, 'reason': str(reason)})
        logging.error('HELIX_CAN_INCIDENT %s', json.dumps({
            'bus': self.name, 'kind': 'profile_activation_failed',
            'epoch': epoch, 'reason': str(reason)}, sort_keys=True))
    def owns_bridge(self, mcu_name):
        return self.bridge_mcu == mcu_name
    def quiesce(self, reason='maintenance'):
        if self.bridge_mcu is not None and self.time_epoch:
            try:
                name = self.bridge_mcu
                if name != 'mcu':
                    name = 'mcu ' + name
                bridge = self.printer.lookup_object(name)
                stop = bridge.lookup_query_command(
                    'can_time_bridge_start epoch=%u cadence_us=%u quality=%c'
                    ' require_discipline=%c',
                    'can_time_bridge_state enabled=%c epoch=%u quality=%c'
                    ' sync_count=%u followup_count=%u invalid_count=%u')
                stop.send([self.time_epoch, 0, 0, 0])
            except Exception:
                logging.exception('Unable to stop CAN time source on %s',
                                  self.name)
        for conn in self.connections:
            try:
                conn.abort_can_profile(self.epoch)
            except Exception:
                logging.exception('Unable to quiesce CAN node on %s',
                                  self.name)
        self._manager_apply('CLASSIC_1M')
        self.active_profile = 'CLASSIC_1M'
        self.state = 'maintenance'
        self.time_epoch = 0
        self.printer.send_event('helix_can:profile_changed', {
            'bus': self.name, 'profile': 'CLASSIC_1M', 'epoch': self.epoch,
            'reason': reason})
        logging.info('HELIX CAN bus %s quiesced to Classical 1 Mbit: %s',
                     self.name, reason)
    def _start_time_source(self):
        if self.bridge_mcu is None:
            return
        name = self.bridge_mcu
        if name != 'mcu':
            name = 'mcu ' + name
        bridge = self.printer.lookup_object(name)
        command = bridge.lookup_query_command(
            'can_time_bridge_start epoch=%u cadence_us=%u quality=%c'
            ' require_discipline=%c',
            'can_time_bridge_state enabled=%c epoch=%u quality=%c'
            ' sync_count=%u followup_count=%u invalid_count=%u')
        self.time_epoch = secrets.randbits(32) or 1
        params = command.send([self.time_epoch, self.time_beacon_us, 1,
                               int(not self.bridge_is_primary)])
        if not params['enabled'] or params['epoch'] != self.time_epoch:
            raise self.printer.config_error(
                'Composite CAN bridge refused hardware time source')
    def _activate(self):
        selected = self._select_profile()
        if selected == 'CLASSIC_1M':
            self.active_profile = selected
            self.state = 'active'
            self._start_time_source()
            return
        profile = dict(self.get_connection_profile())
        mtu, brs, data_bitrate = PROFILE_DATA[selected]
        profile.update({'name': selected, 'mtu': mtu, 'brs': brs,
                        'data_bitrate': data_bitrate})
        epoch = secrets.randbits(32) or 1
        prepared = []
        try:
            self.state = 'preparing'
            for conn in self.connections:
                conn.prepare_can_profile(profile, epoch)
                prepared.append(conn)
            for conn in prepared:
                conn.commit_can_profile(profile, epoch)
            self._manager_apply(selected)
            for conn in prepared:
                conn.enable_can_profile(profile, epoch)
            self._start_time_source()
        except Exception as exc:
            self._abort(prepared, epoch, exc)
            raise self.printer.config_error(
                "HELIX CAN profile %s failed on %s: %s"
                % (selected, self.name, exc))
        self.active_profile = selected
        self.epoch = epoch
        self.state = 'active'
        self.printer.send_event('helix_can:profile_changed', {
            'bus': self.name, 'profile': selected, 'epoch': epoch})
    def get_status(self, eventtime):
        profile = self.get_connection_profile()
        return {'interface': self.interface,
                'profile': profile['name'],
                'nominal_bitrate': self.nominal_bitrate,
                'data_bitrate': profile['data_bitrate'],
                'required_nodes': list(self.required_nodes),
                'epoch': self.epoch, 'time_epoch': self.time_epoch,
                'time_source': ('usb_sof_can_timestamp'
                                if self.bridge_mcu else None),
                'state': self.state}


def load_config_prefix(config):
    return HelixCANBus(config)
