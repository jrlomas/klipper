"""Transactional native-Ethernet provisioning from printer.cfg."""

import ipaddress
import logging
import secrets

import mcu


MODE = {'static': 0, 'dhcp': 1}
RESPONSE = (
    'eth_network_config result=%c state=%c mode=%c ip=%u netmask=%u'
    ' gateway=%u port=%hu epoch=%u generation=%u dhcp_state=%c'
    ' rejected=%u dhcp_malformed=%u dhcp_naks=%u dhcp_retries=%u')


def _ipv4(value, name):
    try:
        return int(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise ValueError("invalid %s: %s" % (name, exc))


class HelixNetwork:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.mcu_name = config.get('mcu', 'mcu')
        self.mcu = mcu.get_printer_mcu(self.printer, self.mcu_name)
        self.mode = config.getchoice('mode', MODE, 'dhcp')
        self.mode_name = 'dhcp' if self.mode else 'static'
        try:
            self.ip = _ipv4(config.get('ip', '192.168.0.254'), 'ip')
            self.netmask = _ipv4(
                config.get('netmask', '255.255.255.0'), 'netmask')
            self.gateway = _ipv4(config.get('gateway', '0.0.0.0'),
                                 'gateway')
        except ValueError as exc:
            raise config.error(str(exc))
        self.port = config.getint('port', 41415, minval=1, maxval=65535)
        self.apply_on_ready = config.getboolean('apply_on_ready', False)
        self.prepare_cmd = self.commit_cmd = self.abort_cmd = None
        self.status_cmd = None
        self.last_status = {
            'schema_version': 1, 'state': 'not_configured',
            'mode': self.mode_name, 'ip': self.ip,
            'netmask': self.netmask, 'gateway': self.gateway,
            'port': self.port, 'epoch': 0, 'generation': 0,
            'dhcp_state': 0, 'rejected': 0, 'dhcp_malformed': 0,
            'dhcp_naks': 0, 'dhcp_retries': 0,
        }
        self.mcu.register_config_callback(self._build_config)
        if self.apply_on_ready:
            self.printer.register_event_handler('klippy:ready', self._ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            'HELIX_NETWORK_APPLY', 'NETWORK', self.name,
            self.cmd_HELIX_NETWORK_APPLY,
            desc='Atomically apply a Helix native-Ethernet configuration')
        gcode.register_mux_command(
            'HELIX_NETWORK_STATUS', 'NETWORK', self.name,
            self.cmd_HELIX_NETWORK_STATUS,
            desc='Report Helix native-Ethernet and DHCP state')
        gcode.register_mux_command(
            'HELIX_NETWORK_ABORT', 'NETWORK', self.name,
            self.cmd_HELIX_NETWORK_ABORT,
            desc='Abort a staged Helix native-Ethernet configuration')

    def _build_config(self):
        cq = self.mcu.alloc_command_queue()
        self.prepare_cmd = self.mcu.lookup_query_command(
            'eth_network_prepare epoch=%u mode=%c ip=%u netmask=%u'
            ' gateway=%u port=%hu', RESPONSE, cq=cq)
        self.commit_cmd = self.mcu.lookup_query_command(
            'eth_network_commit epoch=%u', RESPONSE, cq=cq)
        self.abort_cmd = self.mcu.lookup_query_command(
            'eth_network_abort epoch=%u', RESPONSE, cq=cq)
        self.status_cmd = self.mcu.lookup_query_command(
            'eth_network_get_status', RESPONSE, cq=cq)

    def _normalize(self, params):
        state = {0: 'active', 1: 'prepared', 2: 'committed'}.get(
            params['state'], 'error')
        return {
            'schema_version': 1, 'state': state,
            'result': params['result'],
            'mode': 'dhcp' if params['mode'] else 'static',
            'ip': params['ip'], 'netmask': params['netmask'],
            'gateway': params['gateway'], 'port': params['port'],
            'epoch': params['epoch'], 'generation': params['generation'],
            'dhcp_state': params['dhcp_state'],
            'rejected': params['rejected'],
            'dhcp_malformed': params['dhcp_malformed'],
            'dhcp_naks': params['dhcp_naks'],
            'dhcp_retries': params['dhcp_retries'],
        }

    def apply(self):
        if self.prepare_cmd is None:
            raise self.printer.command_error(
                'native-Ethernet MCU is not identified')
        epoch = secrets.randbits(32) or 1
        try:
            prepared = self.prepare_cmd.send([
                epoch, self.mode, self.ip, self.netmask,
                self.gateway, self.port])
        except Exception as exc:
            self._abort_epoch(epoch)
            raise self.printer.command_error(
                'native-Ethernet configuration prepare failed: %s' % exc)
        if prepared['result'] or prepared['state'] != 1:
            self._abort_epoch(epoch)
            raise self.printer.command_error(
                'native-Ethernet configuration prepare failed')
        try:
            committed = self.commit_cmd.send([epoch])
        except Exception as exc:
            self._abort_epoch(epoch)
            raise self.printer.command_error(
                'native-Ethernet configuration commit failed: %s' % exc)
        if committed['result'] or committed['state'] != 2:
            self._abort_epoch(epoch)
            raise self.printer.command_error(
                'native-Ethernet configuration commit failed')
        self.last_status = self._normalize(committed)
        return dict(self.last_status)

    def _abort_epoch(self, epoch):
        try:
            if self.abort_cmd is not None:
                self.abort_cmd.send([epoch])
        except Exception:
            logging.exception(
                'Unable to abort native-Ethernet configuration epoch %u',
                epoch)

    def _ready(self):
        self.apply()

    def cmd_HELIX_NETWORK_APPLY(self, gcmd):
        status = self.apply()
        gcmd.respond_info(
            "Helix network '%s' committed epoch=%u mode=%s generation=%u"
            % (self.name, status['epoch'], status['mode'],
               status['generation']))

    def cmd_HELIX_NETWORK_STATUS(self, gcmd):
        if self.status_cmd is not None:
            self.last_status = self._normalize(self.status_cmd.send())
        status = self.last_status
        gcmd.respond_info(
            "Helix network '%s': state=%s mode=%s ip=%s netmask=%s "
            "gateway=%s port=%u epoch=%u generation=%u dhcp_state=%u "
            "rejected=%u malformed=%u naks=%u retries=%u" % (
                self.name, status['state'], status['mode'],
                ipaddress.IPv4Address(status['ip']),
                ipaddress.IPv4Address(status['netmask']),
                ipaddress.IPv4Address(status['gateway']), status['port'],
                status['epoch'], status['generation'], status['dhcp_state'],
                status['rejected'], status['dhcp_malformed'],
                status['dhcp_naks'], status['dhcp_retries']))

    def cmd_HELIX_NETWORK_ABORT(self, gcmd):
        epoch = gcmd.get_int('EPOCH', self.last_status['epoch'], minval=0)
        if self.abort_cmd is not None:
            self.last_status = self._normalize(self.abort_cmd.send([epoch]))
        gcmd.respond_info("Helix network '%s' staging aborted" % self.name)

    def get_status(self, eventtime):
        return dict(self.last_status)


def load_config_prefix(config):
    return HelixNetwork(config)
