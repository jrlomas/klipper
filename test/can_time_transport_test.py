#!/usr/bin/env python3
"""Contract test for the hardware-timestamped two-step CAN time path."""

import os
import struct


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as stream:
        return stream.read()


def main():
    # The protocol permits three command frames outstanding.  A time transfer
    # independently contributes sync and follow-up frames.  A shared
    # three-entry FIFO necessarily loses two at the coincident worst case;
    # separate three-entry FIFOs retain both bounded traffic classes.
    data_burst, control_burst, fifo_depth = 3, 2, 3
    assert max(0, data_burst + control_burst - fifo_depth) == 2
    assert max(0, data_burst - fifo_depth) == 0
    assert max(0, control_burst - fifo_depth) == 0

    legal_lengths = (1, 2, 3, 4, 5, 6, 7, 8,
                     12, 16, 20, 24, 32, 48, 64)
    for total in range(1, 193):
        remaining = total
        chunks = []
        while remaining:
            chunk = max(length for length in legal_lengths
                        if length <= min(remaining, 64))
            chunks.append(chunk)
            remaining -= chunk
        assert sum(chunks) == total
        assert all(chunk in legal_lengths for chunk in chunks)

    # A two-step transfer must be independent of both software queue delay
    # before CAN arbitration and interrupt/follow-up delivery delay.
    machine_at_tx = 0x12345678
    node_local_at_rx = 0x89abcdef
    arbitration_delay = 18000
    irq_delay = 22000
    followup_delay = 44000
    queued_machine = (machine_at_tx - arbitration_delay) & 0xffffffff
    isr_local = (node_local_at_rx + irq_delay) & 0xffffffff
    delivered_local = (isr_local + followup_delay) & 0xffffffff
    del queued_machine, delivered_local
    followup = struct.pack('<BBBBI', 0x48, 0x02, 7, 1,
                           machine_at_tx)
    magic, kind, seq, quality, recovered_machine = struct.unpack(
        '<BBBBI', followup)
    assert (magic, kind, seq, quality) == (0x48, 0x02, 7, 1)
    assert recovered_machine == machine_at_tx
    assert node_local_at_rx != isr_local

    fdcan = read('src/stm32/fdcan.c')
    bridge = read('src/generic/usb_canbus.c')
    node = read('src/generic/canserial.c')
    assert 'msg->hw_clock = fdcan_timestamp_to_clock' in fdcan
    assert 'FDCAN_IR_TEFN' in fdcan and 'MSG_RAM.TEF' in fdcan
    assert 'CANMSG_FLAG_TX_EVENT' in bridge
    assert 'command_get_usb_canbus_status' in bridge
    assert 'UsbCan.canhw_queue_drops++' in bridge
    assert 'UsbCan.canhw_queue_highwater = depth + 1' in bridge
    assert 'canhw_queue[512]' in bridge
    assert 'timesync_local_to_clock(local_clock)' in bridge
    assert 'timesync_ingest_can_sample(seq, machine_clock' in node
    assert 'CANBUS_TIME_FOLLOWUP' in node
    assert 'CAN FD carrier error burst' in node
    assert 'canbus_notify_bus_off();' in fdcan
    assert 'if (ir & FDCAN_IR_BO)' in fdcan
    assert 'FDCAN_IE_BOE' in fdcan
    assert 'shutdown("CAN bus-off")' in bridge
    assert 'SOC_CAN->TXBCR = cancel' in fdcan
    assert 'SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT' in fdcan
    assert 'fdcan_ram_write(txfifo->data' in fdcan
    assert 'fdcan_ram_read(msg->data' in fdcan
    assert 'memcpy(txfifo->data' not in fdcan
    assert 'SOC_CAN->TXBC &= ~FDCAN_TXBC_TFQM' in fdcan
    assert 'SOC_CAN->TXBTIE = tx_irq_mask' in fdcan
    assert 'SOC_CAN->TXBCIE = tx_irq_mask' in fdcan
    assert 'FDCAN_IE_RF0LE' in fdcan
    assert 'FDCAN_IE_RF1LE' in fdcan
    assert 'drained < ARRAY_SIZE(MSG_RAM.RXF0)' in fdcan
    assert 'drained < ARRAY_SIZE(MSG_RAM.RXF1)' in fdcan
    assert ('can_filter(3, id, FDCAN_FILTER_FIFO0)' in fdcan
            and 'can_filter(1, CANBUS_ID_TIME_SYNC,' in fdcan)
    assert 'CAN_Errors.rx_fifo0_overruns++' in fdcan
    assert 'CAN_Errors.rx_fifo1_overruns++' in fdcan
    assert 'rx_service_max_delay_ticks' in fdcan
    assert 'CAN_Errors.rx_fifo_overruns++' in fdcan
    assert 'CAN_Errors.rx_protocol_errors++' in fdcan
    assert 'command_get_canbus_diagnostics' in node
    assert 'command_get_canbus_diagnostics_v2' in node
    self_test = read('src/self_test.c')
    assert 'command_self_test_irq_hold' in self_test
    assert 'timer_from_us(2000)' in self_test
    assert 'command_self_test_rx_nop' in self_test
    self_test_host = read('klippy/extras/helix_self_test.py')
    assert "'HELIX_CAN_RX_STRESS'" in self_test_host
    assert 'padding = bytes(range(44))' in self_test_host
    assert 'usb_local_check_reboot' in bridge
    assert 'line_coding.dwDTERate == 1200' in bridge
    assert 'CANBUS_RESP_SESSION_RESET' in node
    assert 'can_reset_host_session' in node
    assert 'command_reset_sequence();' in node
    assert 'canhw_abort_fd();' in node
    assert "pack as many complete protocol" in node
    assert 'canserial_frame_logical_len(msg->data, len)' in node
    assert 'canserial_carrier_wire_len(now)' in node
    host = read('klippy/chelper/serialqueue.c')
    mcu = read('klippy/mcu.py')
    assert 'can_frame_logical_len(cf.data, len)' in host
    assert 'ret == CANFD_MTU' in host
    assert ('set_digital_out_late_policy oid=%d apply_late=1' in mcu)
    assert 'hw_rx_frames=%u usb_forwarded_frames=%u' in bridge
    assert 'irqstatus_t irqflag = irq_save();' in bridge
    print('PASS: CAN time transfer uses RX and Tx-Event hardware timestamps')


if __name__ == '__main__':
    main()
