#!/usr/bin/env python3
"""Source-contract regression for STM32 USB-SOF discard windows."""

import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(ROOT, path), encoding='utf-8') as source:
        return source.read()


def _function(source, name):
    match = re.search(
        r'\n%s\(.*?\n\{(?P<body>.*?)\n\}' % re.escape(name),
        source, re.S)
    assert match is not None, name
    return match.group('body')


def test_arm_irq_discards_before_restoring_primask():
    source = _read('src/generic/armcm_irq.c')
    disable = _function(source, 'irq_disable')
    enable = _function(source, 'irq_enable')
    restore = _function(source, 'irq_restore')
    wait = _function(source, 'irq_wait')

    assert 'irq_timing_discard' not in disable
    assert enable.index('irq_timing_discard_pending()') < enable.index(
        'cpsie i')
    assert restore.index('irq_timing_discard_pending()') < restore.index(
        'msr primask')
    assert wait.index('irq_timing_discard_pending()') < wait.index('cpsie i')
    print("PASS: pending SOF is discarded before every ARM PRIMASK restore")


def test_stm32_guard_discards_only_sof():
    source = _read('src/stm32/usbfs.c')
    generic = _read('src/generic/usb_sof.c')
    mask = _function(source, 'usb_irq_mask')
    discard = _function(source, 'usb_sof_board_discard_pending')
    query = _function(generic, 'command_usb_sof_query')

    assert 'USB_CNTR_CTRM | USB_CNTR_RESETM' in mask
    assert 'usb_sof_enabled ? USB_CNTR_SOFM : 0' in mask
    assert 'USB->ISTR & USB_ISTR_SOF' in discard
    assert 'mrs %0, primask' in discard
    assert 'USB->FNR & USB_FNR_FN' in discard
    assert '~USB_ISTR_SOF' in discard
    assert discard.index('~USB_ISTR_SOF') < discard.index(
        'usb_sof_note_discard')
    assert 'USB->CNTR' not in discard
    assert 'discard_match_primask' in query
    print("PASS: STM32 clears late SOF without touching endpoint IRQ state")
    print("PASS: discarded SOF records exact frame and sampled PRIMASK")


def main():
    test_arm_irq_discards_before_restoring_primask()
    test_stm32_guard_discards_only_sof()
    print("ALL PASS")


if __name__ == '__main__':
    main()
