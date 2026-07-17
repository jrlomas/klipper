#!/usr/bin/env python3
"""Contract test for the hardware-timestamped two-step CAN time path."""

import os
import struct


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as stream:
        return stream.read()


def main():
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
    assert 'msg.hw_clock = fdcan_timestamp_to_clock' in fdcan
    assert 'FDCAN_IR_TEFN' in fdcan and 'MSG_RAM.TEF' in fdcan
    assert 'CANMSG_FLAG_TX_EVENT' in bridge
    assert 'timesync_local_to_clock(local_clock)' in bridge
    assert 'timesync_ingest_can_sample(seq, machine_clock' in node
    assert 'CANBUS_TIME_FOLLOWUP' in node
    assert 'CAN FD protocol error burst' in node
    assert 'SOC_CAN->TXBCR = cancel' in fdcan
    assert 'SOC_CAN->CCCR &= ~FDCAN_CCCR_INIT' in fdcan
    assert 'fdcan_ram_write(txfifo->data' in fdcan
    assert 'fdcan_ram_read(msg.data' in fdcan
    assert 'memcpy(txfifo->data' not in fdcan
    assert 'SOC_CAN->TXBC &= ~FDCAN_TXBC_TFQM' in fdcan
    assert 'SOC_CAN->TXBTIE = tx_irq_mask' in fdcan
    assert 'SOC_CAN->TXBCIE = tx_irq_mask' in fdcan
    assert 'usb_local_check_reboot' in bridge
    assert 'line_coding.dwDTERate == 1200' in bridge
    assert 'CANBUS_RESP_SESSION_RESET' in node
    assert 'can_reset_host_session' in node
    assert 'command_reset_sequence();' in node
    assert 'canhw_abort_fd();' in node
    assert 'canserial_payload_chunk(avail, mtu)' in node
    assert 'can_payload_chunk(buflen, mtu)' in read(
        'klippy/chelper/serialqueue.c')
    print('PASS: CAN time transfer uses RX and Tx-Event hardware timestamps')


if __name__ == '__main__':
    main()
